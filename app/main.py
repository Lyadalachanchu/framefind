import asyncio
import math
import os
import shutil
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from .db import Database
from .importer import import_video
from .jev import JevClient
from .media import SAMPLE_EVERY, extract, make_clip, probe, ensure_playback
from .search import MIN_MATCH, build_snippets, normalized_query, top_labels
from .taxonomy import LABELS, TAXONOMY_VERSION

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get('JEV_DATA_DIR',ROOT/'data')).resolve()
CONCURRENCY = max(1,min(24,int(os.environ.get('JEV_CONCURRENCY','8'))))
MODEL = os.environ.get('JEV_MODEL','jev-1.14.0')
PRICE = 0.042
# A hard local cost guard. Reserve enough for one full 64k context per in-flight
# request; successful response usage replaces the reservation with actual usage.
BUDGET = float(os.environ.get('JEV_BUDGET_USD','15'))
MAX_UPLOAD = 2*1024**3
MAX_DURATION = 60*60
db = None
client = None
video_tasks = {}
pause_flags = {}
search_jobs = {}
search_tasks = {}


def spent_tokens():
    return db.one('SELECT COALESCE(SUM(input_tokens),0) AS n FROM usage')['n']


async def paid_call(kind, call, *args, video_id=None):
    # Include every allowed attempt, even if an upstream 5xx was billed. Keep
    # uncertain calls reserved across restarts rather than silently undercounting.
    reserve=5*65536
    reserved=db.one('SELECT COALESCE(SUM(tokens),0) AS n FROM budget_reservations')['n']
    if (spent_tokens()+reserved+reserve)*PRICE/1_000_000 > BUDGET:
        raise ValueError('The local API spending limit has been reached. Raise JEV_BUDGET_USD and restart to continue.')
    reservation_id=db.run("INSERT INTO budget_reservations(tokens,status) VALUES(?,'pending')",(reserve,))
    try:
        result=await call(*args)
        tokens=int(result.get('input_tokens',0))
        with db.conn:
            db.conn.execute('INSERT INTO usage(kind,input_tokens) VALUES(?,?)',(kind,tokens))
            if video_id:
                db.conn.execute('UPDATE videos SET input_tokens=input_tokens+? WHERE id=?',(tokens,video_id))
            uncertain=int(result.get('uncertain_input_tokens',0))
            if uncertain:
                db.conn.execute("UPDATE budget_reservations SET tokens=?,status='uncertain' WHERE id=?",(uncertain,reservation_id))
            else:
                db.conn.execute('DELETE FROM budget_reservations WHERE id=?',(reservation_id,))
        return result
    except BaseException:
        db.run("UPDATE budget_reservations SET status='uncertain' WHERE id=?",(reservation_id,))
        raise


@asynccontextmanager
async def lifespan(app):
    global db,client
    DATA.mkdir(parents=True,exist_ok=True)
    (DATA/'videos').mkdir(exist_ok=True)
    (DATA/'clips').mkdir(exist_ok=True)
    db=Database(DATA)
    db.run('CREATE TABLE IF NOT EXISTS budget_reservations (id INTEGER PRIMARY KEY,tokens INTEGER NOT NULL,status TEXT NOT NULL)')
    db.run("UPDATE budget_reservations SET status='uncertain' WHERE status='pending'")
    key=os.environ.get('TYPESAFE_API_KEY','').strip()
    client=JevClient(key,concurrency=CONCURRENCY,model=MODEL) if key else None
    yield
    for flag in pause_flags.values():
        flag.set()
    tasks=list(video_tasks.values())+list(search_tasks.values())
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks,return_exceptions=True)
    if client:
        await client.close()
    db.close()


app=FastAPI(title='FrameFind',lifespan=lifespan,docs_url=None,redoc_url=None)


@app.middleware('http')
async def local_only(request: Request,call_next):
    # Bind to loopback too. These checks stop unrelated websites driving the
    # user's local paid API through cross-origin forms or DNS rebinding.
    hostname=request.url.hostname
    if hostname not in ('localhost','127.0.0.1','::1','testserver'):
        return JSONResponse({'detail':'This app accepts local requests only.'},status_code=403)
    origin=request.headers.get('origin')
    if origin and origin.rstrip('/') != str(request.base_url).rstrip('/'):
        return JSONResponse({'detail':'Cross-origin requests are not allowed.'},status_code=403)
    if request.headers.get('sec-fetch-site')=='cross-site':
        return JSONResponse({'detail':'Cross-site requests are not allowed.'},status_code=403)
    response=await call_next(request)
    response.headers['X-Content-Type-Options']='nosniff'
    response.headers['Referrer-Policy']='no-referrer'
    response.headers['Content-Security-Policy']="default-src 'self'; img-src 'self' data:; media-src 'self' blob:; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'"
    return response


def require_client():
    if client is None:
        raise HTTPException(503,'Restart with TYPESAFE_API_KEY set to enable search.')


def require_media_tools():
    if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
        raise HTTPException(503,'Install FFmpeg (brew install ffmpeg), then restart.')


def get_video(video_id):
    row=db.one('SELECT * FROM videos WHERE id=?',(video_id,))
    if not row:
        raise HTTPException(404,'Video not found.')
    return row


def public_video(row):
    counts=db.one("SELECT COUNT(*) AS total, SUM(status='ready') AS ready, SUM(status='failed') AS failed FROM frames WHERE video_id=?",(row['id'],))
    first=db.one('SELECT id FROM frames WHERE video_id=? ORDER BY ordinal LIMIT 1',(row['id'],))
    return {key:value for key,value in row.items() if key!='source'} | dict(
        frames_total=max(row['frames_total'],counts['total']),frames_ready=counts['ready'] or 0,
        frames_indexed=counts['ready'] or 0,
        frames_failed=counts['failed'] or 0,cost_usd=row['input_tokens']*PRICE/1_000_000,
        thumbnail_url=f"/api/frames/{first['id']}/image" if first else None,
        media_url=f"/api/videos/{row['id']}/media")


def safe_error(exc):
    # Never echo upstream response bodies, credentials, or subprocess arguments.
    if isinstance(exc,(ValueError,HTTPException)):
        return str(getattr(exc,'detail',exc))[:300]
    message=str(exc)
    if 'apikey_' in message or len(message)>500:
        return 'The operation failed. Please retry or check your API configuration.'
    return message[:300] or type(exc).__name__


def start_prepare(video_id):
    if video_id in video_tasks and not video_tasks[video_id].done():
        return
    flag=asyncio.Event()
    pause_flags[video_id]=flag
    video_tasks[video_id]=asyncio.create_task(prepare_video(video_id,flag))


async def prepare_video(video_id,flag):
    try:
        row=get_video(video_id)
        source=Path(row['source'])
        if not source.is_file():
            raise ValueError('The source video is missing. Import the video again.')
        metadata=await probe(source)
        if metadata['duration']>MAX_DURATION:
            raise ValueError('This local build supports videos up to 60 minutes. Trim longer videos before importing.')
        db.update_video(video_id,**metadata,error=None)
        db.update_video(video_id,status='extracting',stage='Preparing browser playback')
        await ensure_playback(source,DATA/'videos'/video_id/'playback.mp4')
        saved=db.all('SELECT * FROM frames WHERE video_id=? ORDER BY ordinal',(video_id,))
        if saved and all(Path(frame['path']).is_file() and Path(frame['path']).stat().st_size for frame in saved):
            # Legacy indexing failures do not invalidate the underlying images.
            # Keep their IDs and query caches; deep search uses the raw frames.
            db.run("UPDATE frames SET status='ready',error=NULL WHERE video_id=?",(video_id,))
            db.update_video(video_id,frames_total=len(saved))
        else:
            db.update_video(video_id,status='extracting',stage='Preparing frames')
            def progress(n):
                db.update_video(video_id,frames_total=n,stage=f'Prepared {n:,} frames')
            frames=await extract(source,DATA/'videos'/video_id/'frames',progress)
            if not frames or not all(Path(frame['path']).is_file() and Path(frame['path']).stat().st_size for frame in frames):
                raise ValueError('Some prepared frames are missing. Resume to prepare them again.')
            db.save_prepared_frames(video_id,frames)
            db.update_video(video_id,frames_total=len(frames))
        if flag.is_set():
            db.update_video(video_id,status='paused',stage='Paused')
            return
        db.update_video(video_id,status='ready',stage='Ready to search',error=None)
    except asyncio.CancelledError:
        db.update_video(video_id,status='paused',stage='Paused; resume to continue')
        raise
    except Exception as exc:
        db.update_video(video_id,status='failed',stage='Preparation stopped',error=safe_error(exc))


@app.get('/api/status')
async def status():
    tokens=spent_tokens()
    reserved=db.one('SELECT COALESCE(SUM(tokens),0) AS n FROM budget_reservations')['n']
    return dict(configured=client is not None,model=MODEL,sample_every=SAMPLE_EVERY,concurrency=CONCURRENCY,
        price_per_million=PRICE,budget_usd=BUDGET,budget_committed_usd=(tokens+reserved)*PRICE/1_000_000,
        ffmpeg_available=bool(shutil.which('ffmpeg') and shutil.which('ffprobe')),
        stats=dict(videos=db.one('SELECT COUNT(*) AS n FROM videos')['n'],
            frames=db.one("SELECT COUNT(*) AS n FROM frames WHERE status='ready'")['n'],input_tokens=tokens,cost_usd=tokens*PRICE/1_000_000))


@app.get('/api/videos')
async def videos():
    return {'videos':[public_video(row) for row in db.all('SELECT * FROM videos ORDER BY created_at DESC,id DESC')]}


@app.post('/api/videos',status_code=201)
async def upload(file: UploadFile=File(...)):
    require_media_tools()
    video_id=uuid.uuid4().hex
    directory=DATA/'videos'/video_id
    directory.mkdir(parents=True)
    name=Path(file.filename or 'Untitled video').name[:160]
    suffix=Path(name).suffix.lower()
    if suffix not in {'.mp4','.mov','.mkv','.webm','.avi','.m4v','.mpeg','.mpg'}:
        shutil.rmtree(directory)
        raise HTTPException(400,'Choose an MP4, MOV, MKV, WebM, AVI, or MPEG video.')
    source=directory/('source'+suffix)
    try:
        size=0
        with source.open('wb') as target:
            while chunk:=await file.read(1024*1024):
                size+=len(chunk)
                if size>MAX_UPLOAD:
                    raise HTTPException(413,'The maximum video size is 2 GB.')
                target.write(chunk)
        if size==0:
            raise HTTPException(400,'The uploaded file is empty.')
    except BaseException:
        shutil.rmtree(directory,ignore_errors=True)
        raise
    finally:
        await file.close()
    db.run('INSERT INTO videos(id,name,source) VALUES(?,?,?)',(video_id,name,str(source)))
    start_prepare(video_id)
    return public_video(get_video(video_id))


class ImportRequest(BaseModel):
    url: str=Field(min_length=8,max_length=4096)


@app.post('/api/videos/import',status_code=201)
async def import_url(body: ImportRequest):
    require_media_tools()
    parsed=urlparse(body.url)
    if parsed.scheme not in ('https','http') or not parsed.hostname:
        raise HTTPException(400,'Enter a public HTTP or HTTPS video URL.')
    video_id=uuid.uuid4().hex
    directory=DATA/'videos'/video_id
    directory.mkdir(parents=True)
    db.run("INSERT INTO videos(id,name,source,status,stage) VALUES(?,?,?,'downloading','Downloading video')",(video_id,'URL import',str(directory/'source.mp4')))
    flag=asyncio.Event()
    pause_flags[video_id]=flag
    async def download_and_prepare():
        downloaded=False
        try:
            source,name=await import_video(body.url,directory,lambda text:db.update_video(video_id,stage=text))
            db.run('UPDATE videos SET source=?,name=? WHERE id=?',(str(source),name[:160],video_id))
            downloaded=True
            await prepare_video(video_id,flag)
        except asyncio.CancelledError:
            if not downloaded:
                db.update_video(video_id,status='failed',stage='Download cancelled',error='Import this URL again to restart the download.')
            raise
        except Exception as exc:
            db.update_video(video_id,status='failed',stage='Import failed',error=safe_error(exc))
    video_tasks[video_id]=asyncio.create_task(download_and_prepare())
    return public_video(get_video(video_id))


@app.post('/api/videos/{video_id}/pause')
async def pause(video_id: str):
    row=get_video(video_id)
    if video_id in pause_flags:
        pause_flags[video_id].set()
    task=video_tasks.get(video_id)
    if task and not task.done():
        task.cancel()
    return {'ok':True}


@app.post('/api/videos/{video_id}/resume')
async def resume(video_id: str):
    require_media_tools()
    get_video(video_id)
    start_prepare(video_id)
    return {'ok':True}


@app.delete('/api/videos/{video_id}')
async def delete_video(video_id: str):
    get_video(video_id)
    # Searches reference these frames; do not delete out from under them.
    if any(job['status']=='running' and video_id in job.get('_video_ids',[]) for job in search_jobs.values()):
        raise HTTPException(409,'Wait for the current search to finish before deleting this video.')
    task=video_tasks.get(video_id)
    if task and not task.done():
        task.cancel()
        await asyncio.gather(task,return_exceptions=True)
    db.run('DELETE FROM videos WHERE id=?',(video_id,))
    shutil.rmtree(DATA/'videos'/video_id,ignore_errors=True)
    return {'ok':True}


@app.get('/api/videos/{video_id}/frames')
async def list_frames(video_id: str,limit: int=100):
    get_video(video_id)
    rows=db.all('SELECT * FROM frames WHERE video_id=? ORDER BY ordinal LIMIT ?',(video_id,max(1,min(500,limit))))
    return {'frames':[dict(id=f['id'],video_id=video_id,timestamp=f['timestamp'],status=f['status'],
        thumbnail_url=f"/api/frames/{f['id']}/image",labels=top_labels(f['probabilities'],LABELS)) for f in rows]}


@app.get('/api/frames/{frame_id}/image')
async def frame_image(frame_id: int):
    frame=db.one('SELECT path FROM frames WHERE id=?',(frame_id,))
    if not frame or not Path(frame['path']).is_file():
        raise HTTPException(404,'Frame not found.')
    return FileResponse(frame['path'],media_type='image/jpeg',headers={'Cache-Control':'private, max-age=86400'})


@app.get('/api/videos/{video_id}/media')
async def video_media(video_id: str):
    source=Path(get_video(video_id)['source'])
    playback=DATA/'videos'/video_id/'playback.mp4'
    if playback.is_file():
        source=playback
    if not source.is_file():
        raise HTTPException(404,'Video is not ready yet.')
    return FileResponse(source)


@app.get('/api/videos/{video_id}/clip')
async def clip(video_id: str,start: float,end: float):
    row=get_video(video_id)
    if not (math.isfinite(start) and math.isfinite(end) and 0<=start<end<=row['duration']+0.05 and end-start<=60):
        raise HTTPException(400,'Choose a valid snippet up to 60 seconds long.')
    target=DATA/'clips'/f'{uuid.uuid4().hex}.mp4'
    try:
        await make_clip(Path(row['source']),target,start,end)
    except ValueError as exc:
        raise HTTPException(500,str(exc))
    return FileResponse(target,media_type='video/mp4',filename=f'framefind-{start:.1f}-{end:.1f}.mp4',
        background=BackgroundTask(target.unlink,missing_ok=True))


class SearchRequest(BaseModel):
    query: str=Field(min_length=2,max_length=500)
    video_ids: list[str]=Field(default_factory=list,max_length=100)


@app.post('/api/search',status_code=202)
async def start_search(body: SearchRequest):
    require_client()
    if len([job for job in search_jobs.values() if job['status']=='running'])>=2:
        raise HTTPException(429,'Two searches are already running. Wait or cancel one first.')
    query=body.query.strip()
    if len(query)<2:
        raise HTTPException(400,'Enter a search with at least two characters.')
    video_rows=db.all('SELECT * FROM videos')
    selected={row['id']:row for row in video_rows if not body.video_ids or row['id'] in body.video_ids}
    if not selected:
        raise HTTPException(400,'Add a video first.')
    placeholders=','.join('?' for _ in selected)
    frames=db.all(f"SELECT * FROM frames WHERE status='ready' AND video_id IN ({placeholders}) ORDER BY video_id,ordinal",tuple(selected))
    if not frames:
        raise HTTPException(400,'The video is still being prepared.')
    # Keep only a bounded history of completed search jobs in memory.
    if len(search_jobs)>100:
        for old_id in list(search_jobs):
            if search_jobs[old_id]['status']!='running':
                search_jobs.pop(old_id); search_tasks.pop(old_id,None)
                if len(search_jobs)<=80:
                    break
    job_id=uuid.uuid4().hex
    job=dict(id=job_id,status='running',progress=0,total=0,results=[],frames_checked=0,total_frames=len(frames),
        cached=0,error=None,input_tokens=0,cost_usd=0,coverage='Preparing search',_video_ids=list(selected),_cancel=False)
    search_jobs[job_id]=job
    search_tasks[job_id]=asyncio.create_task(run_search(job,body,frames,selected))
    return {'id':job_id}


async def run_search(job,body,frames,videos):
    try:
        query=normalized_query(body.query)
        cache_key=f'{TAXONOMY_VERSION}:sequence-v1:{query}'
        candidates=frames
        job['coverage']=f"Checking all {len(frames):,} frames"
        job['total']=len(candidates)
        matches=[]
        # Neighboring images give Jev short temporal evidence for actions.
        by_video={}
        for frame in frames:
            by_video.setdefault(frame['video_id'],[]).append(frame)
        positions={f['id']:(group,i) for group in by_video.values() for i,f in enumerate(group)}
        queue=asyncio.Queue()
        for frame in candidates:
            queue.put_nowait(frame)
        errors=[]
        async def worker():
            while not job['_cancel']:
                try:
                    frame=queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                try:
                    group,i=positions[frame['id']]
                    neighbors=[f for f in group[max(0,i-1):i+2] if abs(f['timestamp']-frame['timestamp'])<=1.5]
                    frame_cache_key=cache_key+':'+','.join(str(f['id']) for f in neighbors)
                    cached=db.one('SELECT score FROM match_cache WHERE frame_id=? AND query=? AND model=?',(frame['id'],frame_cache_key,MODEL))
                    if cached:
                        score=cached['score']; job['cached']+=1
                    else:
                        result=await paid_call('search',client.match_frames,[Path(f['path']) for f in neighbors],body.query)
                        score=result['score']; job['input_tokens']+=result['input_tokens']
                        db.run('INSERT OR REPLACE INTO match_cache(frame_id,query,model,score) VALUES(?,?,?,?)',(frame['id'],frame_cache_key,MODEL,score))
                    matches.append({**frame,'score':score})
                    job['frames_checked']+=1
                except Exception as exc:
                    errors.append(safe_error(exc))
                    if len(errors)>=3:
                        job['_cancel']=True
                finally:
                    job['progress']+=1
                    job['cost_usd']=job['input_tokens']*PRICE/1_000_000
                    job['results']=build_snippets(matches,videos,LABELS,MIN_MATCH)
                    queue.task_done()
        await asyncio.gather(*(worker() for _ in range(CONCURRENCY)))
        if errors:
            job['status']='failed'; job['error']=errors[-1]
        else:
            job['status']='cancelled' if job['_cancel'] else 'complete'
    except asyncio.CancelledError:
        job['status']='cancelled'
        raise
    except Exception as exc:
        job['status']='failed'; job['error']=safe_error(exc)


@app.get('/api/search/{job_id}')
async def get_search(job_id: str):
    if job_id not in search_jobs:
        raise HTTPException(404,'Search not found. Run your search again.')
    return {key:value for key,value in search_jobs[job_id].items() if not key.startswith('_')}


@app.post('/api/search/{job_id}/cancel')
async def cancel_search(job_id: str):
    if job_id not in search_jobs:
        raise HTTPException(404,'Search not found.')
    search_jobs[job_id]['_cancel']=True
    return {'ok':True}


app.mount('/',StaticFiles(directory=ROOT/'app'/'static',html=True),name='static')
