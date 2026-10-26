"""Analysis durability, publication fences, combined budgets and manual precedence."""
import hashlib
import shutil
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from test_workflow import client, project, upload, claim, AUTH
from test_visual_jobs import setup, visual_claim
from test_accounts import team, login, invited


def enqueue(client):
    rid, body = setup(client)
    path = f'/api/recordings/{rid}/analysis-jobs'
    response = client.post(path,json={'reference':body['reference']})
    assert response.status_code == 200,response.text
    return rid, path, response.json(), body


def result():
    return dict(status='complete',partial_reason=None,analysis_recipe_version='visual-analysis-v1',
        extraction_recipe_version='visual-v1',requested_interval={'start':0,'end':1},
        scanned_interval={'start':0,'end':1},decoded_interval={'start':0,'end':1},
        proposals=[dict(type='image',source_start=t,source_end=t,timestamp=t,score=0,
                        reason='Sparse reference',metrics={},recipe_version='visual-analysis-v1',
                        extraction_recipe_version='visual-v1',fallback=True) for t in (.2,.8)],
        counts={'detected_events':0,'cursor_suppressed':0,'deduplicated_static':0},
        budgets={'frames_decoded':2,'frame_limit':602,'analysis_bytes':256*144*2,
                 'analysis_byte_limit':22228992,'elapsed_seconds':.1,'wall_seconds_limit':45,
                 'analysis_width':256,'analysis_height':144,'sample_fps':2})


def analysis_claim(client,auth=AUTH):
    response=client.post('/api/worker/analysis/claim',headers=auth)
    assert response.status_code==200,response.text
    return response.json()


def publish(client):
    task=analysis_claim(client); base=f'/api/worker/analysis/{task["id"]}'
    headers={**AUTH,'X-Lease-Id':task['lease_id']}
    response=client.post(base+'/complete',headers=headers,json=result())
    assert response.status_code==200,response.text
    return task,base,headers


def decision(client,rid,choice):
    state=client.get(f'/api/recordings/{rid}/evidence').json()
    return {'choice':choice,'expected_revision':state['versions']['annotations'],
            'expected_source_sha256':state['source_sha256']}


def test_review_cache_stale_edit_undo_and_rejection(client):
    rid,path,job,body=enqueue(client)
    task,base,headers=publish(client)
    assert task['id']==job['id']
    assert client.post(base+'/complete',headers=headers,json=result()).status_code==200
    assert client.post(path,json={'reference':body['reference']}).json()['state']=='completed'
    before=client.get(f'/api/recordings/{rid}/transcript').json()
    choice=decision(client,rid,'accept')
    reviewed=client.post(path+'/'+job['id']+'/proposals/0',json=choice)
    assert reviewed.status_code==200,reviewed.text
    proposal=reviewed.json()['proposals'][0]
    assert proposal['mark_active'] and proposal['choice']=='accept'
    assert client.post(path+'/'+job['id']+'/proposals/1',json=choice).status_code==409
    undo=decision(client,rid,'accept');undo.pop('choice')
    assert client.post(f'/api/recordings/{rid}/marks/undo',json=undo).status_code==200
    repeat=client.post(path+'/'+job['id']+'/proposals/0',json=decision(client,rid,'accept'))
    assert repeat.status_code==200 and not repeat.json()['proposals'][0]['mark_active']
    assert client.get(f'/api/recordings/{rid}/marks').json()['marks']==[]
    rejection=decision(client,rid,'reject')
    assert client.post(path+'/'+job['id']+'/proposals/1',json=rejection).status_code==200
    assert client.get(f'/api/recordings/{rid}/evidence').json()['versions']['annotations']==rejection['expected_revision']+1
    assert client.post(path+'/'+job['id']+'/proposals/1',json=decision(client,rid,'accept')).status_code==409
    assert client.get(f'/api/recordings/{rid}/transcript').json()==before


@pytest.mark.parametrize('mark',[
    {'interval':{'start':.2,'end':.2},'pinned':True},
    {'interval':{'start':.1,'end':.4},'policy':'motion'},
])
def test_pinned_and_manual_policies_win_without_changes(client,mark):
    rid,path,job,_=enqueue(client);publish(client)
    write=decision(client,rid,'accept');write.pop('choice');write['mark']=mark
    assert client.post(f'/api/recordings/{rid}/marks',json=write).status_code==201
    before=client.get(f'/api/recordings/{rid}/marks').json()
    detail=client.get(path+'/'+job['id']).json()
    assert detail['proposals'][0]['manual_conflict']
    assert client.post(path+'/'+job['id']+'/proposals/0',json=decision(client,rid,'accept')).status_code==409
    assert client.get(f'/api/recordings/{rid}/marks').json()==before
    assert client.post(path+'/'+job['id']+'/proposals/0',json=decision(client,rid,'reject')).status_code==200


def test_analysis_lease_capacity_expiry_cancel_and_source_fences(client):
    rid,path,job,body=enqueue(client);task=analysis_claim(client)
    upload(client,project(client));assert claim(client) is None
    base=f'/api/worker/analysis/{task["id"]}';headers={**AUTH,'X-Lease-Id':task['lease_id']}
    assert client.get(base+'/source',headers=headers).content==b'synthetic media'
    with client.app.state.store.db() as db: db.execute('UPDATE analysis_jobs SET lease_until=? WHERE id=?',(time.time()-1,job['id']))
    newer=analysis_claim(client);assert newer['lease_id']!=task['lease_id']
    assert client.post(base+'/complete',headers=headers,json=result()).status_code==409
    assert client.post(path+'/'+job['id']+'/cancel').status_code==200
    assert client.post(base+'/heartbeat',headers={**AUTH,'X-Lease-Id':newer['lease_id']},json={'progress':.5,'stage':'test'}).status_code==409
    assert client.post(path,json={'reference':body['reference']}).json()['state']=='queued'
    latest=analysis_claim(client)
    with client.app.state.store.db() as db: db.execute('UPDATE recordings SET sha256=? WHERE id=?',('e'*64,rid))
    assert client.post(base+'/complete',headers={**AUTH,'X-Lease-Id':latest['lease_id']},json=result()).status_code==409


def test_source_and_publication_bounds_and_shared_queue_limits(client):
    rid,path,job,body=enqueue(client);task=analysis_claim(client)
    base=f'/api/worker/analysis/{job["id"]}';headers={**AUTH,'X-Lease-Id':task['lease_id']}
    bad=result();bad['budgets']['frames_decoded']=603
    assert client.post(base+'/complete',headers=headers,json=bad).status_code==422
    bad=result();bad['scanned_interval']['end']=2
    assert client.post(base+'/complete',headers=headers,json=bad).status_code==422
    bad=result();bad['proposals'][0]['source_end']=.7
    assert client.post(base+'/complete',headers=headers,json=bad).status_code==422
    bad=result();bad['requested_interval']['end']=.9;bad['scanned_interval']['end']=.9;bad['decoded_interval']['end']=.9
    assert client.post(base+'/complete',headers=headers,json=bad).status_code==422
    with client.app.state.store.db() as db:
        for i in range(7):
            db.execute("INSERT INTO visual_jobs(id,recording_id,source_sha256,recipe,cache_key,state,created) VALUES(?,?,?,?,?,'queued',?)",(f'fill-{i}',rid,'0'*64,'{}',f'fill-{i}',time.time()))
    body['reference']['interval']['end']=.9
    assert client.post(path,json={'reference':body['reference']}).status_code==429
    assert client.post(f'/api/recordings/{rid}/visual-jobs',json=body).status_code==429
    source=client.app.state.store.root/'sources'/rid;source.unlink()
    assert client.post(path,json={'reference':body['reference']}).status_code==409


def test_project_roles_csrf_and_body_budget(team):
    app,owner,manager,uid=team;headers=login(owner)
    pid=owner.post('/api/projects',headers=headers,json={'name':'Analysis team'}).json()['id']
    rid=owner.post(f'/api/projects/{pid}/recordings',headers=headers,json={'title':'Private video','filename':'demo.mp4'}).json()['id']
    source=manager.store.root/'sources'/rid;source.write_bytes(b'abc')
    with manager.store.db() as db:
        db.execute("UPDATE recordings SET sha256=?,bytes=3,duration=1,status='ready' WHERE id=?",(hashlib.sha256(b'abc').hexdigest(),rid))
        db.execute("INSERT INTO artifacts VALUES(?,'frame-0000.jpg',0,'frame','synthetic-not-read')",(rid,))
    guest,gh=invited(app,manager,uid,'analysis-viewer@example.com')
    outsider,oh=invited(app,manager,uid,'analysis-outsider@example.com')
    try:
        assert owner.put(f'/api/projects/{pid}/members',headers=headers,json={'email':'analysis-viewer@example.com','role':'viewer'}).status_code==200
        path=f'/api/recordings/{rid}/analysis-jobs'
        state=owner.get(f'/api/recordings/{rid}/evidence').json()
        body={'reference':{'recording_id':rid,'source_sha256':state['source_sha256'],'versions':state['versions'],'interval':{'start':0,'end':1}}}
        assert owner.post(path,json=body).status_code==403
        created=owner.post(path,headers=headers,json=body);assert created.status_code==200,created.text
        jid=created.json()['id']
        assert guest.get(path).status_code==200 and guest.get(path+'/'+jid).status_code==200
        assert guest.post(path,headers=gh,json=body).status_code==403
        assert guest.post(path+'/'+jid+'/cancel',headers=gh).status_code==403
        assert guest.post(path+'/'+jid+'/proposals/0',headers=gh,json={}).status_code==403
        assert outsider.get(path,headers=oh).status_code==404
        assert owner.post(path,headers={**headers,'Content-Type':'application/json'},content=b'x'*65537).status_code==413
    finally:
        guest.close();outsider.close()


def test_isolated_worker_restart_and_retry_exhaustion(client):
    from argon_recordings.api import create_app,Settings
    rid,path,job,_=enqueue(client)
    settings=Settings(client.app.state.store.root,worker_tokens={'one':'worker-one','two':'worker-two'},reserve=0)
    auth={'Authorization':'Bearer worker-one'}
    with TestClient(create_app(settings)) as isolated:
        task=analysis_claim(isolated,auth);base=f'/api/worker/analysis/{job["id"]}'
        wrong={'Authorization':'Bearer worker-two','X-Lease-Id':task['lease_id']}
        assert isolated.get(base+'/source',headers=wrong).status_code==409
        assert isolated.post(base+'/fail',headers=wrong,json={'message':'no'}).status_code==409
    with TestClient(create_app(settings)) as restarted:
        assert analysis_claim(restarted,auth) is None
        headers={**auth,'X-Lease-Id':task['lease_id']}
        assert restarted.get(base+'/source',headers=headers).status_code==200
        with restarted.app.state.store.db() as db: db.execute('UPDATE analysis_jobs SET lease_until=?,attempts=3 WHERE id=?',(time.time()-1,job['id']))
        assert analysis_claim(restarted,auth) is None
        assert restarted.get(path).json()[0]['state']=='failed'


@pytest.mark.skipif(not shutil.which('ffmpeg'),reason='Actual FFmpeg environment required')
def test_real_worker_analysis_review_and_transcript_preservation(client,tmp_path):
    from test_visual_analysis_ffmpeg import make_demo
    from argon_recordings.analysis_worker import run
    rid,body=setup(client);source=client.app.state.store.root/'sources'/rid
    data=make_demo(tmp_path/'demo.mp4').read_bytes();source.write_bytes(data)
    with client.app.state.store.db() as db:
        db.execute('UPDATE recordings SET sha256=?,bytes=?,duration=30 WHERE id=?',(hashlib.sha256(data).hexdigest(),len(data),rid))
    state=client.get(f'/api/recordings/{rid}/evidence').json()
    body['reference'].update(source_sha256=state['source_sha256'],interval={'start':0,'end':30})
    path=f'/api/recordings/{rid}/analysis-jobs'
    job=client.post(path,json={'reference':body['reference']}).json()
    before=client.get(f'/api/recordings/{rid}/transcript').json()
    client.headers['Authorization']='Bearer synthetic-test-only';transcriber=SimpleNamespace(model=object())
    assert run(client,transcriber,tmp_path) and transcriber.model is None
    detail=client.get(path+'/'+job['id']).json()
    assert detail['state']=='completed',detail
    assert detail['result']['status']=='complete' and detail['proposals']
    assert any(p['type']=='clip' for p in detail['proposals'])
    assert client.post(path+'/'+job['id']+'/proposals/0',json=decision(client,rid,'accept')).status_code==200
    assert len(client.get(f'/api/recordings/{rid}/marks').json()['marks'])==1
    assert client.get(f'/api/recordings/{rid}/transcript').json()==before
    assert not list(tmp_path.glob('recordings-analysis-*'))
    assert not run(client,transcriber,tmp_path)
