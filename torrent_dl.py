#!/usr/bin/env python3
"""Command-line torrent downloader with live stats and pause/resume.

    python torrent_dl.py "magnet:?xt=urn:btih:..." -o downloads
    python torrent_dl.py ubuntu.torrent -o downloads --files

While it runs:

    p        pause (progress is written to disk)
    r        resume
    space    toggle pause/resume
    f        show/hide per-file progress
    q        quit, saving progress

Quitting and re-running the same command continues where it left off.
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import time
from collections import deque
from typing import Optional

from torrent_engine import Snapshot, TorrentDownload

BAR_WIDTH = 38


# -- formatting -----------------------------------------------------------

def human_bytes(value: float) -> str:
    units = ("B", "KB", "MB", "GB", "TB", "PB")
    size = float(value)
    index = 0
    while abs(size) >= 1024.0 and index < len(units) - 1:
        size /= 1024.0
        index += 1
    return f"{size:.0f} {units[index]}" if index == 0 else f"{size:.2f} {units[index]}"


def human_rate(value: float) -> str:
    return human_bytes(value) + "/s"


def human_time(seconds: Optional[float]) -> str:
    if seconds is None:
        return "--:--:--"
    total = int(max(seconds, 0))
    if total >= 99 * 86400:
        return "  a while"
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    if days:
        return f"{days}d {hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def _console_can(text: str) -> bool:
    encoding = getattr(sys.stdout, "encoding", None) or "ascii"
    try:
        text.encode(encoding)
        return True
    except (UnicodeEncodeError, LookupError):
        return False


# Probe the glyphs themselves: a Windows console redirected to a cp1252 pipe
# cannot encode them, and printing one would abort the download.
FANCY = _console_can("█░↓↑")
GLYPHS = {
    "full": "█" if FANCY else "#",
    "empty": "░" if FANCY else "-",
    "down": "↓" if FANCY else "v",
    "up": "↑" if FANCY else "^",
}


def progress_bar(percent: float, width: int = BAR_WIDTH) -> str:
    filled = int(round(width * max(0.0, min(percent, 100.0)) / 100.0))
    return GLYPHS["full"] * filled + GLYPHS["empty"] * (width - filled)


# -- terminal plumbing ----------------------------------------------------

def enable_ansi() -> bool:
    """Turn on VT escape processing. Returns True if the terminal can redraw."""
    if not sys.stdout.isatty():
        return False
    if os.name != "nt":
        return True
    import ctypes

    kernel32 = ctypes.windll.kernel32
    handle = kernel32.GetStdHandle(-11)          # STD_OUTPUT_HANDLE
    mode = ctypes.c_uint32()
    if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
        return False
    enable_vt = 0x0004                            # ENABLE_VIRTUAL_TERMINAL_PROCESSING
    return bool(kernel32.SetConsoleMode(handle, mode.value | enable_vt))


def _on_break(signum, frame):
    raise KeyboardInterrupt


def install_break_handler() -> None:
    """Make Ctrl+Break behave like Ctrl+C.

    On Windows, Python turns Ctrl+C into KeyboardInterrupt but lets Ctrl+Break
    terminate the process outright -- which would throw away the resume data.
    """
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _on_break)


class LiveDisplay:
    """Repaints a multi-line block in place, or falls back to plain lines."""

    def __init__(self) -> None:
        self.can_redraw = enable_ansi()
        self._painted = 0
        self._last_plain = 0.0

    def render(self, panel: str, line: str) -> None:
        if not self.can_redraw:
            # Piped output or a dumb terminal: log one status line now and then.
            now = time.monotonic()
            if now - self._last_plain >= 2.0:
                self._last_plain = now
                print(line, flush=True)
            return
        lines = panel.split("\n")
        out = []
        if self._painted:
            out.append(f"\x1b[{self._painted}A")
        for line in lines:
            out.append("\x1b[2K" + line + "\n")   # 2K = clear whole line
        sys.stdout.write("".join(out))
        sys.stdout.flush()
        self._painted = len(lines)

    def done(self) -> None:
        self._painted = 0


class KeyReader:
    """Non-blocking single-key reads. A no-op when stdin isn't a terminal."""

    def __init__(self) -> None:
        self.enabled = sys.stdin.isatty()
        self._fd = None
        self._saved = None

    def __enter__(self) -> "KeyReader":
        if self.enabled and os.name != "nt":
            import termios
            import tty

            self._fd = sys.stdin.fileno()
            self._saved = termios.tcgetattr(self._fd)
            tty.setcbreak(self._fd)
        return self

    def __exit__(self, *exc_info) -> None:
        if self._saved is not None:
            import termios

            termios.tcsetattr(self._fd, termios.TCSADRAIN, self._saved)

    def get(self) -> Optional[str]:
        if not self.enabled:
            return None
        if os.name == "nt":
            import msvcrt

            if msvcrt.kbhit():
                return msvcrt.getwch()
            return None
        import select

        if select.select([sys.stdin], [], [], 0)[0]:
            return sys.stdin.read(1)
        return None


# -- the panel ------------------------------------------------------------

def compose(snap: Snapshot, download: TorrentDownload,
            notices: deque, show_files: bool) -> str:
    size = human_bytes(snap.total_bytes) if snap.total_bytes else "size unknown"
    lines = [
        f"{snap.name}  ({size})",
        f"[{progress_bar(snap.percent)}] {snap.percent:6.2f}%",
        "",
    ]

    swarm = ""
    if snap.swarm_seeds >= 0 or snap.swarm_peers >= 0:
        swarm = f"   swarm {max(snap.swarm_seeds, 0)} seeds / {max(snap.swarm_peers, 0)} peers"

    down = f"{GLYPHS['down']} {human_rate(snap.download_rate):>12}"
    up = f"{GLYPHS['up']} {human_rate(snap.upload_rate):>12}"

    lines += [
        f"  status     {snap.state:<22} peers  {snap.peers} connected"
        f" ({snap.seeds} seeds){swarm}",
        f"  speed      {down}   {up}   avg {human_rate(snap.average_rate)}",
        f"  done       {human_bytes(snap.done_bytes)} / {human_bytes(snap.total_bytes)}"
        f"        pieces {snap.pieces_done}/{snap.pieces_total}",
        f"  time       elapsed {human_time(snap.elapsed_seconds)}"
        f"      remaining {human_time(snap.eta_seconds)}",
        f"  traffic    down {human_bytes(snap.all_time_downloaded)}"
        f"   up {human_bytes(snap.all_time_uploaded)}",
        f"  saving to  {snap.save_path}",
    ]

    if snap.error:
        lines.append(f"  error      {snap.error}")

    if show_files:
        entries = download.files()
        lines.append("")
        if not entries:
            lines.append("  (file list arrives with the metadata)")
        for entry in entries[:12]:
            name = entry.path if len(entry.path) <= 48 else "..." + entry.path[-45:]
            lines.append(
                f"  {entry.percent:6.2f}%  {human_bytes(entry.size):>10}  {name}"
            )
        if len(entries) > 12:
            lines.append(f"  ... and {len(entries) - 12} more files")

    if notices:
        lines.append("")
        for note in notices:
            lines.append(f"  * {note}")

    lines += [
        "",
        "  [p] pause   [r] resume   [space] toggle   [f] files   [q] quit",
    ]
    return "\n".join(lines)


def compose_line(snap: Snapshot) -> str:
    """One-line status, for piped output and log files."""
    return "  ".join([
        f"{snap.percent:6.2f}%",
        f"{human_bytes(snap.done_bytes)}/{human_bytes(snap.total_bytes)}",
        f"down {human_rate(snap.download_rate)}",
        f"eta {human_time(snap.eta_seconds)}",
        f"peers {snap.peers}",
        snap.state,
    ])


# -- entry point ----------------------------------------------------------

def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download a torrent with live stats and pause/resume.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("source", help="magnet: URI or path to a .torrent file")
    parser.add_argument("-o", "--output", default="downloads",
                        help="where to save the data (default: downloads)")
    parser.add_argument("--state-dir", default=None,
                        help="where to keep resume files "
                             "(default: <output>/.torrent-state)")
    parser.add_argument("--port", type=int, default=6881, help="listen port")
    parser.add_argument("--max-down", type=float, default=0,
                        help="download cap in KB/s (0 = unlimited)")
    parser.add_argument("--max-up", type=float, default=0,
                        help="upload cap in KB/s (0 = unlimited)")
    parser.add_argument("--paused", action="store_true",
                        help="add the torrent but don't start it yet")
    parser.add_argument("--sequential", action="store_true",
                        help="download pieces in order (useful for media)")
    parser.add_argument("--seed", action="store_true",
                        help="keep seeding after the download finishes")
    parser.add_argument("--files", action="store_true",
                        help="show per-file progress")
    parser.add_argument("--peer", action="append", default=[], metavar="HOST:PORT",
                        help="dial a known peer directly (repeatable); useful on "
                             "a LAN or for a tracker-less torrent")
    parser.add_argument("--interval", type=float, default=0.5,
                        help="seconds between refreshes (default: 0.5)")
    parser.add_argument("--restart", action="store_true",
                        help="discard the saved state and re-verify the files "
                             "on disk (never deletes downloaded data)")
    return parser.parse_args(argv)


def parse_peers(values) -> list:
    """Turn HOST:PORT strings into (host, port) pairs."""
    peers = []
    for item in values:
        host, separator, port = item.rpartition(":")
        if not separator or not port.isdigit():
            raise ValueError(f"expected HOST:PORT, got {item!r}")
        peers.append((host.strip("[]"), int(port)))
    return peers


# States during which libtorrent ignores a connect_peer request.
_NOT_DIALABLE = ("checking resume data", "checking files", "allocating")


def main(argv=None) -> int:
    args = parse_args(argv)
    install_break_handler()
    try:
        peers = parse_peers(args.peer)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    extra_settings = {}
    if args.max_down or args.max_up:
        # libtorrent exempts local-network peers from rate limits by default,
        # which makes --max-down look broken on a LAN. If the user asked for a
        # cap, apply it to every peer.
        extra_settings["ignore_limits_on_local_network"] = False

    download = TorrentDownload(
        args.source,
        save_path=args.output,
        state_dir=args.state_dir,
        port=args.port,
        download_limit=int(args.max_down * 1024),
        upload_limit=int(args.max_up * 1024),
        sequential=args.sequential,
        start_paused=args.paused,
        restart=args.restart,
        extra_settings=extra_settings,
    )

    try:
        download.start()
    except (FileNotFoundError, RuntimeError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:                     # bad magnet, port in use, ...
        print(f"error: could not start the download: {exc}", file=sys.stderr)
        return 2

    print(f"info hash   {download.infohash}")
    print(f"resume file {download.resume_file}")
    print()

    notices: deque = deque(maxlen=3)
    display = LiveDisplay()
    show_files = args.files
    interrupted = False
    final: Optional[Snapshot] = None
    last_dial = 0.0

    try:
        with KeyReader() as keys:
            while True:
                snap = download.poll()
                final = snap
                for event in download.drain_events():
                    notices.append(event)
                    if not display.can_redraw:
                        # No in-place panel to hold them: log events as they happen.
                        print(f"* {event}", flush=True)

                now = time.monotonic()
                if (peers and snap.peers == 0 and not snap.paused
                        and snap.has_metadata and snap.state not in _NOT_DIALABLE
                        and now - last_dial > 3.0):
                    last_dial = now
                    for host, port in peers:
                        download.add_peer(host, port)

                display.render(compose(snap, download, notices, show_files),
                               compose_line(snap))

                if snap.finished and not args.seed:
                    break

                key = keys.get()
                if key:
                    key = key.lower()
                    if key == "q":
                        break
                    if key == "p" and not snap.paused:
                        download.pause()
                        notices.append("paused - progress saved")
                    elif key == "r" and snap.paused:
                        download.resume()
                        notices.append("resumed")
                    elif key == " ":
                        notices.append("paused - progress saved"
                                       if download.toggle() else "resumed")
                    elif key == "f":
                        show_files = not show_files
                        display.done()

                time.sleep(args.interval)
    except KeyboardInterrupt:
        interrupted = True
    finally:
        display.done()
        print()
        saved = False
        try:
            saved = download.save_resume()
        except Exception as exc:
            print(f"warning: could not save resume data: {exc}", file=sys.stderr)
        download.close(save=False)

    if final is not None:
        verb = "Complete" if final.finished else "Stopped"
        if interrupted:
            verb = "Interrupted"
        print(f"{verb}: {final.name}")
        print(f"  {final.percent:.2f}% - {human_bytes(final.done_bytes)}"
              f" of {human_bytes(final.total_bytes)}")
        print(f"  active for {human_time(final.elapsed_seconds)}"
              f", average {human_rate(final.average_rate)}")
        print(f"  files in {final.save_path}")
        if saved and not final.finished:
            print("  progress saved - run the same command again to continue")

    return 0 if (final is not None and final.finished) or interrupted else 1


if __name__ == "__main__":
    sys.exit(main())
