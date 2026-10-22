"""Synthetic inference validates durable distributed protocol, not model accuracy."""
import hashlib
import io
import time
import wave
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from fastapi.testclient import TestClient

from argon_recordings.api import Settings, create_app
from argon_recordings.chunk_audio import plan, reconcile
from argon_recordings.contracts import Segment
from argon_recordings.worker import crop_audio, run_once
from test_workflow import project, upload

TOKENS = {'one':'synthetic-one','two':'synthetic-two'}


def auth(worker, lease=None):
    value = {'Authorization':'Bearer '+TOKENS[worker]}
    if lease:
        value['X-Lease-Id'] = lease
    return value


def pcm(duration):
    output = io.BytesIO()
    with wave.open(output,'wb') as audio:
        audio.setnchannels(1); audio.setsampwidth(2); audio.setframerate(16000)
        audio.writeframes(b'\0\0'*round(16000*duration))
    return output.getvalue()


def word(start,end,text):
    return dict(start=start,end=end,word=text,probability=.9)


def result(start,end,text):
    return {'segments':[dict(start=start,end=end,text=text,words=[word(start,end,text)])]}


@pytest.fixture
def prepared(tmp_path):
    app = create_app(Settings(tmp_path/'data',worker_tokens=TOKENS,worker_limit=2,reserve=0))
    with TestClient(app) as client:
        rid = upload(client,project(client))
        job = client.post('/api/worker/tasks/claim',headers=auth('one')).json()
        audio = tmp_path/'audio.wav'; audio.write_bytes(pcm(370))
        _, chunks = plan(audio)
        headers = auth('one',job['lease_id']); jid = job['job_id']
        assert client.put(f'/api/worker/jobs/{jid}/artifacts/audio.wav',headers=headers,content=audio.read_bytes()).status_code == 200
        manifest = []
        for index,item in enumerate(chunks):
            path = tmp_path/f'{index}.wav'; crop_audio(audio,path,item['start'],item['end']); payload=path.read_bytes()
            assert client.put(f'/api/worker/jobs/{jid}/chunks/{index}',headers=headers,content=payload).status_code == 200
            manifest.append({**item,'bytes':len(payload),'sha256':hashlib.sha256(payload).hexdigest()})
        body = {'duration':370,'artifacts':[{'name':'audio.wav','kind':'audio'}],'chunks':manifest}
        response = client.post(f'/api/worker/jobs/{jid}/prepare',headers=headers,json=body)
        assert response.status_code == 200,response.text
        assert client.post(f'/api/worker/jobs/{jid}/prepare',headers=headers,json=body).status_code == 200
        yield client,rid,jid,headers,body


def claim(client,worker):
    response=client.post('/api/worker/tasks/claim',headers=auth(worker))
    assert response.status_code==200,response.text
    return response.json()


def test_parallel_capacity_fencing_atomic_publication(prepared):
    client,rid,jid,headers,body=prepared
    first=claim(client,'one'); second=claim(client,'two')
    assert first['id'] != second['id']
    assert claim(client,'one') is None
    assert client.post('/api/worker/corrections/claim',headers=auth('two')).json() is None
    assert client.get(f"/api/worker/chunks/{first['id']}/audio",headers=auth('two',first['lease_id'])).status_code==403
    assert client.get(f'/api/recordings/{rid}/media/audio.wav').status_code==404
    assert client.get(f'/api/recordings/{rid}/transcript').json()['segments']==[]
    assert client.put(f'/api/worker/jobs/{jid}/chunks/0',headers=headers,content=b'replace').status_code==409
    for worker,job in [('one',first),('two',second)]:
        url=f"/api/worker/chunks/{job['id']}/complete"
        # Word at the center of the owned core, expressed in local crop time.
        start=(job['core_start']+job['core_end'])/2-job['start']
        payload=result(start,start+.2,' hola')
        assert client.post(url,headers=auth(worker,job['lease_id']),json=payload).status_code==200
        assert client.post(url,headers=auth(worker,job['lease_id']),json=payload).status_code==200
        assert client.post(url,headers=auth(worker,job['lease_id']),json=result(start,start+.2,' changed')).status_code==409
    while job:=claim(client,'one'):
        start=(job['core_start']+job['core_end'])/2-job['start']
        assert client.post(f"/api/worker/chunks/{job['id']}/complete",headers=auth('one',job['lease_id']),json=result(start,start+.2,' hola')).status_code==200
    recording=client.get(f'/api/recordings/{rid}').json()
    assert recording['status']=='ready' and recording['job']['progress']==1
    segments=client.get(f'/api/recordings/{rid}/transcript').json()['segments']
    assert len(segments)==5
    assert [s['start'] for s in segments]==sorted(s['start'] for s in segments)
    assert client.get(f'/api/recordings/{rid}/media/audio.wav').status_code==200
    with client.app.state.store.db() as db:
        assert db.execute('SELECT count(*) FROM artifacts WHERE recording_id=?',(rid,)).fetchone()[0]==1


def test_expiry_reclaims_only_lost_chunk(prepared):
    client,rid,jid,headers,body=prepared
    old=claim(client,'one')
    with client.app.state.store.db() as db:
        db.execute('UPDATE chunks SET lease_until=? WHERE id=?',(time.time()-1,old['id']))
    new=claim(client,'two')
    assert new['id']==old['id'] and new['lease_id']!=old['lease_id']
    url=f"/api/worker/chunks/{old['id']}"
    assert client.get(url+'/audio',headers=auth('one',old['lease_id'])).status_code in (403,409)
    assert client.post(url+'/complete',headers=auth('one',old['lease_id']),json=result(0,1,'stale')).status_code in (403,409)
    assert client.get(url+'/audio',headers=auth('two',new['lease_id'])).status_code==200
    # Independent Store reopening keeps preparation and result/lease identity.
    from argon_recordings.store import Store
    reopened=Store(client.app.state.store.root)
    with reopened.db() as db:
        assert db.execute('SELECT lease_id FROM chunks WHERE id=?',(old['id'],)).fetchone()[0]==new['lease_id']


def test_overlap_preserves_intentional_repeat_and_removes_shifted_duplicate():
    rows=[dict(start=0,end=182,core_start=0,core_end=180,boundary=False,segments=result(177.7,178.1,' yes')['segments']),
          dict(start=178,end=360,core_start=180,core_end=360,boundary=False,segments=result(182.1,182.3,' yes')['segments']),
          dict(start=168,end=192,core_start=178,core_end=182,boundary=True,segments=[dict(start=177.8,end=182.2,text='yes yes yes',words=[word(177.8,178.3,' yes'),word(180,180.4,' yes'),word(181.7,182.2,' yes')])])]
    segments=reconcile(rows)
    words=[w for s in segments for w in s.words]
    assert len(words)==4  # only overlapping 177.8 duplicate removed; repeats remain
    assert len([w for w in words if w.start>=180])==3


def test_empty_boundary_cannot_drop_original_words():
    rows=[dict(start=0,end=182,core_start=0,core_end=180,boundary=False,segments=result(179,179.3,' keep')['segments']),
          dict(start=168,end=192,core_start=178,core_end=182,boundary=True,segments=[])]
    with pytest.raises(ValueError,match='no words'):
        reconcile(rows)


def test_pause_aware_cut_near_quiet_window(tmp_path):
    path=tmp_path/'tone.wav'
    with wave.open(str(path),'wb') as audio:
        audio.setnchannels(1);audio.setsampwidth(2);audio.setframerate(16000)
        audio.writeframes(b'\x10\x10'*16000*178+b'\0\0'*16000+b'\x10\x10'*16000*191)
    duration,chunks=plan(path)
    assert duration==370 and 178<=chunks[0]['core_end']<179
    assert chunks[0]['end']==chunks[0]['core_end']+2
    assert chunks[-1]['boundary'] and chunks[3]['end']-chunks[3]['start']==24


def test_concurrent_claims_one_per_identity_and_global_limit(prepared):
    client,*_=prepared
    with ThreadPoolExecutor(max_workers=4) as pool:
        values=list(pool.map(lambda worker:(worker,claim(client,worker)),['one','one','two','two']))
    winners=[(worker,job) for worker,job in values if job]
    assert len(winners)==2 and {worker for worker,_ in winners}=={'one','two'}
    assert len({job['id'] for _,job in winners})==2


def test_preparation_rejects_audio_mismatch_and_coverage_gaps(prepared):
    client,rid,jid,headers,body=prepared
    other=upload(client,project(client,'Other'))
    job=client.app.state.store.claim('one'); jid=job['job_id']; headers=auth('one',job['lease_id'])
    payload=pcm(30)
    assert client.put(f'/api/worker/jobs/{jid}/artifacts/audio.wav',headers=headers,content=payload).status_code==200
    mutated=payload[:-2]+b'\x01\x01'
    assert client.put(f'/api/worker/jobs/{jid}/chunks/0',headers=headers,content=mutated).status_code==200
    chunk=dict(start=0,end=30,core_start=0,core_end=30,boundary=False,bytes=len(mutated),sha256=hashlib.sha256(mutated).hexdigest())
    manifest=dict(duration=30,artifacts=[dict(name='audio.wav',kind='audio')],chunks=[chunk])
    response=client.post(f'/api/worker/jobs/{jid}/prepare',headers=headers,json=manifest)
    assert response.status_code==422 and 'retained source' in response.text
    assert client.put(f'/api/worker/jobs/{jid}/chunks/0',headers=headers,content=payload).status_code==200
    chunk['sha256']=hashlib.sha256(payload).hexdigest();chunk['core_start']=1
    assert client.post(f'/api/worker/jobs/{jid}/prepare',headers=headers,json=manifest).status_code==422
    chunk['core_start']=0
    tiny={**manifest,'chunks':[{**chunk,'core_end':15},{**chunk,'core_start':15}]}
    response=client.post(f'/api/worker/jobs/{jid}/prepare',headers=headers,json=tiny)
    assert response.status_code==422 and 'pause window' in response.text
    assert client.post(f'/api/worker/jobs/{jid}/prepare',headers=headers,json=manifest).status_code==200
    with client.app.state.store.db() as db:
        assert db.execute('SELECT count(*) FROM chunks WHERE job_id=?',(jid,)).fetchone()[0]==1


def test_boundary_omission_blocks_publication_and_retry_reuses_cores(prepared):
    client,rid,jid,headers,body=prepared
    while job:=claim(client,'one'):
        if job['boundary']:
            payload={'segments':[]}
        elif job['ordinal']==0:
            payload=result(179,179.3,' keep')
        else:
            payload={'segments':[]}
        response=client.post(f"/api/worker/chunks/{job['id']}/complete",headers=auth('one',job['lease_id']),json=payload)
        assert response.status_code==200,response.text
    recording=client.get(f'/api/recordings/{rid}').json()
    assert recording['status']=='failed' and 'no words' in recording['error']
    assert client.get(f'/api/recordings/{rid}/transcript').json()['segments']==[]
    assert client.post(f'/api/recordings/{rid}/retry').status_code==200
    retry=claim(client,'one')
    assert retry['boundary'] and retry['attempts']==0
    with client.app.state.store.db() as db:
        assert db.execute("SELECT count(*) FROM chunks WHERE job_id=? AND boundary=0 AND state='completed'",(jid,)).fetchone()[0]==3


@pytest.mark.parametrize('words',[
    [word(0,600,' same')]*1000,
    [word(0,1,' same')]*1000,
    [word(0,0,' same')]*1000,
    [word(0,1,' same'),word(.1,.5,' again')],
])
def test_reconciliation_rejects_amplifying_word_timelines(words):
    row=dict(start=0,end=600,core_start=0,core_end=600,boundary=False,
             segments=[dict(start=0,end=600,text='same',words=words)])
    with pytest.raises(ValueError):
        reconcile([row])


def test_reconciliation_allows_small_neighbor_word_overlap():
    words=[word(0,.3,' yes'),word(.2,.5,' yes'),word(.5,.7,' again')]
    row=dict(start=0,end=1,core_start=0,core_end=1,boundary=False,
             segments=[dict(start=0,end=1,text='yes yes again',words=words)])
    assert len(reconcile([row])[0].words)==3


def test_api_rejects_amplifying_result_without_accepting_lease(prepared):
    client,*_=prepared
    job=claim(client,'one')
    payload={'segments':[dict(start=0,end=180,text='same',words=[word(0,180,' same')]*1000)]}
    response=client.post(f"/api/worker/chunks/{job['id']}/complete",headers=auth('one',job['lease_id']),json=payload)
    assert response.status_code==422
    with client.app.state.store.db() as db:
        row=db.execute('SELECT state,result FROM chunks WHERE id=?',(job['id'],)).fetchone()
        assert row['state']=='running' and row['result'] is None


def test_child_heartbeat_refreshes_truthful_aggregate_coverage(prepared):
    client,rid,jid,*_=prepared
    job=claim(client,'one')
    with client.app.state.store.db() as db:
        db.execute('UPDATE job_progress SET updated=0 WHERE job_id=?',(jid,))
    response=client.post(f"/api/worker/chunks/{job['id']}/heartbeat",headers=auth('one',job['lease_id']),json={'progress':.5,'stage':'Transcribing chunk'})
    assert response.status_code==200
    recording=client.get(f'/api/recordings/{rid}').json()
    assert recording['job']['updated_at']>0
    progress=recording['job']['transcription']
    assert progress['audio_processed_seconds']==0 and progress['audio_total_seconds']==370
    assert progress['eta_seconds'] is None
    payload=result(1,1.2,' done')
    assert client.post(f"/api/worker/chunks/{job['id']}/complete",headers=auth('one',job['lease_id']),json=payload).status_code==200
    progress=client.get(f'/api/recordings/{rid}').json()['job']['transcription']
    assert progress['audio_processed_seconds']==180 and progress['passages']==1
