#!/bin/zsh
set -e
cd "${0:A:h}"
if ! command -v ffmpeg >/dev/null || ! command -v ffprobe >/dev/null; then
  print 'FFmpeg is required. Install it with: brew install ffmpeg'
  exit 1
fi
if [[ "$OSTYPE" == darwin* ]]; then
  framefind_venv="$HOME/Library/Application Support/FrameFind/venv"
  export PYTHONPYCACHEPREFIX="$HOME/Library/Application Support/FrameFind/pycache"
else
  framefind_venv="$PWD/.venv"
fi
framefind_python="$framefind_venv/bin/python"
if [[ ! -x "$framefind_python" ]]; then
  python3 -m venv "$framefind_venv"
fi
if ! "$framefind_python" -c 'import fastapi, uvicorn, httpx, multipart, yt_dlp' >/dev/null 2>&1; then
  "$framefind_python" -m pip install -r requirements.txt
fi
print "FrameFind will be available at http://127.0.0.1:${PORT:-8765}"
exec "$framefind_python" start.py
