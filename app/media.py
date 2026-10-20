"""Literal frame-stride sampling with presentation timestamps, including VFR."""
import asyncio
import json
import math
import re
from pathlib import Path

SAMPLE_EVERY = 10


async def _stop_playback_process(proc):
    """Reap a cancelled probe/transcode without leaving an FFmpeg child behind."""
    if proc.returncode is not None:
        return
    try:
        proc.terminate()
    except ProcessLookupError:
        pass
    try:
        await asyncio.wait_for(proc.communicate(), timeout=5)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.communicate()


async def ensure_playback(source: Path, target: Path) -> Path:
    """Return a browser-compatible source or an atomically cached H264 MP4 copy.

    Sampling/indexing must still use the original video, so transcoding does not
    change the meaning of "every tenth frame". This copy is only for playback.
    """
    source, target = Path(source), Path(target)
    if target != source and target.is_file() and target.stat().st_size > 0:
        return target
    probe_proc = await asyncio.create_subprocess_exec(
        'ffprobe', '-v', 'error', '-show_entries',
        'stream=codec_type,codec_name,pix_fmt:format=format_name',
        '-of', 'json', str(source),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        output, _ = await probe_proc.communicate()
    except BaseException:
        await _stop_playback_process(probe_proc)
        raise
    if probe_proc.returncode:
        raise ValueError('The video could not be inspected for browser playback.')
    try:
        info = json.loads(output)
        streams = info.get('streams', [])
        videos = [stream for stream in streams if stream.get('codec_type') == 'video']
        audio = [stream for stream in streams if stream.get('codec_type') == 'audio']
    except (ValueError, TypeError, AttributeError):
        raise ValueError('The video returned unreadable playback metadata.') from None
    if not videos:
        raise ValueError('The file has no video stream.')
    video = videos[0]
    compatible = (
        source.suffix.lower() == '.mp4'
        and 'mp4' in info.get('format', {}).get('format_name', '').split(',')
        and video.get('codec_name') == 'h264'
        and video.get('pix_fmt') == 'yuv420p'
        and all(stream.get('codec_name') in ('aac', 'mp3') for stream in audio)
    )
    if compatible:
        return source
    if source.resolve() == target.resolve():
        raise ValueError('The playback copy must have a different path from the source video.')
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.stem + '.part.mp4')
    temporary.unlink(missing_ok=True)
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            'ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-y',
            '-i', str(source), '-map', '0:v:0', '-map', '0:a:0?',
            '-vf', 'pad=ceil(iw/2)*2:ceil(ih/2)*2',
            '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '22',
            '-pix_fmt', 'yuv420p', '-fps_mode', 'vfr',
            '-c:a', 'aac', '-b:a', '160k', '-movflags', '+faststart',
            str(temporary),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()
        if proc.returncode or not temporary.is_file() or temporary.stat().st_size == 0:
            raise ValueError('Unable to create a browser-compatible playback copy.')
        temporary.replace(target)
    except BaseException:
        if proc is not None:
            await _stop_playback_process(proc)
        temporary.unlink(missing_ok=True)
        raise
    return target


async def probe(path: Path):
    proc = await asyncio.create_subprocess_exec('ffprobe', '-v','error','-select_streams','v:0',
        '-show_entries','stream=width,height,avg_frame_rate,duration,nb_frames:format=duration',
        '-of','json',str(path),stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
    output, _ = await proc.communicate()
    if proc.returncode:
        raise ValueError('This file could not be read as a video.')
    info = json.loads(output)
    if not info.get('streams'):
        raise ValueError('The file has no video stream.')
    stream = info['streams'][0]
    rate = stream.get('avg_frame_rate','0/1').split('/')
    fps = float(rate[0]) / float(rate[1]) if len(rate)==2 and float(rate[1]) else 0
    duration = float(info.get('format',{}).get('duration') or stream.get('duration') or 0)
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError('The video needs a finite, readable duration.')
    return dict(duration=duration,fps=fps,width=int(stream['width']),height=int(stream['height']))


async def extract(path: Path, directory: Path, progress=None):
    directory.mkdir(parents=True,exist_ok=True)
    manifest = directory / 'frames.json'
    if manifest.exists():
        frames = json.loads(manifest.read_text())
        if frames and all((directory / f['file']).exists() for f in frames):
            return [{'timestamp':f['timestamp'],'path':directory/f['file']} for f in frames]
    # n is the decoded frame number, NOT seconds. showinfo preserves each chosen PTS.
    filters = r'select=not(mod(n\,10)),showinfo,scale=640:640:force_original_aspect_ratio=decrease,pad=max(iw\,280):max(ih\,280):(ow-iw)/2:(oh-ih)/2'
    proc = await asyncio.create_subprocess_exec('ffmpeg','-nostdin','-hide_banner','-y','-i',str(path),
        '-map','0:v:0','-an','-vf',filters,'-fps_mode','vfr','-q:v','3',str(directory/'frame-%08d.jpg'),
        stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.PIPE)
    timestamps, tail = [], []
    try:
        while line := await proc.stderr.readline():
            text = line.decode(errors='replace')
            tail = (tail+[text])[-8:]
            match = re.search(r'\bn:\s*\d+.*?\bpts_time:([\d.eE+\-]+)',text)
            if match:
                timestamps.append(float(match.group(1)))
                if progress and len(timestamps)%30==0:
                    progress(len(timestamps))
        await proc.wait()
    except BaseException:
        if proc.returncode is None:
            proc.terminate()
            await proc.wait()
        raise
    if proc.returncode or not timestamps:
        raise ValueError('Frame extraction failed. The video may be damaged or unsupported.')
    first_pts = timestamps[0]
    frames = [{'file':f'frame-{i+1:08d}.jpg','timestamp':max(0,t-first_pts)} for i,t in enumerate(timestamps)]
    if not all((directory/f['file']).is_file() for f in frames):
        raise ValueError('Frame extraction produced an incomplete frame sequence.')
    manifest.write_text(json.dumps(frames))
    return [{'timestamp':f['timestamp'],'path':directory/f['file']} for f in frames]


async def make_clip(source: Path, target: Path, start: float, end: float):
    proc = await asyncio.create_subprocess_exec('ffmpeg','-nostdin','-hide_banner','-loglevel','error','-y',
        '-ss',str(start),'-i',str(source),'-t',str(end-start),'-map','0:v:0','-map','0:a?',
        '-c:v','libx264','-preset','veryfast','-crf','23','-c:a','aac','-movflags','+faststart',str(target),
        stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.PIPE)
    try:
        await proc.communicate()
    except BaseException:
        if proc.returncode is None:
            proc.terminate()
            await proc.wait()
        raise
    if proc.returncode:
        target.unlink(missing_ok=True)
        raise ValueError('Unable to export this clip.')
