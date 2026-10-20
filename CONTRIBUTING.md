# Contributing to FrameFind

Bug reports, small fixes, documentation improvements, and new ideas are welcome. For a larger change, open an issue first so we can agree on the behavior and scope.

## Set up a development environment

Use Python 3.11 or newer, FFmpeg (including `ffprobe`), and Node.js 22 or newer. Node is used to check the browser JavaScript and can also help with YouTube imports. There is no frontend build step.

On macOS, install the system tools with Homebrew:

```sh
brew install python ffmpeg node
```

On Ubuntu/Debian, install Python 3.11+, its `venv` package, and FFmpeg using your package manager; install Node.js 22+ separately if your distribution provides an older version. On Windows, use Ubuntu under WSL2. Native Windows is not currently supported: downloader cancellation uses POSIX process groups.

From your clone:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
```

Run the app with `python start.py`. Enter your own TypeSafe API key at the hidden prompt, then open <http://127.0.0.1:8765>. For UI work without a key, run `python -m uvicorn app.main:app --host 127.0.0.1 --port 8765`; local video preparation works, but searches require a key.

Keep test media outside the repository or in the ignored `data/` folder. To protect your usual library while developing, point `JEV_DATA_DIR` at a separate directory.

## Check a change

```sh
python -m pytest -q
node --check app/static/app.js
```

The test suite uses temporary data, generated video fixtures, and mocked provider responses. It needs no API key and makes no paid API calls. FFmpeg must be installed to run the media tests; otherwise those checks are skipped. CI installs it and runs the suite on Linux with Python 3.11 and 3.14, and macOS with Python 3.14.

For UI changes, also check upload/import, search progress, results, and snippet playback in a browser at both desktop and narrow widths. Real searches send sampled images to TypeSafe and incur API charges; use short videos and your own key when live verification is needed.

## Find your way around

| Path | Responsibility |
| --- | --- |
| `app/main.py` | HTTP routes, background preparation and search jobs, budget guard |
| `app/media.py` | FFmpeg sampling, playback conversion, clip export |
| `app/importer.py` | Public URL and YouTube downloads |
| `app/jev.py` | TypeSafe requests, retries, and concurrency |
| `app/db.py` | SQLite storage, durable match caches, usage accounting |
| `app/search.py` | Match filtering and snippet grouping |
| `app/static/` | HTML, CSS, and browser JavaScript |
| `tests/` | API, media, importer, and provider-contract checks |

FrameFind prepares every tenth decoded frame locally. Each new query scans those frames with Jev, and results use a fixed 95% relevance cutoff. Query matches are cached; there is no upfront AI indexing stage. Changes should preserve existing libraries and budget records unless an explicit migration is included.

## Send a pull request

1. Fork the repository and create a branch for one focused change.
2. Explain the problem, the resulting behavior, and how you checked it. Include screenshots or a short recording when the interface changes.
3. Add a regression test for a meaningful bug fix when it can run without live API access. Documentation and cosmetic changes usually do not need new tests.
4. Run the checks above and open a pull request. Keep keys, personal videos, databases, and local runtimes out of the diff.

Use the issue templates for bugs and feature ideas. See [SECURITY.md](SECURITY.md) for vulnerabilities. Please keep discussion respectful and specific; a reproducible example helps more than a large log dump.
