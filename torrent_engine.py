"""Single-torrent download engine built on libtorrent 2.x.

This wraps the parts of libtorrent that are easy to get wrong:

  * fast-resume data, so pausing (or killing the process) continues the
    download later instead of starting it over
  * manual pause/resume: the auto-managed queue is switched OFF, because
    libtorrent's queue manager will otherwise un-pause torrents behind your back
  * a smoothed transfer rate, so the ETA doesn't jitter with every sample

The engine never prints anything -- it returns Snapshot objects and leaves
presentation to the caller. See torrent_dl.py for the command-line front end.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import libtorrent as lt

MAGNET_PREFIX = "magnet:"

# How often, while running, to refresh the on-disk resume file.
RESUME_INTERVAL = 30.0

# Window (seconds) used to average the download rate for the ETA.
RATE_WINDOW = 15.0

# Below this rate an ETA is noise rather than an estimate, and any estimate
# beyond MAX_ETA is not worth showing. Both report as "unknown" instead.
MIN_ETA_RATE = 1024.0          # bytes/s
MAX_ETA = 30 * 86400.0         # seconds

_STATE_LABELS = {
    lt.torrent_status.states.checking_files: "checking files",
    lt.torrent_status.states.checking_resume_data: "checking resume data",
    lt.torrent_status.states.downloading_metadata: "fetching metadata",
    lt.torrent_status.states.downloading: "downloading",
    lt.torrent_status.states.finished: "finished",
    lt.torrent_status.states.seeding: "seeding",
    lt.torrent_status.states.allocating: "allocating",
}


def infohash_hex(hashes) -> str:
    """Stable hex id for a torrent: the v2 hash when present, else v1."""
    if hashes.has_v2():
        return str(hashes.v2)
    if hashes.has_v1():
        return str(hashes.v1)
    return ""


@dataclass(frozen=True)
class FileProgress:
    path: str
    size: int
    done: int

    @property
    def percent(self) -> float:
        return 100.0 * self.done / self.size if self.size else 0.0


@dataclass(frozen=True)
class Snapshot:
    """Everything worth showing about a download at one instant."""

    name: str
    state: str
    paused: bool
    finished: bool
    has_metadata: bool

    percent: float          # 0-100, of the bytes we actually want
    done_bytes: int
    total_bytes: int

    download_rate: float    # bytes/s, instantaneous (libtorrent's own figure)
    upload_rate: float
    average_rate: float     # bytes/s, smoothed over RATE_WINDOW -- drives the ETA

    eta_seconds: Optional[float]   # None while unknown
    elapsed_seconds: float         # active time, carried across restarts

    peers: int
    seeds: int
    swarm_seeds: int        # -1 when the tracker hasn't told us
    swarm_peers: int

    pieces_done: int
    pieces_total: int

    session_downloaded: int  # payload this run
    session_uploaded: int
    all_time_downloaded: int
    all_time_uploaded: int

    save_path: str
    error: str


class TorrentDownload:
    """One torrent, one libtorrent session, with pause/resume that persists.

    Typical use:

        with TorrentDownload(source, save_path="downloads").start() as dl:
            while True:
                snap = dl.poll()
                ...
                dl.pause()   # writes resume data
                dl.resume()
    """

    def __init__(
        self,
        source: str,
        save_path: str = ".",
        state_dir: Optional[str] = None,
        port: int = 6881,
        download_limit: int = 0,      # bytes/s, 0 = unlimited
        upload_limit: int = 0,
        sequential: bool = False,
        start_paused: bool = False,
        restart: bool = False,
        extra_settings: Optional[dict] = None,
    ) -> None:
        self.source = str(source)
        self.save_path = Path(save_path).expanduser().resolve()
        self.state_dir = (
            Path(state_dir).expanduser().resolve()
            if state_dir
            else self.save_path / ".torrent-state"
        )
        self.port = int(port)
        self.download_limit = int(download_limit)
        self.upload_limit = int(upload_limit)
        self.sequential = bool(sequential)
        self.start_paused = bool(start_paused)
        # Discard any saved state and re-verify what is already on disk.
        # This never deletes downloaded data.
        self.restart = bool(restart)
        # Any libtorrent settings_pack key; merged last so it wins.
        self.extra_settings = dict(extra_settings or {})

        self._session: Optional[lt.session] = None
        self._handle: Optional[lt.torrent_handle] = None
        self._infohash = ""
        self._resume_file: Optional[Path] = None
        self._resume_writes = 0
        self._last_resume_request = 0.0
        self._samples: deque = deque(maxlen=256)
        self._events: list[str] = []
        self._resumed_from_disk = False
        self._error = ""

    # -- lifecycle --------------------------------------------------------

    def start(self) -> "TorrentDownload":
        if self._session is not None:
            raise RuntimeError("already started")
        self.save_path.mkdir(parents=True, exist_ok=True)
        self.state_dir.mkdir(parents=True, exist_ok=True)

        self._session = lt.session(self._settings())
        self._handle = self._session.add_torrent(self._build_params())

        # Take the torrent out of the auto-managed queue so that pause() and
        # resume() mean exactly what they say.
        self._handle.unset_flags(lt.torrent_flags.auto_managed)
        if self.sequential:
            self._handle.set_flags(lt.torrent_flags.sequential_download)
        if self.start_paused:
            self._handle.pause()
        return self

    def close(self, save: bool = True) -> None:
        if self._handle is not None and save:
            try:
                self.save_resume()
            except Exception as exc:
                self._events.append(f"could not save resume data: {exc}")
        if self._session is not None:
            self._session.pause()
        self._handle = None
        self._session = None

    def __enter__(self) -> "TorrentDownload":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def _settings(self) -> dict:
        settings = {
            "listen_interfaces": f"0.0.0.0:{self.port},[::]:{self.port}",
            # storage is required: save_resume_data_alert arrives on it
            "alert_mask": (
                lt.alert_category.status
                | lt.alert_category.error
                | lt.alert_category.storage
            ),
            "user_agent": f"pytorrent/1.0 libtorrent/{lt.version}",
            "enable_dht": True,
            "enable_lsd": True,
            "enable_upnp": True,
            "enable_natpmp": True,
            "announce_to_all_trackers": True,
            "announce_to_all_tiers": True,
            "download_rate_limit": self.download_limit,
            "upload_rate_limit": self.upload_limit,
        }
        settings.update(self.extra_settings)
        return settings

    def _build_params(self):
        """Build add_torrent_params from the source, preferring saved resume data."""
        torrent_info = None
        if self.source.lower().startswith(MAGNET_PREFIX):
            params = lt.parse_magnet_uri(self.source)
            hashes = params.info_hashes
        else:
            path = Path(self.source).expanduser()
            if not path.is_file():
                raise FileNotFoundError(f"no such .torrent file: {path}")
            torrent_info = lt.torrent_info(str(path))
            params = lt.add_torrent_params()
            params.ti = torrent_info
            hashes = torrent_info.info_hashes()

        self._infohash = infohash_hex(hashes)
        self._resume_file = self.state_dir / f"{self._infohash}.resume"

        if self.restart and self._resume_file.is_file():
            self._resume_file.unlink()
            self._events.append("discarded saved state, re-verifying files")

        if self._resume_file.is_file():
            try:
                params = lt.read_resume_data(self._resume_file.read_bytes())
                self._resumed_from_disk = True
                self._events.append("resuming from saved state")
            except Exception as exc:
                self._events.append(f"ignoring unreadable resume file: {exc}")
            else:
                # A magnet's resume data carries the info dict once metadata
                # has been seen; for .torrent sources re-attach it regardless.
                if torrent_info is not None and params.ti is None:
                    params.ti = torrent_info

        params.save_path = str(self.save_path)

        flags = params.flags
        flags &= ~lt.torrent_flags.auto_managed
        flags &= ~lt.torrent_flags.duplicate_is_error
        if self.start_paused:
            flags |= lt.torrent_flags.paused
        else:
            flags &= ~lt.torrent_flags.paused
        params.flags = flags
        return params

    # -- controls ---------------------------------------------------------

    def pause(self, save: bool = True) -> None:
        self._require_started()
        self._handle.unset_flags(lt.torrent_flags.auto_managed)
        self._handle.pause()
        if save:
            self.save_resume()

    def resume(self) -> None:
        self._require_started()
        self._handle.unset_flags(lt.torrent_flags.auto_managed)
        self._handle.resume()

    def toggle(self) -> bool:
        """Flip pause state. Returns True if now paused."""
        if self.is_paused():
            self.resume()
            return False
        self.pause()
        return True

    def is_paused(self) -> bool:
        self._require_started()
        return bool(self._handle.status().flags & lt.torrent_flags.paused)

    def add_peer(self, host: str, port: int) -> None:
        """Dial a known peer directly, bypassing trackers and the DHT.

        Useful for a LAN transfer or a tracker-less torrent. libtorrent
        ignores this while the torrent is paused or still checking.
        """
        self._require_started()
        self._handle.connect_peer((host, int(port)))

    def set_limits(self, download: Optional[int] = None,
                   upload: Optional[int] = None) -> None:
        """Cap transfer rates in bytes/s (0 = unlimited)."""
        self._require_started()
        if download is not None:
            self.download_limit = int(download)
            self._handle.set_download_limit(self.download_limit)
        if upload is not None:
            self.upload_limit = int(upload)
            self._handle.set_upload_limit(self.upload_limit)

    # -- resume data ------------------------------------------------------

    def _request_resume(self) -> None:
        if self._handle is None or not self._handle.is_valid():
            return
        if not self._handle.status().has_metadata:
            return  # nothing meaningful to save yet
        self._handle.save_resume_data(
            lt.save_resume_flags_t.save_info_dict
            | lt.save_resume_flags_t.flush_disk_cache
        )

    def save_resume(self, timeout: float = 8.0) -> bool:
        """Ask for resume data and wait for it to hit disk. True if written."""
        self._require_started()
        if not self._handle.status().has_metadata:
            return False
        before = self._resume_writes
        self._request_resume()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for alert in self._session.pop_alerts():
                self._on_alert(alert)
            if self._resume_writes > before:
                return True
            time.sleep(0.05)
        self._events.append("timed out waiting for resume data")
        return False

    def _write_resume(self, params) -> None:
        assert self._resume_file is not None
        data = lt.write_resume_data_buf(params)
        tmp = self._resume_file.parent / (self._resume_file.name + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(self._resume_file)          # atomic swap
        self._resume_writes += 1

    def forget_resume(self) -> None:
        """Delete saved state so the next run starts from scratch."""
        if self._resume_file and self._resume_file.is_file():
            self._resume_file.unlink()

    # -- polling ----------------------------------------------------------

    def poll(self) -> Snapshot:
        """Pump libtorrent's alert queue and return the current snapshot.

        Call this a few times a second; libtorrent needs its alerts drained.
        """
        self._require_started()
        for alert in self._session.pop_alerts():
            self._on_alert(alert)

        status = self._handle.status()
        now = time.monotonic()
        self._samples.append((now, status.total_wanted_done))

        if (
            not (status.flags & lt.torrent_flags.paused)
            and status.has_metadata
            and getattr(status, "need_save_resume", True)
            and now - self._last_resume_request >= RESUME_INTERVAL
        ):
            self._request_resume()
            self._last_resume_request = now

        return self._snapshot(status)

    def _on_alert(self, alert) -> None:
        if isinstance(alert, lt.save_resume_data_alert):
            self._write_resume(alert.params)
        elif isinstance(alert, lt.save_resume_data_failed_alert):
            self._events.append(f"resume save failed: {alert.message()}")
        elif isinstance(alert, lt.metadata_received_alert):
            self._events.append("metadata received")
            self._request_resume()
        elif isinstance(alert, lt.torrent_finished_alert):
            self._events.append("download complete")
            self._request_resume()
        elif isinstance(alert, (lt.torrent_error_alert, lt.file_error_alert)):
            self._error = alert.message()
            self._events.append(self._error)
        elif isinstance(alert, lt.tracker_error_alert):
            self._events.append(f"tracker: {alert.message()}")

    def _average_rate(self) -> float:
        """Download rate over the trailing RATE_WINDOW, in bytes/s."""
        if len(self._samples) < 2:
            return 0.0
        newest_time, newest_done = self._samples[-1]
        oldest_time, oldest_done = self._samples[0]
        for stamp, done in self._samples:
            if newest_time - stamp <= RATE_WINDOW:
                oldest_time, oldest_done = stamp, done
                break
        span = newest_time - oldest_time
        if span <= 0:
            return 0.0
        return max(newest_done - oldest_done, 0) / span

    def _snapshot(self, status) -> Snapshot:
        total = status.total_wanted
        done = status.total_wanted_done
        percent = (100.0 * done / total) if total else 100.0 * status.progress

        average = self._average_rate()
        paused = bool(status.flags & lt.torrent_flags.paused)
        remaining = max(total - done, 0)

        # Prefer the smoothed rate for the ETA; fall back to the live one
        # before the window has filled up.
        rate = average if average > 0 else status.download_rate
        if status.is_finished:
            eta: Optional[float] = 0.0
        elif paused or rate < MIN_ETA_RATE or not total:
            eta = None
        else:
            eta = remaining / rate
            if eta > MAX_ETA:
                eta = None          # too slow to mean anything yet

        torrent_file = self._handle.torrent_file() if status.has_metadata else None

        if paused:
            state = "paused"
        else:
            state = _STATE_LABELS.get(status.state, str(status.state))

        return Snapshot(
            name=status.name or self._infohash or "(unknown)",
            state=state,
            paused=paused,
            finished=bool(status.is_finished),
            has_metadata=bool(status.has_metadata),
            percent=percent,
            done_bytes=done,
            total_bytes=total,
            # libtorrent's rates are a trailing one-second average, so they
            # linger for a moment after a pause. Nothing is moving, so say so.
            download_rate=0.0 if paused else float(status.download_rate),
            upload_rate=0.0 if paused else float(status.upload_rate),
            average_rate=average,
            eta_seconds=eta,
            elapsed_seconds=status.active_duration.total_seconds(),
            peers=status.num_peers,
            seeds=status.num_seeds,
            swarm_seeds=status.num_complete,
            swarm_peers=status.num_incomplete,
            pieces_done=status.num_pieces,
            pieces_total=torrent_file.num_pieces() if torrent_file else 0,
            session_downloaded=status.total_payload_download,
            session_uploaded=status.total_payload_upload,
            all_time_downloaded=status.all_time_download,
            all_time_uploaded=status.all_time_upload,
            save_path=status.save_path,
            error=self._error,
        )

    def files(self) -> list:
        """Per-file progress; empty until metadata arrives."""
        self._require_started()
        status = self._handle.status()
        if not status.has_metadata:
            return []
        torrent_file = self._handle.torrent_file()
        if torrent_file is None:
            return []
        storage = torrent_file.files()
        progress = self._handle.file_progress()
        return [
            FileProgress(
                path=storage.file_path(i),
                size=storage.file_size(i),
                done=progress[i] if i < len(progress) else 0,
            )
            for i in range(storage.num_files())
        ]

    def drain_events(self) -> list:
        """Pop human-readable notices accumulated since the last call."""
        events, self._events = self._events, []
        return events

    # -- misc -------------------------------------------------------------

    @property
    def infohash(self) -> str:
        return self._infohash

    @property
    def resume_file(self) -> Optional[Path]:
        return self._resume_file

    @property
    def resumed_from_disk(self) -> bool:
        return self._resumed_from_disk

    def _require_started(self) -> None:
        if self._session is None or self._handle is None:
            raise RuntimeError("call start() first")
