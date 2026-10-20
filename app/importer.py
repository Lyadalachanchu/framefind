"""Bounded public video URL imports; local and private network URLs are rejected."""
from __future__ import annotations

import asyncio
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import sys
import tempfile
import time
from typing import Callable
from urllib.parse import parse_qs, unquote, urljoin, urlsplit, urlunsplit
from uuid import uuid4

import httpx

MAX_BYTES = 2 * 1024 ** 3
MAX_SECONDS = 60 * 60
DOWNLOAD_TIMEOUT = 15 * 60
YOUTUBE_STARTUP_TIMEOUT = 30
PROCESS_STOP_TIMEOUT = 3
MAX_REDIRECTS = 5
VIDEO_EXTENSIONS = {'.mp4', '.webm', '.mov', '.mkv', '.m4v', '.avi', '.mpeg', '.mpg', '.ogv', '.ts'}
YOUTUBE_HOSTS = {'youtube.com', 'www.youtube.com', 'm.youtube.com', 'music.youtube.com', 'youtu.be', 'www.youtu.be'}
Progress = Callable[[str], None] | None
_BACKGROUND_CLEANUPS: set[asyncio.Task] = set()


class VideoImportError(ValueError):
    """An import failure safe to show in the local web app."""


def _parse_url(url: str):
    if not isinstance(url, str) or not url or len(url) > 8192:
        raise VideoImportError('Enter a public HTTP or HTTPS video URL.')
    if any(ord(char) < 33 for char in url) or '\\' in url:
        raise VideoImportError('The video URL contains invalid characters.')
    try:
        parsed = urlsplit(url)
        port = parsed.port
        if parsed.scheme.lower() not in {'http', 'https'} or not parsed.hostname:
            raise ValueError
        if parsed.username is not None or parsed.password is not None:
            raise VideoImportError('URLs containing usernames or passwords are not supported.')
        if port is not None and not 1 <= port <= 65535:
            raise ValueError
        hostname = parsed.hostname.encode('idna').decode('ascii').lower().rstrip('.')
        if not hostname or '%' in hostname:
            raise ValueError
    except (ValueError, UnicodeError) as exc:
        if isinstance(exc, VideoImportError):
            raise
        raise VideoImportError('Enter a valid public HTTP or HTTPS video URL.') from None
    if hostname in {'localhost', 'localhost.localdomain'} or hostname.endswith(('.localhost', '.local', '.internal')):
        raise VideoImportError('Local and private network URLs are not supported. Upload the file instead.')
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        if not address.is_global:
            raise VideoImportError('Local and private network URLs are not supported. Upload the file instead.')
    return parsed, hostname


async def _resolve_public_url(url: str) -> tuple[str, str, str]:
    """Return canonical URL, host, and one verified IP (used directly to prevent rebinding)."""
    parsed, hostname = _parse_url(url)
    port = parsed.port or (443 if parsed.scheme == 'https' else 80)
    try:
        records = await asyncio.wait_for(
            asyncio.get_running_loop().getaddrinfo(hostname, port, type=socket.SOCK_STREAM),
            timeout=10,
        )
    except (OSError, TimeoutError):
        raise VideoImportError('The video host could not be resolved. Check the URL and try again.') from None
    addresses = list(dict.fromkeys(record[4][0] for record in records))
    if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise VideoImportError('Local and private network URLs are not supported. Upload the file instead.')
    # Prefer IPv4 where available, while still permitting public IPv6-only hosts.
    addresses.sort(key=lambda address: ':' in address)
    return urlunsplit(parsed._replace(fragment='')), hostname, addresses[0]


def _youtube_url(url: str) -> str | None:
    parsed, hostname = _parse_url(url)
    if hostname not in YOUTUBE_HOSTS:
        return None
    if parsed.port not in {None, 80, 443}:
        raise VideoImportError('Use a standard public YouTube video URL.')
    parts = parsed.path.strip('/').split('/')
    if hostname.endswith('youtu.be'):
        video_id = parts[0] if len(parts) == 1 else ''
    elif parsed.path == '/watch':
        video_id = parse_qs(parsed.query).get('v', [''])[0]
    elif len(parts) == 2 and parts[0] in {'shorts', 'live', 'embed'}:
        video_id = parts[1]
    else:
        video_id = ''
    if not re.fullmatch(r'[A-Za-z0-9_-]{11}', video_id):
        raise VideoImportError('Use a public YouTube video link, rather than a channel or playlist.')
    # Rebuild the input so no arbitrary extractor target, redirect, or playlist reaches yt-dlp.
    return f'https://www.youtube.com/watch?v={video_id}'


def _title(value: str, fallback: str = 'Imported video') -> str:
    return re.sub(r'\s+', ' ', ''.join(c for c in value if ord(c) >= 32)).strip()[:180] or fallback


async def _download_direct(url: str, directory: Path, progress: Progress) -> tuple[Path, str]:
    async with httpx.AsyncClient(
        follow_redirects=False, trust_env=False,
        timeout=httpx.Timeout(45, connect=15),
        headers={'User-Agent': 'JevVideoSearch/1.0', 'Accept-Encoding': 'identity'},
    ) as client:
        for redirect in range(MAX_REDIRECTS + 1):
            canonical, hostname, address = await _resolve_public_url(url)
            parsed = urlsplit(canonical)
            authority = f'[{address}]' if ':' in address else address
            if parsed.port:
                authority += f':{parsed.port}'
            destination = urlunsplit(parsed._replace(netloc=authority))
            # Connection uses the validated IP; TLS still verifies the original hostname.
            host_header = f'[{hostname}]' if ':' in hostname else hostname
            if parsed.port:
                host_header += f':{parsed.port}'
            async with client.stream(
                'GET', destination, headers={'Host': host_header},
                extensions={'sni_hostname': hostname},
            ) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    if redirect == MAX_REDIRECTS or not response.headers.get('location'):
                        raise VideoImportError('The video URL redirects too many times or has an invalid redirect.')
                    url = urljoin(canonical, response.headers['location'])
                    continue
                if response.status_code in {401, 403}:
                    raise VideoImportError('This video requires permission or blocks downloads. Upload a local copy instead.')
                if response.status_code >= 400:
                    raise VideoImportError(f'The video server returned HTTP {response.status_code}. Check the URL.')
                if response.status_code != 200:
                    raise VideoImportError('The video server did not return a complete downloadable file.')
                try:
                    size = int(response.headers.get('content-length', '0'))
                except ValueError:
                    size = 0
                if size > MAX_BYTES:
                    raise VideoImportError('Videos must be 2 GB or smaller.')
                mime = response.headers.get('content-type', '').split(';')[0].lower().strip()
                name = _title(unquote(parsed.path.rsplit('/', 1)[-1]), 'Imported video.mp4')
                extension = Path(name).suffix.lower()
                if mime in {'text/html', 'application/xhtml+xml', 'application/json'}:
                    raise VideoImportError('This link is a webpage. Use a direct video file URL or a public YouTube video link.')
                if extension not in VIDEO_EXTENSIONS:
                    if not mime.startswith('video/') and mime not in {'application/octet-stream', 'application/mp4'}:
                        raise VideoImportError('The URL does not point to a downloadable video file.')
                    extension = {'video/webm': '.webm', 'video/quicktime': '.mov', 'video/x-matroska': '.mkv'}.get(mime, '.mp4')
                path = directory / f'source{extension}'
                count, reported = 0, 0
                with path.open('wb') as output:
                    async for chunk in response.aiter_bytes(256 * 1024):
                        count += len(chunk)
                        if count > MAX_BYTES:
                            raise VideoImportError('Videos must be 2 GB or smaller.')
                        output.write(chunk)
                        if progress and count - reported >= 8 * 1024 ** 2:
                            progress(f'Downloading video · {count // 1024 ** 2} MB')
                            reported = count
                if not count:
                    raise VideoImportError('The video server returned an empty file.')
                return path, name
    raise VideoImportError('The video download could not be completed.')


async def _stop_process(process: asyncio.subprocess.Process) -> None:
    # An OS process can remain uninterruptible even after SIGKILL. The UI must still
    # leave the importing state rather than waiting forever for that process.
    for sig in (signal.SIGTERM, signal.SIGKILL):
        if process.returncode is not None:
            return
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(process.wait(), timeout=PROCESS_STOP_TIMEOUT)
            return
        except TimeoutError:
            continue


def _youtube_command() -> list[str]:
    # The launcher installs this runtime outside iCloud. Never execute a bundled
    # binary under Documents, where launching may wait for filesystem hydration.
    return [sys.executable, '-m', 'yt_dlp']


def _cleanup_late_process(spawn: asyncio.Task) -> None:
    """A cancelled/timed-out import still owns any process launched afterwards."""
    _BACKGROUND_CLEANUPS.discard(spawn)
    if spawn.cancelled():
        return
    try:
        process = spawn.result()
    except Exception:
        return
    cleanup = asyncio.create_task(_stop_process(process))
    _BACKGROUND_CLEANUPS.add(cleanup)
    cleanup.add_done_callback(_finish_background_cleanup)


def _finish_background_cleanup(task: asyncio.Task) -> None:
    _BACKGROUND_CLEANUPS.discard(task)
    if not task.cancelled():
        task.exception()


async def _start_youtube_process(args: list[str], deadline: float) -> asyncio.subprocess.Process:
    spawn = asyncio.create_task(asyncio.create_subprocess_exec(
        *args, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True,
    ))
    try:
        # Shield the launch so cancellation cannot lose a process created just as
        # the timeout expires; the completion callback reaps it if it arrives late.
        return await asyncio.wait_for(asyncio.shield(spawn), timeout=max(0, deadline - time.monotonic()))
    except (TimeoutError, asyncio.CancelledError) as exc:
        _BACKGROUND_CLEANUPS.add(spawn)
        spawn.add_done_callback(_cleanup_late_process)
        if isinstance(exc, asyncio.CancelledError):
            raise
        raise VideoImportError('The YouTube downloader did not start in time. Retry the import or upload a local copy.') from None


def _youtube_progress(line: str) -> tuple[str, str] | None:
    """Turn downloader output into safe UI text without exposing paths or raw errors."""
    line = line.strip()
    if line.startswith('FRAMEFIND_PROGRESS:'):
        try:
            data = json.loads(line.partition(':')[2])
        except (ValueError, TypeError):
            return None
        if not isinstance(data, dict):
            return None

        def number(key):
            value = data.get(key)
            return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0 else None

        downloaded = number('downloaded_bytes')
        total = number('total_bytes') or number('total_bytes_estimate')
        speed, eta = number('speed'), number('eta')
        parts = ['Downloading YouTube video']
        if downloaded is not None:
            if total:
                parts += [f'{min(100, downloaded / total * 100):.0f}%', f'{downloaded / 1024 ** 2:.1f} / {total / 1024 ** 2:.1f} MiB']
            else:
                parts.append(f'{downloaded / 1024 ** 2:.1f} MiB')
        if speed:
            parts.append(f'{speed / 1024 ** 2:.1f} MiB/s')
        if eta is not None:
            seconds = int(eta)
            parts.append(f'{seconds // 60}:{seconds % 60:02d} left')
        if data.get('status') == 'finished':
            return 'stream-finished', 'Downloaded YouTube stream · preparing video'
        if data.get('status') == 'downloading':
            return 'download', ' · '.join(parts)
        return None
    if line.startswith('FRAMEFIND_POSTPROCESS:'):
        try:
            data = json.loads(line.partition(':')[2])
        except (ValueError, TypeError):
            return None
        if not isinstance(data, dict):
            return None
        if data.get('postprocessor') == 'Merger' and data.get('status') != 'finished':
            return 'merge', 'Merging YouTube video and audio'
        return 'prepare', 'Preparing downloaded video'
    if line.startswith(('[youtube]', '[youtube:')):
        return 'check', 'Checking YouTube video and available formats'
    if line.startswith('[info]'):
        return 'connect', 'Connecting to YouTube video stream'
    if line.startswith('[download]'):
        if 'retry' in line.lower() or 'error' in line.lower():
            return 'retry', 'YouTube connection interrupted · retrying download'
        return 'download', 'Downloading YouTube video'
    if line.startswith('[Merger]'):
        return 'merge', 'Merging YouTube video and audio'
    return None


class _YouTubeProgress:
    def __init__(self, callback: Progress):
        self.callback = callback
        self.phase = None
        self.message = None
        self.reported = float('-inf')

    def read_line(self, line: bytes) -> None:
        update = _youtube_progress(line.decode(errors='replace'))
        if not update or not self.callback:
            return
        phase, message = update
        now = time.monotonic()
        # Stage changes are immediate; byte counters cause at most one DB update per second.
        if message != self.message and (phase != self.phase or now - self.reported >= 1):
            self.callback(message)
            self.phase, self.message, self.reported = phase, message, now


async def _read_youtube_output(stream: asyncio.StreamReader, reporter: _YouTubeProgress, started: asyncio.Event) -> bytes:
    tail, pending = b'', b''
    while chunk := await stream.read(4096):
        started.set()
        tail = (tail + chunk)[-8192:]
        pending += chunk
        while b'\n' in pending:
            line, pending = pending.split(b'\n', 1)
            reporter.read_line(line)
        pending = pending[-16384:]
    if pending:
        reporter.read_line(pending)
    return tail


async def _monitor_youtube_startup(started: asyncio.Event, deadline: float) -> None:
    try:
        await asyncio.wait_for(started.wait(), timeout=max(0, deadline - time.monotonic()))
    except TimeoutError:
        raise VideoImportError('The YouTube downloader did not start in time. Retry the import or upload a local copy.') from None


async def _monitor_size(directory: Path, process: asyncio.subprocess.Process) -> None:
    while process.returncode is None:
        total = 0
        for path in directory.iterdir():
            try:
                if path.is_file():
                    total += path.stat().st_size
            except FileNotFoundError:
                pass
        if total > MAX_BYTES:
            raise VideoImportError('Videos must be 2 GB or smaller.')
        await asyncio.sleep(0.5)


async def _download_youtube(url: str, directory: Path, progress: Progress) -> tuple[Path, str]:
    await _resolve_public_url(url)
    if progress:
        progress('Starting YouTube downloader')
    deadline = time.monotonic() + YOUTUBE_STARTUP_TIMEOUT
    args = _youtube_command() + (['--js-runtimes', 'node'] if shutil.which('node') else []) + [
        '--ignore-config', '--no-plugin-dirs',
        '--no-cache-dir', '--no-cookies', '--no-cookies-from-browser',
        '--proxy', '', '--use-extractors', 'youtube', '--no-playlist',
        '--no-quiet', '--no-colors', '--newline', '--progress', '--progress-delta', '1',
        '--progress-template', 'download:FRAMEFIND_PROGRESS:%(progress)j',
        '--progress-template', 'postprocess:FRAMEFIND_POSTPROCESS:%(progress)j',
        '--socket-timeout', '30',
        '--retries', '2', '--fragment-retries', '2', '--abort-on-unavailable-fragments',
        '--max-filesize', str(MAX_BYTES), '--match-filters', f'duration <= {MAX_SECONDS} & !is_live',
        '--format', 'best[height<=720]/bestvideo[height<=720]+bestaudio/best',
        '--merge-output-format', 'mp4', '--print', 'after_move:%(title)j',
        '--output', str(directory / 'source.%(ext)s'), '--', url,
    ]
    process = await _start_youtube_process(args, deadline)
    reporter, started = _YouTubeProgress(progress), asyncio.Event()
    stdout = asyncio.create_task(_read_youtube_output(process.stdout, reporter, started))
    stderr = asyncio.create_task(_read_youtube_output(process.stderr, reporter, started))
    startup = asyncio.create_task(_monitor_youtube_startup(started, deadline))
    monitor = asyncio.create_task(_monitor_size(directory, process))
    finished = asyncio.create_task(process.wait())
    try:
        pending = {startup, monitor, finished}
        while finished in pending:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                await task
        await finished
        raw_title, errors = await asyncio.gather(stdout, stderr)
        if process.returncode:
            detail = errors.decode(errors='replace').lower()
            if 'no module named yt_dlp' in detail:
                raise VideoImportError('YouTube import requires yt-dlp. Install the app requirements and try again.')
            if any(term in detail for term in ('sign in', 'login', 'private video', 'age-restricted', 'cookies')):
                raise VideoImportError('YouTube requires sign-in for this video. Upload a local copy instead.')
            raise VideoImportError('YouTube could not download this video. It may be unavailable or blocking downloads; try uploading a local copy.')
        videos = [path for path in directory.iterdir() if path.suffix.lower() in VIDEO_EXTENSIONS]
        if len(videos) != 1 or not videos[0].stat().st_size:
            raise VideoImportError('No video was downloaded. YouTube imports must be public, finished videos under 60 minutes and 2 GB.')
        if videos[0].stat().st_size > MAX_BYTES:
            raise VideoImportError('Videos must be 2 GB or smaller.')
        title = 'YouTube video'
        for line in raw_title.decode(errors='replace').splitlines():
            try:
                candidate = json.loads(line)
                if isinstance(candidate, str):
                    title = _title(candidate, title)
            except (ValueError, TypeError):
                pass
        return videos[0], title
    finally:
        await _stop_process(process)
        for task in (startup, monitor, finished, stdout, stderr):
            if not task.done():
                task.cancel()
        await asyncio.gather(startup, monitor, finished, stdout, stderr, return_exceptions=True)


async def import_video(url: str, directory: Path, progress: Progress = None) -> tuple[Path, str]:
    """Download a direct public video or a YouTube video, cleaning up on failure/cancellation."""
    youtube = _youtube_url(url)
    directory.mkdir(parents=True, exist_ok=True)
    if progress:
        progress('Checking video URL')
    try:
        with tempfile.TemporaryDirectory(prefix='.import-', dir=directory) as temporary:
            staging = Path(temporary)
            async with asyncio.timeout(DOWNLOAD_TIMEOUT):
                path, name = await (_download_youtube(youtube, staging, progress) if youtube
                                    else _download_direct(url, staging, progress))
                target = directory / f'import-{uuid4().hex}{path.suffix}'
                path.replace(target)
                return target, name
    except VideoImportError:
        raise
    except TimeoutError:
        raise VideoImportError('The video download timed out. Try again or upload a local copy.') from None
    except httpx.HTTPError:
        raise VideoImportError('The video download failed because of a network or server error. Try again or upload a local copy.') from None
    except OSError:
        raise VideoImportError('The video could not be saved. Check free disk space and try again.') from None
