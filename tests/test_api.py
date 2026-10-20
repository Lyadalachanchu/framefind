import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app import main
from app.db import Database


@pytest.fixture
def api(tmp_path,monkeypatch):
    monkeypatch.setattr(main,'DATA',tmp_path)
    for name in ('video_tasks','pause_flags','search_jobs','search_tasks'):
        monkeypatch.setattr(main,name,{})
    monkeypatch.delenv('TYPESAFE_API_KEY',raising=False)
    with TestClient(main.app) as client:
        yield client


def test_status_never_exposes_credentials(api):
    result=api.get('/api/status')
    assert result.status_code==200
    assert result.json()['configured'] is False
    assert 'api_key' not in result.text


def test_cross_origin_and_rebinding_requests_blocked(api):
    assert api.post('/api/videos/import',json={'url':'https://example.com/a.mp4'},headers={'Origin':'https://attacker.example'}).status_code==403
    assert api.get('/api/status',headers={'Host':'attacker.example'}).status_code==403
    assert api.post('/api/search',json={'query':'car'},headers={'Sec-Fetch-Site':'cross-site'}).status_code==403


def test_pending_frames_have_empty_labels_and_source_path_is_private(api):
    main.db.run("INSERT INTO videos(id,name,source,status) VALUES('v','Test','/private/source.mp4','paused')")
    main.db.save_frames('v',[dict(timestamp=0,path='/private/0.jpg')])
    video=api.get('/api/videos').json()['videos'][0]
    assert 'source' not in video
    frame=api.get('/api/videos/v/frames').json()['frames'][0]
    assert frame['labels']==[]
    assert 'path' not in frame


def test_invalid_clip_bounds_are_rejected(api):
    main.db.run("INSERT INTO videos(id,name,source,duration) VALUES('v','Test','source.mp4',5)")
    for bounds in ('start=-1&end=2','start=0&end=10','start=nan&end=1','start=2&end=1'):
        assert api.get('/api/videos/v/clip?'+bounds).status_code==400


def test_missing_key_blocks_paid_operations(api):
    assert api.post('/api/search',json={'query':'car'}).status_code==503


def wait_for(api,url,finished):
    deadline=time.monotonic()+5
    while time.monotonic()<deadline:
        response=api.get(url)
        assert response.status_code==200
        body=response.json()
        if finished(body):
            return body
        time.sleep(.01)
    pytest.fail(f'Background operation did not complete: {body}')


@pytest.mark.parametrize('source',['upload','url'])
def test_preparation_without_key_never_calls_jev(api,monkeypatch,tmp_path,source):
    """Import is local preparation; every extracted frame is immediately usable."""
    monkeypatch.setattr(main.shutil,'which',lambda command:f'/usr/bin/{command}')
    monkeypatch.setattr(main,'probe',AsyncMock(return_value=dict(duration=2,fps=30,width=320,height=240)))
    monkeypatch.setattr(main,'ensure_playback',AsyncMock())
    prepared=tmp_path/'frame.jpg'
    prepared.write_bytes(b'nonempty extracted frame')
    monkeypatch.setattr(main,'extract',AsyncMock(return_value=[dict(timestamp=0,path=prepared)]))
    paid=AsyncMock(side_effect=AssertionError('Preparation must not call Jev'))
    monkeypatch.setattr(main,'paid_call',paid)
    if source=='upload':
        response=api.post('/api/videos',files={'file':('test.mp4',b'video','video/mp4')})
    else:
        downloaded=tmp_path/'download.mp4'
        downloaded.write_bytes(b'video')
        monkeypatch.setattr(main,'import_video',AsyncMock(return_value=(downloaded,'Public video')))
        response=api.post('/api/videos/import',json={'url':'https://example.com/video.mp4'})
    assert response.status_code==201
    video_id=response.json()['id']
    result=wait_for(api,'/api/videos',lambda body:body['videos'][0]['status'] in ('ready','failed'))
    video=result['videos'][0]
    assert video['id']==video_id
    assert video['status']=='ready'
    assert video['frames_ready']==video['frames_total']==1
    assert video['cost_usd']==0
    assert main.db.one('SELECT probabilities FROM frames')['probabilities'] is None
    assert main.spent_tokens()==0
    paid.assert_not_awaited()


def test_preparation_reuses_legacy_frames_and_query_cache(api,monkeypatch,tmp_path):
    source=tmp_path/'source.mp4'
    source.write_bytes(b'video')
    main.db.run("INSERT INTO videos(id,name,source,status) VALUES('v','Test',?,'paused')",(str(source),))
    extracted=[]
    for i in range(3):
        image=tmp_path/f'{i}.jpg'
        image.write_bytes(b'frame')
        extracted.append(dict(timestamp=i/3,path=image))
    main.db.save_frames('v',extracted)
    original=main.db.all('SELECT id FROM frames ORDER BY ordinal')
    for frame,status in zip(original,('pending','failed','processing')):
        main.db.run('UPDATE frames SET status=?,error=? WHERE id=?',(status,'Old indexing failure',frame['id']))
        main.db.run('INSERT INTO match_cache(frame_id,query,model,score) VALUES(?,?,?,?)',(frame['id'],'cached',main.MODEL,.98))
    main.db.add_usage('index',2500,'v')
    monkeypatch.setattr(main,'probe',AsyncMock(return_value=dict(duration=2,fps=30,width=320,height=240)))
    monkeypatch.setattr(main,'ensure_playback',AsyncMock())
    extract=AsyncMock(side_effect=AssertionError('Saved images should be reused'))
    paid=AsyncMock(side_effect=AssertionError('Preparation must not call Jev'))
    monkeypatch.setattr(main,'extract',extract)
    monkeypatch.setattr(main,'paid_call',paid)
    asyncio.run(main.prepare_video('v',asyncio.Event()))
    assert main.db.all('SELECT id FROM frames ORDER BY ordinal')==original
    assert all(frame['status']=='ready' and frame['error'] is None for frame in main.db.all('SELECT * FROM frames'))
    assert main.db.one('SELECT COUNT(*) AS n FROM match_cache')['n']==3
    assert main.spent_tokens()==2500
    assert main.get_video('v')['status']=='ready'
    extract.assert_not_awaited()
    paid.assert_not_awaited()


def test_missing_saved_frame_is_regenerated_before_ready(api,monkeypatch,tmp_path):
    source=tmp_path/'source.mp4'
    source.write_bytes(b'video')
    main.db.run("INSERT INTO videos(id,name,source,status) VALUES('v','Test',?,'paused')",(str(source),))
    missing=tmp_path/'missing.jpg'
    main.db.save_frames('v',[dict(timestamp=0,path=missing)])
    old_id=main.db.one('SELECT id FROM frames')['id']
    main.db.run('INSERT INTO match_cache(frame_id,query,model,score) VALUES(?,?,?,?)',(old_id,'cached',main.MODEL,.98))
    replacement=tmp_path/'regenerated.jpg'
    replacement.write_bytes(b'frame')
    monkeypatch.setattr(main,'probe',AsyncMock(return_value=dict(duration=2,fps=30,width=320,height=240)))
    monkeypatch.setattr(main,'ensure_playback',AsyncMock())
    extract=AsyncMock(return_value=[dict(timestamp=0,path=replacement)])
    monkeypatch.setattr(main,'extract',extract)
    asyncio.run(main.prepare_video('v',asyncio.Event()))
    frame=main.db.one('SELECT * FROM frames')
    assert frame['id']==old_id
    assert frame['path']==str(replacement)
    assert frame['status']=='ready'
    assert main.get_video('v')['status']=='ready'
    assert main.db.one('SELECT COUNT(*) AS n FROM match_cache')['n']==0
    extract.assert_awaited_once()


def test_inflight_requests_reserve_budget_and_success_records_actual_usage(api,monkeypatch):
    reserve_cost=5*65536*main.PRICE/1_000_000
    monkeypatch.setattr(main,'BUDGET',reserve_cost*1.5)

    async def scenario():
        started=asyncio.Event()
        release=asyncio.Event()

        async def provider_call():
            started.set()
            await release.wait()
            return {'input_tokens':1200}

        first=asyncio.create_task(main.paid_call('search',provider_call))
        await started.wait()
        with pytest.raises(ValueError,match='spending limit'):
            await main.paid_call('search',provider_call)
        release.set()
        await first

    asyncio.run(scenario())
    assert main.spent_tokens()==1200
    assert main.db.one('SELECT COUNT(*) AS n FROM budget_reservations')['n']==0


def test_uncertain_billed_failure_does_not_release_budget_for_another_call(api,monkeypatch):
    """A timeout can follow provider billing; a hard cap must retain that allowance."""
    reserve_cost=5*65536*main.PRICE/1_000_000
    monkeypatch.setattr(main,'BUDGET',reserve_cost*1.5)
    ambiguous_call=AsyncMock(side_effect=TimeoutError('Response lost after provider accepted request'))
    next_call=AsyncMock(return_value={'input_tokens':100})

    async def scenario():
        with pytest.raises(TimeoutError):
            await main.paid_call('search',ambiguous_call)
        with pytest.raises(ValueError,match='spending limit'):
            await main.paid_call('search',next_call)

    asyncio.run(scenario())
    next_call.assert_not_awaited()


def test_deleting_video_keeps_spend_ledger_and_removes_cached_frames(api):
    main.db.run("INSERT INTO videos(id,name,source,status) VALUES('v','Test','source.mp4','ready')")
    main.db.save_frames('v',[dict(timestamp=0,path='0.jpg')])
    frame=main.db.one('SELECT id FROM frames WHERE video_id=?',('v',))
    main.db.run('INSERT INTO match_cache(frame_id,query,model,score) VALUES(?,?,?,?)',(frame['id'],'car',main.MODEL,.9))
    main.db.add_usage('index',2500,'v')
    assert api.delete('/api/videos/v').status_code==200
    assert main.spent_tokens()==2500
    assert main.db.one('SELECT COUNT(*) AS n FROM frames')['n']==0
    assert main.db.one('SELECT COUNT(*) AS n FROM match_cache')['n']==0


def test_interrupted_download_does_not_remain_active_after_restart(tmp_path):
    database=Database(tmp_path)
    database.run("INSERT INTO videos(id,name,source,status,stage) VALUES('v','URL import','missing.mp4','downloading','Downloading video')")
    database.close()
    database=Database(tmp_path)
    try:
        row=database.one('SELECT * FROM videos WHERE id=?',('v',))
        assert row['status']=='failed'
        assert row['error'] and ('import' in row['error'].lower() or 'download' in row['error'].lower())
    finally:
        database.close()


def test_deep_search_reuses_durable_matches_and_groups_snippets(api,monkeypatch):
    main.db.run("INSERT INTO videos(id,name,source,status,duration,fps) VALUES('v','Street','source.mp4','ready',10,30)")
    main.db.save_frames('v',[dict(timestamp=t,path=f'{i}.jpg') for i,t in enumerate((0,.333,.666,8))])
    main.db.run("UPDATE frames SET status='ready',probabilities=?",(json.dumps({'car':.9}),))
    frames=main.db.all('SELECT * FROM frames ORDER BY ordinal')
    videos={'v':main.db.one('SELECT * FROM videos WHERE id=?',('v',))}
    fake_client=SimpleNamespace(match_frames=AsyncMock(return_value={'score':.96,'input_tokens':100}),close=AsyncMock())
    monkeypatch.setattr(main,'client',fake_client)

    def new_job():
        return dict(status='running',progress=0,total=0,results=[],frames_checked=0,total_frames=len(frames),
                    cached=0,error=None,input_tokens=0,cost_usd=0,coverage='',_cancel=False)

    first=new_job()
    body=main.SearchRequest(query='red car',mode='deep')
    asyncio.run(main.run_search(first,body,frames,videos))
    assert first['status']=='complete'
    assert first['frames_checked']==4
    assert first['cached']==0
    assert len(first['results'])==2
    assert first['results'][0]['evidence_count']==3
    assert first['results'][1]['start']==6.5
    assert first['results'][1]['end']==10
    assert fake_client.match_frames.await_count==4
    # The frame at eight seconds must not use the distant preceding frame as action evidence.
    assert len(fake_client.match_frames.await_args_list[-1].args[0])==1
    second=new_job()
    asyncio.run(main.run_search(second,body,frames,videos))
    assert second['status']=='complete'
    assert second['cached']==4
    assert second['input_tokens']==0
    assert second['results']==first['results']
    assert fake_client.match_frames.await_count==4
    assert main.spent_tokens()==400


def test_search_always_checks_all_frames_with_fixed_95_percent_match(api,monkeypatch):
    main.db.run("INSERT INTO videos(id,name,source,status,duration,fps) VALUES('v','Street','source.mp4','ready',160,30)")
    # More than the former 48-candidate fast limit, with no taxonomy probabilities.
    main.db.save_frames('v',[dict(timestamp=i*3,path=f'{i}.jpg') for i in range(51)])
    main.db.run("UPDATE frames SET status='ready'")

    async def match(images,query):
        i=int(Path(images[0]).stem)
        return {'score':{0:.949999,1:.95,2:.99}.get(i,.1),'input_tokens':100}

    provider=SimpleNamespace(match_frames=AsyncMock(side_effect=match),
        query_profile=AsyncMock(side_effect=AssertionError('No taxonomy query is needed')),close=AsyncMock())
    monkeypatch.setattr(main,'client',provider)
    # An old browser cannot re-enable fast search or lower the minimum match.
    response=api.post('/api/search',json={'query':'red car','mode':'fast','threshold':0})
    assert response.status_code==202
    result=wait_for(api,f"/api/search/{response.json()['id']}",lambda job:job['status']!='running')
    assert result['status']=='complete'
    assert result['frames_checked']==result['total']==51
    assert provider.match_frames.await_count==51
    assert sorted(snippet['score'] for snippet in result['results'])==[.95,.99]
    assert sorted(snippet['peak_time'] for snippet in result['results'])==[3,6]
    response=api.post('/api/search',json={'query':'red car','threshold':1})
    repeated=wait_for(api,f"/api/search/{response.json()['id']}",lambda job:job['status']!='running')
    assert repeated['status']=='complete'
    assert repeated['cached']==51
    assert repeated['input_tokens']==0
    assert repeated['results']==result['results']
    assert provider.match_frames.await_count==51
    provider.query_profile.assert_not_awaited()
    assert 'mode' not in main.SearchRequest.model_fields
    assert 'threshold' not in main.SearchRequest.model_fields
