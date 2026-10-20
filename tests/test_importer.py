"""Network-free tests for import boundaries, redirected requests, limits and cleanup."""
import asyncio
from contextlib import contextmanager
import json
from pathlib import Path
import signal
import socket
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from urllib.parse import urlsplit

import httpx

from app import importer


async def public_resolution(url):
    _, hostname = importer._parse_url(url)
    return url, hostname, '93.184.216.34'


@contextmanager
def mock_http(handler):
    client_class = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    with patch.object(importer.httpx, 'AsyncClient', side_effect=lambda **kwargs: client_class(transport=transport, **kwargs)):
        yield


class ImporterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    async def test_rejects_local_private_credentialed_and_non_http_urls(self):
        urls = [
            'file:///tmp/movie.mp4', 'ftp://example.com/video.mp4',
            'http://localhost/video.mp4', 'http://foo.local/video.mp4',
            'http://127.0.0.1/video.mp4', 'http://10.1.2.3/video.mp4',
            'http://169.254.169.254/latest/meta-data/', 'http://192.168.1.1/a.mp4',
            'http://[::1]/video.mp4', 'http://[fe80::1]/video.mp4',
            'https://username:password@example.com/video.mp4',
            'https://example.com:0/video.mp4', 'https://example.com\\@localhost/video.mp4',
        ]
        with patch.object(importer, '_download_direct', new_callable=AsyncMock) as download:
            for url in urls:
                with self.subTest(url=url), self.assertRaises(importer.VideoImportError):
                    await importer.import_video(url, self.directory)
            download.assert_not_called()

    async def test_dns_rejects_any_private_address_in_answer(self):
        records = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 443)),
        ]
        with patch.object(asyncio.get_running_loop(), 'getaddrinfo', AsyncMock(return_value=records)):
            with self.assertRaisesRegex(importer.VideoImportError, 'private network'):
                await importer._resolve_public_url('https://example.com/movie.mp4')

    async def test_public_dns_is_resolved_once_and_connection_is_pinned(self):
        records = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 443))]
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(200, headers={'Content-Type': 'video/mp4'}, content=b'fake-video')

        with patch.object(asyncio.get_running_loop(), 'getaddrinfo', AsyncMock(return_value=records)) as resolver, mock_http(handler):
            path, name = await importer.import_video('https://example.com/summer.mp4', self.directory)
        self.assertEqual(path.read_bytes(), b'fake-video')
        self.assertEqual(name, 'summer.mp4')
        resolver.assert_awaited_once()
        self.assertEqual(requests[0].url.host, '93.184.216.34')
        self.assertEqual(requests[0].headers['Host'], 'example.com')
        self.assertEqual(requests[0].extensions['sni_hostname'], 'example.com')
        self.assertEqual(list(self.directory.iterdir()), [path])

    async def test_redirect_to_private_network_is_rejected_before_second_request(self):
        requested = []

        def handler(request):
            requested.append(request)
            return httpx.Response(302, headers={'Location': 'http://127.0.0.1/private.mp4'})

        with patch.object(importer, '_resolve_public_url', side_effect=public_resolution) as resolver, mock_http(handler):
            with self.assertRaisesRegex(importer.VideoImportError, 'private network'):
                await importer.import_video('https://example.com/video.mp4', self.directory)
        self.assertEqual(resolver.await_count, 2)
        self.assertEqual(len(requested), 1)
        self.assertEqual(list(self.directory.iterdir()), [])

    async def test_relative_redirect_resolves_against_original_hostname(self):
        requested = []

        def handler(request):
            requested.append(request)
            if request.url.path == '/start':
                return httpx.Response(302, headers={'Location': '/movie.mp4'})
            return httpx.Response(200, headers={'Content-Type': 'video/mp4'}, content=b'video')

        with patch.object(importer, '_resolve_public_url', side_effect=public_resolution) as resolver, mock_http(handler):
            path, _ = await importer.import_video('https://example.com/start', self.directory)
        self.assertEqual(resolver.await_args_list[1].args[0], 'https://example.com/movie.mp4')
        self.assertEqual(path.read_bytes(), b'video')
        self.assertEqual([req.headers['Host'] for req in requested], ['example.com', 'example.com'])

    async def test_over_limit_header_and_stream_both_leave_no_partial_files(self):
        for headers, content in [
            ({'Content-Type': 'video/mp4', 'Content-Length': '100'}, b''),
            ({'Content-Type': 'video/mp4', 'Content-Length': '1'}, b'123456'),
        ]:
            with self.subTest(headers=headers), patch.object(importer, 'MAX_BYTES', 4), \
                    patch.object(importer, '_resolve_public_url', side_effect=public_resolution), \
                    mock_http(lambda request: httpx.Response(200, headers=headers, content=content)):
                with self.assertRaisesRegex(importer.VideoImportError, '2 GB'):
                    await importer.import_video('https://example.com/a.mp4', self.directory)
            self.assertEqual(list(self.directory.iterdir()), [])

    async def test_webpage_does_not_become_a_video(self):
        with patch.object(importer, '_resolve_public_url', side_effect=public_resolution), \
                mock_http(lambda request: httpx.Response(200, headers={'Content-Type': 'text/html'}, content=b'<html>')):
            with self.assertRaisesRegex(importer.VideoImportError, 'webpage'):
                await importer.import_video('https://example.com/fake.mp4', self.directory)
        self.assertEqual(list(self.directory.iterdir()), [])

    async def test_cancellation_cleans_staging(self):
        async def interrupted(url, directory, progress):
            (directory / 'partial.mp4').write_bytes(b'partial')
            raise asyncio.CancelledError

        with patch.object(importer, '_download_direct', side_effect=interrupted):
            with self.assertRaises(asyncio.CancelledError):
                await importer.import_video('https://example.com/a.mp4', self.directory)
        self.assertEqual(list(self.directory.iterdir()), [])

    async def test_timeout_has_safe_message_and_cleans_staging(self):
        async def timed_out(url, directory, progress):
            (directory / 'partial.mp4').write_bytes(b'partial')
            raise TimeoutError

        with patch.object(importer, '_download_direct', side_effect=timed_out):
            with self.assertRaisesRegex(importer.VideoImportError, 'timed out'):
                await importer.import_video('https://example.com/a.mp4', self.directory)
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_youtube_normalizes_video_links_and_rejects_nonvideo_paths(self):
        expected = 'https://www.youtube.com/watch?v=dQw4w9WgXcQ'
        for url in ['https://youtu.be/dQw4w9WgXcQ?t=42', 'https://youtube.com/watch?v=dQw4w9WgXcQ&list=ignored',
                    'https://www.youtube.com/shorts/dQw4w9WgXcQ']:
            self.assertEqual(importer._youtube_url(url), expected)
        for url in ['https://youtube.com/redirect?q=http://127.0.0.1/', 'https://youtube.com/playlist?list=123',
                    'https://youtube.com/watch?v=invalid']:
            with self.assertRaises(importer.VideoImportError):
                importer._youtube_url(url)
        self.assertIsNone(importer._youtube_url('https://youtube.com.attacker.example/video.mp4'))

    async def test_youtube_invocation_is_isolated_and_success_returns_title(self):
        calls = []
        progress = []

        async def create_process(*args, **kwargs):
            calls.append((args, kwargs))
            output = Path(args[args.index('--output') + 1].replace('%(ext)s', 'mp4'))
            output.write_bytes(b'fake-youtube-video')

            class Process:
                returncode = None
                stdout = asyncio.StreamReader()
                stderr = asyncio.StreamReader()

                async def wait(self):
                    self.returncode = 0
                    return 0

            process = Process()
            process.stdout.feed_data(b'[youtube] Extracting URL: https://www.youtube.com/watch?v=dQw4w9WgXcQ\n"A public video"\n')
            process.stdout.feed_eof()
            process.stderr.feed_data(b'FRAMEFIND_PROGRESS:{"status":"downloading","downloaded_bytes":1048576,"total_bytes":2097152,"speed":524288,"eta":2}\n')
            process.stderr.feed_data(b'FRAMEFIND_POSTPROCESS:{"status":"started","postprocessor":"Merger"}\n')
            process.stderr.feed_eof()
            return process

        with patch.object(importer, '_resolve_public_url', side_effect=public_resolution), \
                patch.object(importer.asyncio, 'create_subprocess_exec', side_effect=create_process):
            path, title = await importer.import_video('https://youtu.be/dQw4w9WgXcQ', self.directory, progress.append)
        self.assertEqual(title, 'A public video')
        self.assertEqual(path.read_bytes(), b'fake-youtube-video')
        args, kwargs = calls[0]
        self.assertIn('--ignore-config', args)
        self.assertIn('--no-cookies-from-browser', args)
        self.assertIn('--no-quiet', args)
        self.assertIn('--newline', args)
        self.assertIn('--progress', args)
        self.assertNotIn('--no-progress', args)
        self.assertIn('Starting YouTube downloader', progress)
        self.assertIn('Checking YouTube video and available formats', progress)
        self.assertIn('Downloading YouTube video · 50% · 1.0 / 2.0 MiB · 0.5 MiB/s · 0:02 left', progress)
        self.assertIn('Merging YouTube video and audio', progress)
        self.assertEqual(args[args.index('--use-extractors') + 1], 'youtube')
        self.assertIn('duration <= 3600 & !is_live', args)
        self.assertTrue(kwargs['start_new_session'])
        self.assertEqual(urlsplit(args[-1]).hostname, 'www.youtube.com')

    def test_youtube_command_uses_current_runtime_on_all_platforms(self):
        for platform in ('darwin', 'linux', 'win32'):
            with self.subTest(platform=platform), patch.object(importer.sys, 'platform', platform), \
                    patch.object(importer.sys, 'executable', '/local-runtime/python'):
                self.assertEqual(importer._youtube_command(), ['/local-runtime/python', '-m', 'yt_dlp'])

    def test_youtube_progress_accepts_estimates_and_missing_fields(self):
        def parse(data):
            return importer._youtube_progress('FRAMEFIND_PROGRESS:' + json.dumps(data))

        phase, message = parse({'status': 'downloading', 'downloaded_bytes': 2 ** 20, 'total_bytes_estimate': 4 * 2 ** 20})
        self.assertEqual(phase, 'download')
        self.assertIn('25%', message)
        self.assertNotIn('left', message)
        self.assertEqual(parse({'status': 'downloading', 'downloaded_bytes': 0}), ('download', 'Downloading YouTube video · 0.0 MiB'))
        self.assertEqual(parse({'status': 'downloading', 'downloaded_bytes': 'NA', 'speed': float('inf'), 'eta': -1}), ('download', 'Downloading YouTube video'))
        self.assertEqual(parse({'status': 'finished'})[0], 'stream-finished')
        for line in ['FRAMEFIND_PROGRESS:broken', 'FRAMEFIND_PROGRESS:[]', 'FRAMEFIND_POSTPROCESS:null', 'ERROR: /private/path with details']:
            self.assertIsNone(importer._youtube_progress(line))

    def test_youtube_progress_throttles_byte_updates_but_reports_phase_changes(self):
        messages = []
        reporter = importer._YouTubeProgress(messages.append)

        def download(size):
            return ('FRAMEFIND_PROGRESS:' + json.dumps({'status': 'downloading', 'downloaded_bytes': size})).encode()

        with patch.object(importer.time, 'monotonic', side_effect=[10, 10.2, 11.1, 11.2, 11.3]):
            reporter.read_line(download(2 ** 20))
            reporter.read_line(download(2 * 2 ** 20))
            reporter.read_line(download(3 * 2 ** 20))
            reporter.read_line(b'[Merger] Merging formats into /private/path')
            reporter.read_line(b'[Merger] Merging formats into /private/path')
        self.assertEqual(messages, ['Downloading YouTube video · 1.0 MiB', 'Downloading YouTube video · 3.0 MiB', 'Merging YouTube video and audio'])

    async def test_youtube_output_handles_split_lines_and_bounds_retained_output(self):
        stream, started, messages = asyncio.StreamReader(), asyncio.Event(), []
        reading = asyncio.create_task(importer._read_youtube_output(stream, importer._YouTubeProgress(messages.append), started))
        stream.feed_data(b'x' * 40000 + b'\nFRAMEFIND_PRO')
        await asyncio.sleep(0)
        stream.feed_data(b'GRESS:{"status":"downloading","downloaded_bytes":1048576}\n"Title"\n')
        stream.feed_eof()
        tail = await reading
        self.assertTrue(started.is_set())
        self.assertEqual(len(tail), 8192)
        self.assertTrue(tail.endswith(b'"Title"\n'))
        self.assertEqual(messages, ['Downloading YouTube video · 1.0 MiB'])

    async def test_youtube_startup_stall_stops_process_and_cleans_import(self):
        stopped = asyncio.Event()

        class Process:
            returncode = None
            stdout = asyncio.StreamReader()
            stderr = asyncio.StreamReader()

            async def wait(self):
                await stopped.wait()
                self.returncode = -15
                return self.returncode

        async def stop(process):
            stopped.set()

        with patch.object(importer, '_resolve_public_url', side_effect=public_resolution), \
                patch.object(importer.asyncio, 'create_subprocess_exec', AsyncMock(return_value=Process())), \
                patch.object(importer, '_stop_process', AsyncMock(side_effect=stop)) as stop_process, \
                patch.object(importer, 'YOUTUBE_STARTUP_TIMEOUT', 0.01):
            with self.assertRaisesRegex(importer.VideoImportError, 'did not start'):
                await importer.import_video('https://youtu.be/dQw4w9WgXcQ', self.directory)
        stop_process.assert_awaited_once()
        self.assertEqual(list(self.directory.iterdir()), [])

    async def test_launch_timeout_returns_before_launch_and_stops_late_process(self):
        launched, stopped = asyncio.Event(), asyncio.Event()
        process = object()

        async def spawn(*args, **kwargs):
            await launched.wait()
            return process

        async def stop(actual):
            self.assertIs(actual, process)
            stopped.set()

        with patch.object(importer, '_resolve_public_url', side_effect=public_resolution), \
                patch.object(importer.asyncio, 'create_subprocess_exec', side_effect=spawn), \
                patch.object(importer, '_stop_process', side_effect=stop), \
                patch.object(importer, 'YOUTUBE_STARTUP_TIMEOUT', 0.01):
            with self.assertRaisesRegex(importer.VideoImportError, 'did not start'):
                await asyncio.wait_for(importer.import_video('https://youtu.be/dQw4w9WgXcQ', self.directory), 0.2)
            self.assertFalse(stopped.is_set())
            self.assertEqual(list(self.directory.iterdir()), [])
            launched.set()
            await asyncio.wait_for(stopped.wait(), 0.2)

    async def test_cancel_during_launch_stops_process_that_arrives_later(self):
        launching, launched, stopped = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def spawn(*args, **kwargs):
            launching.set()
            await launched.wait()
            return object()

        async def stop(process):
            stopped.set()

        with patch.object(importer, '_resolve_public_url', side_effect=public_resolution), \
                patch.object(importer.asyncio, 'create_subprocess_exec', side_effect=spawn), \
                patch.object(importer, '_stop_process', side_effect=stop):
            importing = asyncio.create_task(importer.import_video('https://youtu.be/dQw4w9WgXcQ', self.directory))
            await asyncio.wait_for(launching.wait(), 0.2)
            importing.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await importing
            self.assertEqual(list(self.directory.iterdir()), [])
            launched.set()
            await asyncio.wait_for(stopped.wait(), 0.2)

    async def test_stalled_kill_cannot_keep_import_in_starting_state(self):
        class Process:
            pid = 12345
            returncode = None
            stdout = asyncio.StreamReader()
            stderr = asyncio.StreamReader()

            async def wait(self):
                await asyncio.Event().wait()

        with patch.object(importer, '_resolve_public_url', side_effect=public_resolution), \
                patch.object(importer.asyncio, 'create_subprocess_exec', AsyncMock(return_value=Process())), \
                patch.object(importer.os, 'killpg') as killpg, \
                patch.object(importer, 'YOUTUBE_STARTUP_TIMEOUT', 0.01), \
                patch.object(importer, 'PROCESS_STOP_TIMEOUT', 0.01):
            with self.assertRaisesRegex(importer.VideoImportError, 'did not start'):
                await asyncio.wait_for(importer.import_video('https://youtu.be/dQw4w9WgXcQ', self.directory), 0.2)
        self.assertEqual([call.args for call in killpg.call_args_list],
                         [(12345, signal.SIGTERM), (12345, signal.SIGKILL)])
        self.assertEqual(list(self.directory.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
