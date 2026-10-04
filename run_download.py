#!/usr/bin/env python3
"""Edit the settings below, then just run this file -- no command line needed.

    python run_download.py

Everything works exactly as in torrent_dl.py (live speed/time/percentage,
p = pause, r = resume, q = quit, and continuing where it left off on the next
run). This file only fills in the settings for you.
"""

from __future__ import annotations

import sys
from pathlib import Path

import torrent_dl

# ---------------------------------------------------------------------------
# EDIT THESE
# ---------------------------------------------------------------------------

# What to download: either the path to a .torrent file, or a magnet: link.
# Keep the r"..." quoting so Windows backslashes are taken literally.
TORRENT = r"C:\Users\y.ramadan\Downloads\daredevil-s-01-s-02_archive.torrent"
# TORRENT = "magnet:?xt=urn:btih:..."

# Where to save the downloaded files.
SAVE_TO = r"C:\Games"

# Where to keep the resume files that hold your progress.
# None means SAVE_TO\.torrent-state
STATE_DIR = None

MAX_DOWN_KB = 0          # download cap in KB/s, 0 = unlimited
MAX_UP_KB = 0            # upload cap in KB/s, 0 = unlimited
LISTEN_PORT = 6881       # incoming port
SHOW_FILES = True        # show per-file progress
SEQUENTIAL = False       # download pieces in order (for streaming media)
KEEP_SEEDING = False     # keep running after the download finishes
START_PAUSED = False     # add the torrent but wait for you to press r
REFRESH_SECONDS = 0.5    # how often the display updates

# Peers to dial directly, e.g. ["192.168.1.50:6881"]. Normally leave empty --
# this is for a LAN transfer or a torrent with no tracker.
PEERS: list = []

# ---------------------------------------------------------------------------


def build_argv() -> list:
    """Turn the settings above into the arguments torrent_dl expects."""
    argv = [
        TORRENT,
        "-o", str(SAVE_TO),
        "--port", str(LISTEN_PORT),
        "--interval", str(REFRESH_SECONDS),
    ]
    if STATE_DIR:
        argv += ["--state-dir", str(STATE_DIR)]
    if MAX_DOWN_KB:
        argv += ["--max-down", str(MAX_DOWN_KB)]
    if MAX_UP_KB:
        argv += ["--max-up", str(MAX_UP_KB)]
    if SHOW_FILES:
        argv.append("--files")
    if SEQUENTIAL:
        argv.append("--sequential")
    if KEEP_SEEDING:
        argv.append("--seed")
    if START_PAUSED:
        argv.append("--paused")
    for peer in PEERS:
        argv += ["--peer", str(peer)]
    return argv


def check_settings() -> str:
    """Return a problem with the settings above, or '' if they look usable."""
    if not str(TORRENT).strip():
        return "TORRENT is empty."
    if str(TORRENT).lower().startswith("magnet:"):
        return ""
    source = Path(TORRENT)
    if source.is_dir():
        return f"TORRENT points at a folder, not a .torrent file:\n  {source}"
    if not source.is_file():
        return f"TORRENT is not a magnet link and there is no such file:\n  {source}"
    return ""


def main() -> int:
    problem = check_settings()
    if problem:
        print(f"error: {problem}")
        print(f"\nEdit the settings at the top of {Path(__file__).name} "
              "and run it again.")
        return 2
    return torrent_dl.main(build_argv())


if __name__ == "__main__":
    sys.exit(main())
