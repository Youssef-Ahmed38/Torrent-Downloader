# Torrent downloader with pause / resume and live stats

A working BitTorrent client in Python, built on libtorrent. It downloads from a
magnet link or a `.torrent` file, can be paused and continued (including after
the process exits), and shows live download speed, elapsed time, remaining
time and percentage.

## Install

```bash
pip install libtorrent
```

That is the only dependency. Verified with libtorrent 2.1.1 on Python 3.12 (Windows).

## Use

### Set the path in the code

Open [run_download.py](run_download.py), edit the variables at the top, and run
the file — no command line arguments needed:

```python
# What to download: either the path to a .torrent file, or a magnet: link.
TORRENT = r"C:\Games\example.torrent"
# TORRENT = "magnet:?xt=urn:btih:..."

# Where to save the downloaded files.
SAVE_TO = r"C:\Games\downloads"
```

```bash
python run_download.py
```

The other settings there cover the same options as the flags below:
`MAX_DOWN_KB`, `MAX_UP_KB`, `LISTEN_PORT`, `SHOW_FILES`, `SEQUENTIAL`,
`KEEP_SEEDING`, `START_PAUSED`, `REFRESH_SECONDS`, `STATE_DIR` and `PEERS`.
Keep the `r"..."` quoting on Windows paths so the backslashes are taken
literally. If `TORRENT` is wrong you get a plain message saying so rather than
a stack trace.

### Or pass it on the command line

```bash
python torrent_dl.py "magnet:?xt=urn:btih:..." -o downloads
```

```bash
python torrent_dl.py ubuntu.torrent -o downloads --files
```

While it runs:

| key | action |
| --- | --- |
| `p` | pause (progress is written to disk) |
| `r` | resume |
| `space` | toggle pause/resume |
| `f` | show/hide per-file progress |
| `q` | quit, saving progress |

Ctrl+C (and Ctrl+Break on Windows) also saves progress before exiting.

The live panel looks like this:

```
payload.bin  (12.00 MB)
[################----------------------]  42.50%

  status     downloading            peers  7 connected (3 seeds)   swarm 12 seeds / 40 peers
  speed      v     1.50 MB/s   ^      64.00 KB/s   avg 1.40 MB/s
  done       5.10 MB / 12.00 MB        pieces 85/200
  time       elapsed 00:01:35      remaining 00:02:05
  traffic    down 5.10 MB   up 1.00 KB
  saving to  C:\Games\downloads

  [p] pause   [r] resume   [space] toggle   [f] files   [q] quit
```

### Continuing a download

Run the same command again. Progress lives in `<output>/.torrent-state/<infohash>.resume`,
so a download interrupted at 40% continues at 40% instead of starting over:

```
* resuming from saved state
```

Piped output (`> log.txt`) drops the panel and logs one status line every two
seconds instead.

### Options

| option | meaning |
| --- | --- |
| `-o, --output DIR` | where to save the data (default `downloads`) |
| `--state-dir DIR` | where to keep resume files |
| `--max-down KB` / `--max-up KB` | rate caps in KB/s (`0` = unlimited) |
| `--paused` | add the torrent without starting it |
| `--sequential` | download pieces in order, for streaming media |
| `--seed` | keep seeding after finishing |
| `--files` | show per-file progress |
| `--peer HOST:PORT` | dial a known peer directly (repeatable) |
| `--port N` | listen port (default 6881) |
| `--interval S` | refresh interval (default 0.5s) |
| `--restart` | discard saved state and re-verify files on disk |

`--restart` never deletes downloaded data; it forces a re-check of what is
already there.

## Using it as a library

`torrent_engine.py` has no printing in it and can drive any UI:

```python
from torrent_engine import TorrentDownload

with TorrentDownload("my.torrent", save_path="downloads").start() as download:
    while True:
        snap = download.poll()          # call a few times a second
        print(f"{snap.percent:.1f}%  {snap.download_rate / 1024:.0f} KB/s"
              f"  eta {snap.eta_seconds}")
        if snap.finished:
            break
        # download.pause() / download.resume() / download.toggle()
```

`poll()` returns a `Snapshot` with `percent`, `done_bytes`, `total_bytes`,
`download_rate`, `upload_rate`, `average_rate`, `eta_seconds`,
`elapsed_seconds`, `peers`, `seeds`, `pieces_done`, `pieces_total`, `state`,
`paused`, `finished` and `error`. `files()` gives per-file progress, and
`drain_events()` returns notices such as `metadata received`.

Pausing and `close()` write the resume file, so the next run continues.

## Notes on the tricky parts

Three things in libtorrent cause most of the bugs in code like this, and are
handled here:

* **`default_flags` contains both `paused` and `auto_managed`.** Clearing only
  `auto_managed` leaves a torrent paused forever; a paused torrent still listens
  but refuses handshakes, so it looks like a mysterious connection failure.
  The engine sets both flags explicitly, which is also what makes `p`/`r` exact
  rather than a suggestion the queue manager may override.
* **`status.state` ignores pausing.** A paused torrent still reports
  `downloading`, so the engine derives "paused" from the flags instead.
* **Rate limits skip local peers by default.** `--max-down` therefore appears
  to do nothing on a LAN, so the CLI turns off `ignore_limits_on_local_network`
  whenever a cap is requested.

Reported speed is zeroed while paused (libtorrent's own figure is a trailing
one-second average and lingers), and an ETA is only shown once the rate is
above 1 KB/s, so the display doesn't flash nonsense like `13d 05:58:21` in the
first seconds.

## Tests

Both suites run offline, with no tracker and no internet: they build a real
torrent, seed it from one libtorrent session and download it from another over
loopback.

```bash
python test_local_swarm.py
```

Checks that progress, speed, ETA and elapsed time move; that pause really stops
the transfer; that resume data reaches disk; that **destroying the downloader and
building a new one continues from the saved progress**; and that the finished
file is byte-identical to the original (SHA-256).

```bash
python test_cli.py
```

Checks the formatters and the panel renderer, then runs the actual CLI as a
subprocess against a local seeder, interrupts it with Ctrl+C, and confirms it
prints a summary, saves progress, and continues on the next run.

Use this with content you have the right to download.
