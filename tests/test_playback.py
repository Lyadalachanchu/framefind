"""Small real-FFmpeg checks for browser playback conversion and caching."""
import asyncio
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from app.media import ensure_playback


def metadata(path):
    return json.loads(subprocess.check_output([
        'ffprobe', '-v', 'error', '-show_entries',
        'stream=codec_type,codec_name,pix_fmt:format=duration', '-of', 'json', str(path),
    ]))


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg is required')
class PlaybackTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)

    async def asyncTearDown(self):
        self.directory.cleanup()

    def create_video(self, name, codec, audio_codec=None):
        target = self.root / name
        command = [
            'ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-y',
            '-f', 'lavfi', '-i', 'testsrc2=size=320x180:rate=12:duration=0.75',
        ]
        if audio_codec:
            command += ['-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000:duration=0.75']
        command += ['-c:v', codec, '-pix_fmt', 'yuv420p']
        if codec == 'libvpx-vp9':
            command += ['-deadline', 'realtime', '-cpu-used', '8']
        if audio_codec:
            command += ['-c:a', audio_codec]
        command += [str(target)]
        subprocess.run(command, check=True, capture_output=True)
        return target

    async def test_webm_is_transcoded_to_h264_aac_mp4(self):
        source = self.create_video('source.webm', 'libvpx-vp9', 'libopus')
        target = self.root / 'playback.mp4'
        before = source.read_bytes()
        result = await ensure_playback(source, target)
        self.assertEqual(result, target)
        info = metadata(target)
        video = next(stream for stream in info['streams'] if stream['codec_type'] == 'video')
        audio = next(stream for stream in info['streams'] if stream['codec_type'] == 'audio')
        self.assertEqual(video['codec_name'], 'h264')
        self.assertEqual(video['pix_fmt'], 'yuv420p')
        self.assertEqual(audio['codec_name'], 'aac')
        self.assertLess(abs(float(info['format']['duration']) - float(metadata(source)['format']['duration'])), 0.15)
        self.assertEqual(source.read_bytes(), before)
        self.assertFalse(target.with_name('playback.part.mp4').exists())

    async def test_complete_playback_cache_is_reused_without_subprocess(self):
        source = self.create_video('source.webm', 'libvpx-vp9')
        target = self.root / 'playback.mp4'
        await ensure_playback(source, target)
        before = target.stat().st_mtime_ns
        with patch('app.media.asyncio.create_subprocess_exec', side_effect=AssertionError('Cache should not transcode again')):
            self.assertEqual(await ensure_playback(source, target), target)
        self.assertEqual(target.stat().st_mtime_ns, before)

    async def test_h264_mp4_with_aac_passes_through(self):
        source = self.create_video('source.mp4', 'libx264', 'aac')
        target = self.root / 'playback.mp4'
        self.assertEqual(await ensure_playback(source, target), source)
        self.assertFalse(target.exists())

    async def test_h264_mp4_without_audio_passes_through(self):
        source = self.create_video('source.mp4', 'libx264')
        self.assertEqual(await ensure_playback(source, self.root / 'playback.mp4'), source)


if __name__ == '__main__':
    unittest.main()
