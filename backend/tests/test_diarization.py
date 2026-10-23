import time

from test_workflow import client, project, upload, claim, completion, AUTH
from argon_recordings.diarization_runtime import MODEL


def setup(client):
    rid = upload(client, project(client))
    task = claim(client)
    headers, result = completion(client, task)
    assert client.post(f'/api/worker/jobs/{task["job_id"]}/complete', headers=headers, json=result).status_code == 200
    snap = client.get(f'/api/recordings/{rid}/evidence').json()
    ref = {'recording_id': rid, 'source_sha256': snap['source_sha256'],
           'interval': {'start': 0, 'end': snap['duration']}, 'versions': snap['versions']}
    return rid, {'reference': ref}


def worker_claim(client):
    response = client.post('/api/worker/diarization/claim', headers=AUTH)
    assert response.status_code == 200, response.text
    return response.json()


def result():
    return {'model': MODEL, 'turns': [{'start': 0, 'end': 1, 'speaker': 'speaker-0'}]}


def test_publish_reuse_and_restart_preserve_transcript(client):
    rid, body = setup(client)
    before = client.get(f'/api/recordings/{rid}/transcript').json()
    job = client.post(f'/api/recordings/{rid}/diarization', json=body).json()
    assert client.post(f'/api/recordings/{rid}/diarization', json=body).json()['id'] == job['id']
    task = worker_claim(client)
    headers = {**AUTH, 'X-Lease-Id': task['lease_id']}
    assert worker_claim(client) is None
    base = f'/api/worker/diarization/{task["id"]}'
    assert client.get(base+'/audio', headers=headers).content[:4] == b'RIFF'
    assert client.post(base+'/complete', headers=headers, json=result()).status_code == 200
    assert client.post(base+'/complete', headers=headers, json=result()).status_code == 200
    changed = result(); changed['model'] = 'different'
    assert client.post(base+'/complete', headers=headers, json=changed).status_code == 409
    assert client.get(f'/api/recordings/{rid}/transcript').json() == before
    assert client.get(f'/api/recordings/{rid}/evidence').json()['versions']['speakers'] == 1
    assert client.get(f'/api/recordings/{rid}/diarization').json()[0]['state'] == 'completed'
    published = client.get(f'/api/recordings/{rid}/speakers').json()
    assert published['turns'] == result()['turns'] and not published['stale']
    from fastapi.testclient import TestClient
    from argon_recordings.api import Settings, create_app
    with TestClient(create_app(Settings(client.app.state.store.root, worker_token='synthetic-test-only', reserve=0))) as restarted:
        assert restarted.get(f'/api/recordings/{rid}/speakers').json() == published
        assert restarted.get(f'/api/recordings/{rid}/diarization').json()[0]['state'] == 'completed'
    with client.app.state.store.db() as db:
        db.execute("UPDATE segments SET text='Edited after diarization' WHERE recording_id=?", (rid,))
    stale = client.get(f'/api/recordings/{rid}/speakers').json()
    assert stale['stale'] and stale['turns'] == []


def test_cancel_and_new_options_fence_old_workers(client):
    rid, body = setup(client)
    endpoint = f'/api/recordings/{rid}/diarization'
    first = client.post(endpoint, json=body).json()
    task = worker_claim(client)
    headers = {**AUTH, 'X-Lease-Id': task['lease_id']}
    assert client.post(f'{endpoint}/{first["id"]}/cancel').status_code == 200
    assert client.post(f'/api/worker/diarization/{first["id"]}/complete', headers=headers, json=result()).status_code == 409
    retry = client.post(endpoint, json=body).json()
    assert retry['id'] == first['id'] and retry['state'] == 'queued'
    worker_claim(client)
    body['options'] = {'min_speakers': 2, 'max_speakers': 3}
    second = client.post(endpoint, json=body).json()
    assert second['id'] != first['id']
    assert client.post(f'/api/worker/diarization/{first["id"]}/heartbeat', headers=headers,
                       json={'progress': .5, 'stage': 'test'}).status_code == 409


def test_transcript_drift_and_invalid_turns_rejected(client):
    rid, body = setup(client)
    client.post(f'/api/recordings/{rid}/diarization', json=body)
    task = worker_claim(client)
    base = f'/api/worker/diarization/{task["id"]}'
    headers = {**AUTH, 'X-Lease-Id': task['lease_id']}
    invalid = result(); invalid['turns'][0]['end'] = 2
    assert client.post(base+'/complete', headers=headers, json=invalid).status_code == 422
    with client.app.state.store.db() as db:
        db.execute("UPDATE segments SET text='Corrected' WHERE recording_id=?", (rid,))
    assert client.post(base+'/complete', headers=headers, json=result()).status_code == 409
    assert client.post(f'/api/recordings/{rid}/diarization', json=body).status_code == 409


def test_expiry_retries_and_aggregate_capacity(client):
    rid, body = setup(client)
    client.post(f'/api/recordings/{rid}/diarization', json=body)
    task = worker_claim(client)
    upload(client, project(client))
    assert claim(client) is None  # speaker enrichment consumes same ASR slot
    with client.app.state.store.db() as db:
        db.execute('UPDATE diarization_jobs SET lease_until=? WHERE id=?', (time.time()-1, task['id']))
    new = worker_claim(client)
    assert new['lease_id'] != task['lease_id']
    headers = {**AUTH, 'X-Lease-Id': task['lease_id']}
    assert client.get(f'/api/worker/diarization/{task["id"]}/audio', headers=headers).status_code == 409
    with client.app.state.store.db() as db:
        db.execute('UPDATE diarization_jobs SET lease_until=0,attempts=3 WHERE id=?', (task['id'],))
    assert worker_claim(client) is None
    assert client.get(f'/api/recordings/{rid}/diarization').json()[0]['state'] == 'failed'


def test_annotation_payload_is_bounded_before_json_decode(client):
    assert client.post('/api/recordings/missing/marks', content=b' ' * 65537).status_code == 413


def test_registered_workers_cannot_read_or_publish_each_others_audio(tmp_path):
    from argon_recordings.api import Settings, create_app
    from fastapi.testclient import TestClient
    from test_workflow import wav
    settings = Settings(tmp_path, worker_tokens={'one': 'test-worker-one', 'two': 'test-worker-two'}, worker_limit=2, reserve=0)
    with TestClient(create_app(settings)) as client:
        rid = upload(client, project(client))
        path = tmp_path/'retained.wav'; path.write_bytes(wav())
        with client.app.state.store.db() as db:
            db.execute("UPDATE recordings SET duration=1,status='ready' WHERE id=?", (rid,))
            db.execute("UPDATE jobs SET state='completed' WHERE recording_id=?", (rid,))
            db.execute('INSERT INTO artifacts VALUES(?,?,?,?,?)', (rid, 'audio.wav', None, 'audio', str(path)))
        snap = client.get(f'/api/recordings/{rid}/evidence').json()
        reference = {'recording_id': rid, 'source_sha256': snap['source_sha256'], 'versions': snap['versions'], 'interval': {'start': 0, 'end': 1}}
        client.post(f'/api/recordings/{rid}/diarization', json={'reference': reference})
        first = {'Authorization': 'Bearer test-worker-one'}
        second = {'Authorization': 'Bearer test-worker-two'}
        task = client.post('/api/worker/diarization/claim', headers=first).json()
        wrong = {**second, 'X-Lease-Id': task['lease_id']}
        base = f'/api/worker/diarization/{task["id"]}'
        assert client.get(base+'/audio', headers=wrong).status_code == 409
        assert client.post(base+'/complete', headers=wrong, json=result()).status_code == 409
        upload(client, project(client))
        assert client.post('/api/worker/tasks/claim', headers=first).json() is None
        assert client.post('/api/worker/tasks/claim', headers=second).json()['kind'] == 'prepare'
