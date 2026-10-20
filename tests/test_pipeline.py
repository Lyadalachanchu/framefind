import asyncio
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from app.db import Database
from app.media import extract, make_clip, probe
from app.search import build_snippets, diverse_candidates, rank_frames


def frame(i,t,video='v',score=.9):
    return dict(id=i,timestamp=t,video_id=video,score=score,probabilities=json.dumps({'car':.95}),index_score=score)


def test_literal_sampling_and_video_timestamps(tmp_path):
    if not shutil.which('ffmpeg'):
        pytest.skip('FFmpeg unavailable')
    source=tmp_path/'test.mp4'
    subprocess.run(['ffmpeg','-v','error','-y','-f','lavfi','-i','testsrc2=size=320x240:rate=30','-t','2','-c:v','libx264',str(source)],check=True)
    frames=asyncio.run(extract(source,tmp_path/'frames'))
    assert len(frames)==6
    assert [f['timestamp'] for f in frames]==pytest.approx([0,1/3,2/3,1,4/3,5/3],abs=1e-5)
    info=asyncio.run(probe(source))
    assert info['fps']==30
    from PIL import Image
    with Image.open(frames[0]['path']) as img:
        assert all(280<=side<=1568 for side in img.size)
    # Resume reuses the exact persisted manifest and frame IDs.
    assert asyncio.run(extract(source,tmp_path/'frames'))==frames
    clip=tmp_path/'clip.mp4'
    asyncio.run(make_clip(source,clip,.3,1.3))
    assert asyncio.run(probe(clip))['duration']==pytest.approx(1,abs=.1)


def test_snippets_do_not_cross_gaps_or_videos():
    videos={key:dict(id=key,name=key,fps=30,duration=20) for key in ('a','b')}
    matches=[frame(1,1,'a'),frame(2,1.33,'a'),frame(3,9,'a'),frame(4,1.4,'b'),frame(5,10,'a',.2)]
    snippets=build_snippets(matches,videos,[{'id':'car','label':'Car'}],.6)
    assert len(snippets)==3
    assert sorted(s['evidence_count'] for s in snippets)==[1,1,2]
    assert all(0<=s['start']<s['end']<=20 for s in snippets)


def test_variable_frame_rate_uses_presentation_times(tmp_path):
    if not shutil.which('ffmpeg'):
        pytest.skip('FFmpeg unavailable')
    source=tmp_path/'vfr.mp4'
    subprocess.run(['ffmpeg','-v','error','-y','-f','lavfi','-i','testsrc2=size=320x280:rate=30:duration=2',
        '-vf',r'setpts=if(lt(N\,30)\,N/(30*TB)\,(1+(N-30)/15)/TB)',
        '-fps_mode','vfr','-c:v','libx264',str(source)],check=True)
    frames=asyncio.run(extract(source,tmp_path/'frames'))
    assert len(frames)==6
    assert [f['timestamp'] for f in frames]==pytest.approx([0,1/3,2/3,1,5/3,7/3],abs=.04)


def test_candidate_diversity():
    candidates=diverse_candidates([frame(i,i/30) for i in range(300)],limit=3)
    assert [int(f['timestamp']//2) for f in candidates]==[0,1,2]


def test_index_ranks_requested_conjunction():
    frames=[dict(id=1,probabilities=json.dumps({'car':.95,'snow':.01})),dict(id=2,probabilities=json.dumps({'car':.9,'snow':.9}))]
    assert rank_frames(frames,{'car':1,'snow':1},[])[0]['id']==2


def test_resume_keeps_completed_frames_and_cascades(tmp_path):
    db=Database(tmp_path)
    db.run("INSERT INTO videos(id,name,source,status) VALUES('v','test','test.mp4','indexing')")
    db.save_frames('v',[dict(timestamp=0,path='0.jpg'),dict(timestamp=.3,path='1.jpg')])
    frames=db.all('SELECT * FROM frames')
    db.save_index(frames[0]['id'],{'car':.9})
    db.run("UPDATE frames SET status='processing' WHERE id=?",(frames[1]['id'],))
    db.close()
    db=Database(tmp_path)
    assert db.one('SELECT status FROM videos')['status']=='paused'
    assert [r['status'] for r in db.all('SELECT status FROM frames ORDER BY ordinal')]==['ready','pending']
    db.run('DELETE FROM videos')
    assert db.all('SELECT * FROM frames')==[]
    db.close()
