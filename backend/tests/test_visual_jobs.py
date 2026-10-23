import hashlib
import time

from test_workflow import client, project, upload, claim, completion, AUTH


def setup(client):
    rid = upload(client,project(client))
    task = claim(client); headers,body = completion(client,task)
    assert client.post(f'/api/worker/jobs/{task["job_id"]}/complete',headers=headers,json=body).status_code==200
    with client.app.state.store.db() as db:
        db.execute("INSERT INTO artifacts VALUES(?, 'frame-0000.jpg', 0, 'frame', 'synthetic-not-read')",(rid,))
    snap = client.get(f'/api/recordings/{rid}/evidence').json()
    return rid, {'reference':{'recording_id':rid,'source_sha256':snap['source_sha256'],
        'interval':{'start':0,'end':1},'versions':snap['versions']},
        'recipe':{'kind':'image','start':.2,'end':.2}}


def visual_claim(client):
    response = client.post('/api/worker/visual/claim',headers=AUTH)
    assert response.status_code==200,response.text
    return response.json()


def manifest(data):
    return dict(filename='image.jpg',kind='image',requested_start=.2,requested_end=.2,
        actual_start=.24,actual_end=.24,width=320,height=240,fps=None,
        source_width=320,source_height=240,source_fps=25,rotation=0,
        sha256=hashlib.sha256(data).hexdigest(),bytes=len(data),recipe_version='visual-v1')


def test_visual_publish_integrity_cache_and_ranges(client):
    rid,body = setup(client); endpoint=f'/api/recordings/{rid}/visual-jobs'
    job=client.post(endpoint,json=body).json(); task=visual_claim(client)
    assert job['id']==task['id'] and visual_claim(client) is None
    assert client.post(endpoint,json=body).json()['id']==task['id']
    headers={**AUTH,'X-Lease-Id':task['lease_id']}; base=f'/api/worker/visual/{task["id"]}'
    assert client.get(base+'/source',headers=headers).content==b'synthetic media'
    data=b'\xff\xd8\xff'+b'synthetic-jpeg-fixture'
    assert client.put(base+'/media',headers=headers,content=data).status_code==200
    m=manifest(data); bad={**m,'sha256':'0'*64}
    assert client.post(base+'/complete',headers=headers,json=bad).status_code==422
    assert client.get(f'/api/recordings/{rid}/visual-evidence').json()==[]
    assert client.post(base+'/complete',headers=headers,json=m).status_code==200
    assert client.post(base+'/complete',headers=headers,json=m).status_code==200
    assert client.post(base+'/complete',headers=headers,json={**m,'width':319}).status_code==409
    assets=client.get(f'/api/recordings/{rid}/visual-evidence').json()
    assert len(assets)==1 and assets[0]['manifest']['actual_start']==.24
    response=client.get(assets[0]['url'],headers={'Range':'bytes=0-2'})
    assert response.status_code==206 and response.content==data[:3]
    assert client.get(f'/api/recordings/{rid}/evidence').json()['versions']['extraction']==1
    assert client.get(f'/api/recordings/{rid}/evidence').json()['visual_extraction']=='completed'
    other=upload(client,project(client))
    assert client.get(assets[0]['url'].replace(rid,other)).status_code==404
    assert client.post(endpoint,json=body).json()['state']=='completed'
    with client.app.state.store.db() as db:
        path=db.execute('SELECT path FROM visual_assets WHERE id=?',(task['id'],)).fetchone()[0]
    from pathlib import Path
    Path(path).unlink()
    assert client.get(assets[0]['url']).status_code==410
    assert client.post(endpoint,json=body).json()['state']=='queued'


def test_visual_cancel_expiry_capacity_and_fencing(client):
    rid,body=setup(client); endpoint=f'/api/recordings/{rid}/visual-jobs'
    job=client.post(endpoint,json=body).json(); task=visual_claim(client)
    upload(client,project(client)); assert claim(client) is None
    headers={**AUTH,'X-Lease-Id':task['lease_id']}; base=f'/api/worker/visual/{task["id"]}'
    with client.app.state.store.db() as db:
        db.execute('UPDATE visual_jobs SET lease_until=? WHERE id=?',(time.time()-1,job['id']))
    second=visual_claim(client); assert second['lease_id']!=task['lease_id']
    assert client.get(base+'/source',headers=headers).status_code==409
    assert client.post(endpoint+'/'+job['id']+'/cancel').status_code==200
    assert client.post(base+'/heartbeat',headers={**AUTH,'X-Lease-Id':second['lease_id']},json={'progress':.5,'stage':'test'}).status_code==409
    assert client.post(endpoint,json=body).json()['state']=='queued'
    third=visual_claim(client)
    with client.app.state.store.db() as db:
        db.execute('UPDATE visual_jobs SET lease_until=?,attempts=3 WHERE id=?',(time.time()-1,job['id']))
    assert visual_claim(client) is None
    assert client.get(endpoint).json()[0]['state']=='failed'


def test_visual_bounds_source_missing_and_audio_only(client):
    rid,body=setup(client); endpoint=f'/api/recordings/{rid}/visual-jobs'
    for recipe in ({'start':float('inf'),'end':1},{'start':1,'end':1},
                   {'kind':'clip','start':0,'end':31},{'start':0,'end':0,'max_width':9000},
                   {'start':0,'end':0,'crop':{'x':.9,'y':0,'width':.2,'height':1}},
                   {'start':0,'end':0,'url':'https://example.com'}):
        # JSON has no non-finite values; exercise that case through raw content.
        if recipe.get('start')==float('inf'):
            import json
            response=client.post(endpoint,content=json.dumps({**body,'recipe':recipe}),headers={'Content-Type':'application/json'})
        else: response=client.post(endpoint,json={**body,'recipe':recipe})
        assert response.status_code==422,response.text
    assert client.get(f'/api/recordings/{rid}/source').status_code==405
    source=client.app.state.store.root/'sources'/rid; source.unlink()
    assert client.post(endpoint,json=body).status_code==409
    source.write_bytes(b'synthetic media')
    with client.app.state.store.db() as db: db.execute("DELETE FROM artifacts WHERE recording_id=? AND kind='frame'",(rid,))
    assert client.post(endpoint,json=body).status_code==409


def test_visual_byte_disk_queue_limits(client,monkeypatch):
    rid,body=setup(client); endpoint=f'/api/recordings/{rid}/visual-jobs'
    import argon_recordings.visual_jobs as module
    monkeypatch.setattr(module.shutil,'disk_usage',lambda _:type('Disk',(),{'free':1})())
    assert client.post(endpoint,json=body).status_code==507
    monkeypatch.undo()
    job=client.post(endpoint,json=body).json(); task=visual_claim(client)
    headers={**AUTH,'X-Lease-Id':task['lease_id'],'Content-Length':str(module.MAX_BYTES+1)}
    assert client.put(f'/api/worker/visual/{task["id"]}/media',headers=headers,content=b'x').status_code==413
    with client.app.state.store.db() as db:
        for i in range(8):
            db.execute("INSERT INTO visual_jobs(id,recording_id,source_sha256,recipe,cache_key,state,created) VALUES(?,?,?,?,?,'queued',?)",(f'synthetic-{i}',rid,'0'*64,'{}',f'synthetic-{i}',time.time()))
    body['recipe']['start']=body['recipe']['end']=.3
    assert client.post(endpoint,json=body).status_code==429


def test_visual_isolated_worker_identity_and_restart(client):
    from fastapi.testclient import TestClient
    from argon_recordings.api import create_app,Settings
    rid,body=setup(client)
    client.post(f'/api/recordings/{rid}/visual-jobs',json=body)
    settings=Settings(client.app.state.store.root,worker_tokens={'one':'worker-one','two':'worker-two'},reserve=0)
    with TestClient(create_app(settings)) as isolated:
        auth={'Authorization':'Bearer worker-one'}
        task=isolated.post('/api/worker/visual/claim',headers=auth).json()
        base=f'/api/worker/visual/{task["id"]}'
        wrong={'Authorization':'Bearer worker-two','X-Lease-Id':task['lease_id']}
        assert isolated.get(base+'/source',headers=wrong).status_code==409
        assert isolated.put(base+'/media',headers=wrong,content=b'no').status_code==409
        assert isolated.post(base+'/fail',headers=wrong,json={'message':'no'}).status_code==409
    with TestClient(create_app(settings)) as restarted:
        assert restarted.post('/api/worker/visual/claim',headers=auth).json() is None
        headers={**auth,'X-Lease-Id':task['lease_id']}
        assert restarted.get(base+'/source',headers=headers).content==b'synthetic media'
        assert restarted.post(base+'/heartbeat',headers=headers,json={'progress':.7,'stage':'test'}).status_code==200
