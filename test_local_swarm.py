#!/usr/bin/env python3
"""End-to-end test for torrent_engine, with no internet required.

Builds a real torrent from a temporary file, seeds it from one libtorrent
session, and downloads it with TorrentDownload from another -- both on
localhost. It then checks the things that are easy to get wrong:

  1. progress, speed and ETA actually move
  2. pause really stops the transfer
  3. resume data lands on disk
  4. destroying the downloader and building a new one CONTINUES from the
     saved progress instead of starting over (the restart case)
  5. the finished file is byte-identical to the original

Run:  python test_local_swarm.py
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

import libtorrent as lt

from torrent_engine import TorrentDownload

PAYLOAD_BYTES = 12 * 1024 * 1024      # 12 MB
PIECE_BYTES = 64 * 1024               # 192 pieces, so progress is fine-grained
SEED_PORT = 6991
LEECH_PORT = 6992
RATE_LIMIT = 1536 * 1024              # throttle so there is time to pause mid-way

# libtorrent exempts local-network peers from rate limits by default, which on
# loopback means the whole payload lands between two polls -- too fast to
# observe speed or elapsed time. Turn the exemption off for the test.
LOCAL_LIMITS = {"ignore_limits_on_local_network": False}

failures: list = []


def check(condition: bool, label: str, detail: str = "") -> bool:
    mark = "PASS" if condition else "FAIL"
    print(f"  [{mark}] {label}" + (f"  ({detail})" if detail else ""))
    if not condition:
        failures.append(label)
    return bool(condition)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_torrent(seed_dir: Path) -> tuple:
    """Create a payload file and a matching .torrent. Returns (path, digest)."""
    payload = seed_dir / "payload.bin"
    payload.write_bytes(os.urandom(PAYLOAD_BYTES))

    creator = lt.create_torrent(lt.list_files(str(payload)), PIECE_BYTES)
    creator.set_creator("torrent_engine self-test")
    lt.set_piece_hashes(creator, str(seed_dir))

    torrent_path = seed_dir / "payload.torrent"
    torrent_path.write_bytes(lt.bencode(creator.generate()))
    return torrent_path, sha256(payload)


def start_seeder(torrent_path: Path, seed_dir: Path):
    """A plain libtorrent session serving the payload on localhost."""
    session = lt.session({
        "listen_interfaces": f"127.0.0.1:{SEED_PORT}",
        "alert_mask": lt.alert_category.error,
        "enable_dht": False,
        "enable_lsd": False,
        "enable_upnp": False,
        "enable_natpmp": False,
    })
    params = lt.add_torrent_params()
    params.ti = lt.torrent_info(str(torrent_path))
    params.save_path = str(seed_dir)
    # The data is already there: skip the recheck and announce as a seed.
    params.flags |= lt.torrent_flags.seed_mode
    # default_flags contains BOTH paused and auto_managed. Clearing only
    # auto_managed leaves the torrent paused forever, and a paused torrent
    # still listens but refuses the handshake -- so clear paused as well.
    params.flags &= ~lt.torrent_flags.auto_managed
    params.flags &= ~lt.torrent_flags.paused
    session.add_torrent(params)
    return session


def pump(download: TorrentDownload, seconds: float, label: str,
         until=None, connect_to_seed: bool = True):
    """Poll for a while, printing a one-line trace. Returns the last snapshot."""
    deadline = time.monotonic() + seconds
    snap = None
    last_print = 0.0
    last_dial = 0.0
    while time.monotonic() < deadline:
        snap = download.poll()
        now = time.monotonic()
        if (connect_to_seed and snap.has_metadata and snap.peers == 0
                and not snap.paused and now - last_dial > 2.0):
            # No tracker or DHT in this test: introduce the peer by hand,
            # and not too often -- re-dialing every poll never settles.
            last_dial = now
            download._handle.connect_peer(("127.0.0.1", SEED_PORT))
        if now - last_print > 0.7:
            last_print = now
            eta = "?" if snap.eta_seconds is None else f"{snap.eta_seconds:5.1f}s"
            print(f"    {label}: {snap.percent:6.2f}%  "
                  f"{snap.download_rate / 1024:8.1f} KB/s  "
                  f"eta {eta}  {snap.state}")
        if until is not None and until(snap):
            break
        time.sleep(0.2)
    return snap


def main() -> int:
    workspace = Path(tempfile.mkdtemp(prefix="torrent-test-"))
    seed_dir = workspace / "seed"
    dest_dir = workspace / "dest"
    seed_dir.mkdir()
    print(f"workspace: {workspace}\n")

    seeder = None
    try:
        print("building a 12 MB torrent...")
        torrent_path, original_digest = build_torrent(seed_dir)
        info = lt.torrent_info(str(torrent_path))
        print(f"  {info.name()}  {info.total_size()} bytes  "
              f"{info.num_pieces()} pieces\n")

        seeder = start_seeder(torrent_path, seed_dir)

        # -- phase 1: download part of it, then pause --------------------
        print("phase 1: start downloading, then pause")
        first = TorrentDownload(
            str(torrent_path),
            save_path=str(dest_dir),
            port=LEECH_PORT,
            download_limit=RATE_LIMIT,
            extra_settings=LOCAL_LIMITS,
        ).start()

        snap = pump(first, 25, "dl", until=lambda s: s.percent >= 25)
        check(snap.percent > 0, "progress advances", f"{snap.percent:.2f}%")
        check(snap.download_rate > 0, "download speed is reported",
              f"{snap.download_rate / 1024:.1f} KB/s")
        check(snap.eta_seconds is not None, "ETA is computed",
              f"{snap.eta_seconds:.1f}s" if snap.eta_seconds else "none")
        check(snap.elapsed_seconds > 0, "elapsed time is tracked",
              f"{snap.elapsed_seconds:.1f}s")
        check(snap.peers >= 1, "connected to the seeder", f"{snap.peers} peer(s)")
        percent_before_pause = snap.percent

        first.pause()
        paused_snap = first.poll()
        check(paused_snap.paused, "pause takes effect", paused_snap.state)

        # Give it a moment: a paused torrent must not keep pulling bytes.
        bytes_at_pause = first.poll().done_bytes
        pump(first, 3, "paused", connect_to_seed=False)
        after = first.poll()
        check(after.done_bytes == bytes_at_pause, "no data flows while paused",
              f"{bytes_at_pause} -> {after.done_bytes} bytes")
        check(after.paused, "still paused after waiting")

        resume_file = first.resume_file
        check(resume_file is not None and resume_file.is_file(),
              "resume data written to disk",
              str(resume_file.name) if resume_file else "none")

        # -- phase 2: resume in the same process -------------------------
        print("\nphase 2: resume the same download")
        first.resume()
        snap = pump(first, 15, "dl", until=lambda s: s.percent >= percent_before_pause + 5)
        check(not snap.paused, "resume clears the paused state", snap.state)
        check(snap.percent > percent_before_pause, "progress continues after resume",
              f"{percent_before_pause:.2f}% -> {snap.percent:.2f}%")

        # -- phase 3: throw the object away and start over ---------------
        # This is the real test of persistence: a brand-new engine, as if the
        # process had been killed and the command re-run.
        print("\nphase 3: destroy the downloader and start a fresh one")
        first.pause()
        percent_before_restart = first.poll().percent
        first.close()

        second = TorrentDownload(
            str(torrent_path),
            save_path=str(dest_dir),
            port=LEECH_PORT,
            download_limit=RATE_LIMIT,
            extra_settings=LOCAL_LIMITS,
        ).start()
        check(second.resumed_from_disk, "new instance loaded the saved state")

        # Let the resume-data check finish before reading progress.
        restarted = pump(second, 20, "check",
                         until=lambda s: s.state not in
                         ("checking resume data", "checking files", "allocating"))
        check(restarted.percent >= percent_before_restart - 1.0,
              "continues instead of restarting from zero",
              f"{percent_before_restart:.2f}% -> {restarted.percent:.2f}%")

        # -- phase 4: finish and verify the bytes ------------------------
        print("\nphase 4: finish the download")
        second.set_limits(download=0)          # lift the cap for the last stretch
        final = pump(second, 90, "dl", until=lambda s: s.finished)
        check(final.finished, "download reaches 100%", f"{final.percent:.2f}%")
        check(final.eta_seconds == 0.0, "ETA is zero once finished")
        second.close()

        downloaded = dest_dir / info.name()
        check(downloaded.is_file(), "output file exists", str(downloaded.name))
        if downloaded.is_file():
            check(downloaded.stat().st_size == PAYLOAD_BYTES, "output size matches",
                  f"{downloaded.stat().st_size} bytes")
            check(sha256(downloaded) == original_digest,
                  "output is byte-identical to the original")
    finally:
        if seeder is not None:
            seeder.pause()
            del seeder
        time.sleep(0.5)
        shutil.rmtree(workspace, ignore_errors=True)

    print()
    if failures:
        print(f"{len(failures)} check(s) FAILED:")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
