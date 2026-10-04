#!/usr/bin/env python3
"""Test the torrent_dl.py command-line front end for real.

Seeds a torrent locally, runs the actual CLI as a subprocess against it, then
interrupts it the way a user would (Ctrl+C) and checks that it reports a
summary and saves its progress. Also checks the panel renderer directly with
a synthetic snapshot, which is where format bugs hide.

Run:  python test_cli.py
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import libtorrent as lt

import torrent_dl
from test_local_swarm import build_torrent, start_seeder
from torrent_engine import Snapshot

SEED_PORT = 6991      # must match test_local_swarm.start_seeder
CLI_PORT = 6993
RUN_SECONDS = 14

failures: list = []


def check(condition: bool, label: str, detail: str = "") -> bool:
    mark = "PASS" if condition else "FAIL"
    print(f"  [{mark}] {label}" + (f"  ({detail})" if detail else ""))
    if not condition:
        failures.append(label)
    return bool(condition)


def test_renderer() -> None:
    """Exercise the panel and formatters without any network involved."""
    print("panel renderer")

    check(torrent_dl.human_bytes(0) == "0 B", "human_bytes(0)",
          torrent_dl.human_bytes(0))
    check(torrent_dl.human_bytes(1536) == "1.50 KB", "human_bytes(1536)",
          torrent_dl.human_bytes(1536))
    check(torrent_dl.human_bytes(5 * 1024 ** 3) == "5.00 GB", "human_bytes(5 GB)",
          torrent_dl.human_bytes(5 * 1024 ** 3))
    check(torrent_dl.human_time(None) == "--:--:--", "human_time(None)")
    check(torrent_dl.human_time(3661) == "01:01:01", "human_time(3661)",
          torrent_dl.human_time(3661))
    check(torrent_dl.human_time(90061).startswith("1d "), "human_time(> a day)",
          torrent_dl.human_time(90061))
    bar = torrent_dl.progress_bar(50, width=10)
    check(len(bar) == 10, "progress bar keeps its width", repr(bar))
    check(torrent_dl.progress_bar(0, 10).strip(GLYPH_EMPTY := torrent_dl.GLYPHS["empty"])
          == "", "0% bar is empty")
    check(torrent_dl.progress_bar(100, 10).strip(torrent_dl.GLYPHS["full"]) == "",
          "100% bar is full")

    try:
        torrent_dl.parse_peers(["127.0.0.1:6881", "[::1]:6882"])
        ok = True
    except ValueError:
        ok = False
    check(ok, "parse_peers accepts host:port and [v6]:port")
    try:
        torrent_dl.parse_peers(["no-port-here"])
        check(False, "parse_peers rejects malformed input")
    except ValueError:
        check(True, "parse_peers rejects malformed input")

    snap = Snapshot(
        name="Example Payload", state="downloading", paused=False, finished=False,
        has_metadata=True, percent=42.5, done_bytes=4 * 1024 ** 2,
        total_bytes=10 * 1024 ** 2, download_rate=1.5 * 1024 ** 2,
        upload_rate=64 * 1024, average_rate=1.4 * 1024 ** 2, eta_seconds=125.0,
        elapsed_seconds=95.0, peers=7, seeds=3, swarm_seeds=12, swarm_peers=40,
        pieces_done=85, pieces_total=200, session_downloaded=4 * 1024 ** 2,
        session_uploaded=1024, all_time_downloaded=4 * 1024 ** 2,
        all_time_uploaded=1024, save_path=r"C:\downloads", error="",
    )

    class FakeDownload:
        def files(self):
            return []

    from collections import deque
    panel = torrent_dl.compose(snap, FakeDownload(), deque(["metadata received"]), False)
    check("42.50%" in panel, "panel shows the percentage")
    check("downloading" in panel, "panel shows the state")
    check("7 connected" in panel, "panel shows peer count")
    check("00:01:35" in panel, "panel shows elapsed time")
    check("00:02:05" in panel, "panel shows remaining time")
    check("85/200" in panel, "panel shows piece counts")
    check(panel.count("\n") >= 8, "panel is a multi-line block",
          f"{panel.count(chr(10)) + 1} lines")

    # A snapshot with no metadata yet must not blow up the formatters.
    empty = Snapshot(
        name="", state="fetching metadata", paused=False, finished=False,
        has_metadata=False, percent=0.0, done_bytes=0, total_bytes=0,
        download_rate=0.0, upload_rate=0.0, average_rate=0.0, eta_seconds=None,
        elapsed_seconds=0.0, peers=0, seeds=0, swarm_seeds=-1, swarm_peers=-1,
        pieces_done=0, pieces_total=0, session_downloaded=0, session_uploaded=0,
        all_time_downloaded=0, all_time_uploaded=0, save_path=".", error="",
    )
    try:
        torrent_dl.compose(empty, FakeDownload(), deque(), True)
        check(True, "panel handles a torrent with no metadata")
    except Exception as exc:
        check(False, "panel handles a torrent with no metadata", repr(exc))

    print()


def test_cli_run() -> None:
    """Run the real CLI against a local seeder, then Ctrl+C it."""
    print("live CLI run")
    workspace = Path(tempfile.mkdtemp(prefix="torrent-cli-"))
    seed_dir = workspace / "seed"
    dest_dir = workspace / "dest"
    seed_dir.mkdir()
    seeder = None
    process = None
    try:
        torrent_path, _ = build_torrent(seed_dir)
        info = lt.torrent_info(str(torrent_path))
        seeder = start_seeder(torrent_path, seed_dir)

        command = [
            sys.executable, "torrent_dl.py", str(torrent_path),
            "-o", str(dest_dir),
            "--port", str(CLI_PORT),
            "--peer", f"127.0.0.1:{SEED_PORT}",
            "--max-down", "512",          # KB/s, so it cannot finish instantly
            "--files",
            "--interval", "0.4",
        ]
        print(f"  $ {' '.join(command[1:])}")

        creation = 0
        if os.name == "nt":
            creation = subprocess.CREATE_NEW_PROCESS_GROUP

        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, text=True,
            cwd=str(Path(__file__).parent), creationflags=creation,
        )
        time.sleep(RUN_SECONDS)

        # Ctrl+C, the way a user stops it.
        if os.name == "nt":
            process.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            process.send_signal(signal.SIGINT)

        try:
            output = process.communicate(timeout=30)[0]
        except subprocess.TimeoutExpired:
            process.kill()
            output = process.communicate()[0]
            check(False, "CLI exits after Ctrl+C")

        print("  --- CLI output (tail) ---")
        for line in output.strip().splitlines()[-10:]:
            print(f"  | {line}")
        print("  -------------------------")

        check("info hash" in output, "CLI prints the info hash")
        check(".resume" in output, "CLI prints the resume-file path")
        check("%" in output, "CLI reports progress")
        check("Interrupted" in output or "Stopped" in output,
              "CLI prints a summary on Ctrl+C")
        check("progress saved" in output or "100.00%" in output,
              "CLI reports that progress was saved")

        state_dir = dest_dir / ".torrent-state"
        resume_files = list(state_dir.glob("*.resume")) if state_dir.is_dir() else []
        check(bool(resume_files), "resume file exists after Ctrl+C",
              resume_files[0].name if resume_files else "none")

        partial = dest_dir / info.name()
        downloaded = partial.stat().st_size if partial.is_file() else 0
        check(downloaded > 0, "data was actually written",
              f"{downloaded} bytes of {info.total_size()}")

        # -- and now the real point: run it again and confirm it continues ---
        before = downloaded
        process = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, text=True,
            cwd=str(Path(__file__).parent), creationflags=creation,
        )
        time.sleep(10)
        if os.name == "nt":
            process.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            process.send_signal(signal.SIGINT)
        second_output = process.communicate(timeout=30)[0]

        check("resuming from saved state" in second_output,
              "second run reports resuming from saved state")
        after = partial.stat().st_size if partial.is_file() else 0
        check(after >= before, "second run kept the data from the first",
              f"{before} -> {after} bytes")
        # Percentages in the second run should start above zero.
        check("0.00%" not in second_output.split("\n")[-6:][0] or after > before,
              "second run continues rather than restarting")
    finally:
        if process is not None and process.poll() is None:
            process.kill()
        if seeder is not None:
            seeder.pause()
            del seeder
        time.sleep(0.5)
        shutil.rmtree(workspace, ignore_errors=True)
    print()


def main() -> int:
    test_renderer()
    test_cli_run()
    if failures:
        print(f"{len(failures)} check(s) FAILED:")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
