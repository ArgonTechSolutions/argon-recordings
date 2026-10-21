import concurrent.futures
import json
from pathlib import Path
import time
import wave
from unittest.mock import patch

from fastapi.testclient import TestClient
import httpx
import pytest

from argon_recordings.api import Settings, create_app
from argon_recordings.contracts import Segment
from argon_recordings.worker import run_once

AUTH = {'Authorization': 'Bearer synthetic-test-only'}


@pytest.fixture
def client(tmp_path):
    app = create_app(Settings(tmp_path, worker_token='synthetic-test-only', reserve=0))
    with TestClient(app) as client:
        yield client


def project(client, name='Research'):
    response = client.post('/api/projects', json={'name': name})
    assert response.status_code == 201
    return response.json()['id']


def upload(client, pid, data=b'synthetic media', priority='normal'):
    recording = client.post(f'/api/projects/{pid}/recordings', json={'title': 'A useful demo', 'filename': '../never-a-storage-path.mp4', 'language': 'es', 'priority': priority}).json()
    response = client.put(f'/api/recordings/{recording["id"]}/source', content=data)
    assert response.status_code == 200, response.text
    assert response.json()['status'] == 'queued'
    return recording['id']


def claim(client):
    response = client.post('/api/worker/claim', json={'worker': 'synthetic'}, headers=AUTH)
    assert response.status_code == 200
    return response.json()


def wav():
    import io
    target = io.BytesIO()
    with wave.open(target, 'wb') as stream:
        stream.setnchannels(1); stream.setsampwidth(2); stream.setframerate(16000)
        stream.writeframes(b'\0\0' * 16000)
    return target.getvalue()


def completion(client, job):
    headers = dict(AUTH, **{'X-Lease-Id': job['lease_id']})
    response = client.put(f'/api/worker/jobs/{job["job_id"]}/artifacts/audio.wav', content=wav(), headers=headers)
    assert response.status_code == 200
    body = {'duration': 1, 'segments': [{'start': 0, 'end': 1, 'text': 'PostgreSQL deployment is ready.', 'words': []}],
            'artifacts': [{'name': 'audio.wav', 'kind': 'audio', 'time': None}]}
    return headers, body


def test_upload_claim_complete_search_and_idempotency(client):
    pid = project(client)
    rid = upload(client, pid)
    assert client.put(f'/api/recordings/{rid}/source', content=b'overwrite').status_code == 409
    assert client.get(f'/api/recordings/{rid}/source').status_code == 405
    job = claim(client)
    assert job['recording']['id'] == rid
    assert claim(client) is None
    headers, body = completion(client, job)
    url = f'/api/worker/jobs/{job["job_id"]}/complete'
    assert client.post(url, json=body, headers=headers).json()['status'] == 'ready'
    assert client.post(url, json=body, headers=headers).status_code == 200
    assert len(client.get(f'/api/recordings/{rid}/transcript').json()['segments']) == 1
    hits = client.get('/api/search', params={'project_id': pid, 'q': 'PostgreSQL'}).json()
    assert len(hits) == 1 and hits[0]['recording_id'] == rid
    other = project(client, 'Other')
    assert client.get('/api/search', params={'project_id': other, 'q': 'PostgreSQL'}).json() == []
    assert client.get('/api/search', params={'project_id': pid, 'q': '" OR *'}).status_code == 200
    assert client.get(f'/api/recordings/{rid}/media/audio.wav').content == wav()
    assert client.get(f'/api/recordings/{rid}/media/missing.jpg').status_code == 404
    body['segments'][0]['text'] = 'Different publication'
    assert client.post(url, json=body, headers=headers).status_code == 409


def test_expired_lease_cannot_publish_or_heartbeat(client):
    upload(client, project(client))
    old = claim(client)
    with client.app.state.store.db() as db:
        db.execute('UPDATE jobs SET lease_until=? WHERE id=?', (time.time() - 1, old['job_id']))
    new = claim(client)
    assert new['lease_id'] != old['lease_id']
    headers = dict(AUTH, **{'X-Lease-Id': old['lease_id']})
    assert client.post(f'/api/worker/jobs/{old["job_id"]}/heartbeat', json={'progress': .5, 'stage': 'stale'}, headers=headers).status_code == 409
    assert client.put(f'/api/worker/jobs/{old["job_id"]}/artifacts/audio.wav', content=wav(), headers=headers).status_code == 409


def test_upload_limit_and_empty_upload_reset_state(tmp_path):
    with TestClient(create_app(Settings(tmp_path, max_upload=4, reserve=0))) as c:
        pid = project(c)
        rid = c.post(f'/api/projects/{pid}/recordings', json={'title': 'Tiny', 'filename': 'x'}).json()['id']
        assert c.put(f'/api/recordings/{rid}/source', content=b'oversized').status_code == 413
        assert c.get(f'/api/recordings/{rid}').json()['status'] == 'awaiting_upload'
        assert c.put(f'/api/recordings/{rid}/source', content=b'').status_code == 400
        assert c.put(f'/api/recordings/{rid}/source', content=b'ok').status_code == 200
        assert not list((tmp_path / 'staging').glob('*.part'))


def test_worker_auth_and_local_boundary(client, tmp_path):
    assert client.post('/api/worker/claim', json={'worker': 'unknown'}).status_code == 401
    assert client.post('/api/projects', json={'name': 'bad'}, headers={'Origin': 'https://outside.example'}).status_code == 403
    assert client.get('/api/projects', headers={'Host': 'outside.example'}).status_code == 403
    with TestClient(create_app(Settings(tmp_path / 'disabled', reserve=0))) as disabled:
        assert disabled.post('/api/worker/claim', json={'worker': 'unknown'}).status_code == 503


def test_only_one_concurrent_claim_wins(client):
    upload(client, project(client))
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: client.app.state.store.claim('race'), range(4)))
    assert sum(r is not None for r in results) == 1


def test_urgent_precedes_normal_and_queue_survives_restart(client):
    pid = project(client)
    upload(client, pid)
    urgent = upload(client, pid, priority='urgent')
    assert claim(client)['recording']['id'] == urgent
    # Reopening the same new-app database preserves ready-to-claim work.
    from argon_recordings.store import Store
    reopened = Store(client.app.state.store.root)
    assert reopened.claim('restart') is None  # One active heavy job globally.
    with reopened.db() as db:
        db.execute("UPDATE jobs SET lease_until=? WHERE recording_id=?", (time.time() - 1, urgent))
    assert reopened.claim('restart')['recording']['id'] == urgent


def test_actual_pull_protocol_with_synthetic_inference(client, tmp_path):
    rid = upload(client, project(client))
    def forward(request):
        return client.request(request.method, str(request.url), headers=dict(request.headers), content=request.read())
    def fake_extract(source, output):
        audio = output / 'audio.wav'
        audio.write_bytes(wav())
        return 1, audio, [{'name': 'audio.wav', 'kind': 'audio', 'time': None}]
    class Synthetic:
        def transcribe(self, audio, language, advance):
            assert language == 'es'
            advance(1)
            return [Segment(start=0, end=1, text='Synthetic worker evidence')]
    with httpx.Client(base_url='http://testserver', headers=AUTH, transport=httpx.MockTransport(forward)) as transport:
        with patch('argon_recordings.worker.extract', fake_extract):
            assert run_once(transport, Synthetic(), tmp_path)
    assert client.get(f'/api/recordings/{rid}').json()['status'] == 'ready'
    assert client.get('/api/search', params={'project_id': client.get(f'/api/recordings/{rid}').json()['project_id'], 'q': 'evidence'}).json()


def test_retry_preserves_source_and_fences_old_worker(client):
    rid = upload(client, project(client))
    old = claim(client)
    headers = dict(AUTH, **{'X-Lease-Id': old['lease_id']})
    assert client.post(f'/api/worker/jobs/{old["job_id"]}/fail', headers=headers, json={'message': 'Old five-minute restriction'}).status_code == 200
    assert client.post(f'/api/recordings/{rid}/retry').json()['status'] == 'queued'
    assert client.post(f'/api/recordings/{rid}/retry').status_code == 409
    assert client.post(f'/api/worker/jobs/{old["job_id"]}/heartbeat', headers=headers, json={'progress': .5, 'stage': 'stale'}).status_code == 409
    new = claim(client)
    assert new['lease_id'] != old['lease_id']
    assert client.get(f'/api/worker/jobs/{new["job_id"]}/source', headers=dict(AUTH, **{'X-Lease-Id': new['lease_id']})).content == b'synthetic media'


def test_long_completion_and_audio_larger_than_old_twelve_mb_limit(client):
    rid = upload(client, project(client))
    job = claim(client)
    headers = dict(AUTH, **{'X-Lease-Id': job['lease_id']})
    # Stream a valid WAV-shaped retained artifact above the old five-minute byte budget.
    payload = b'RIFF' + b'\0'*4 + b'WAVE' + b'\0' * (13 * 1024**2)
    assert client.put(f'/api/worker/jobs/{job["job_id"]}/artifacts/audio.wav', headers=headers, content=payload).status_code == 200
    body = {'duration': 7200, 'segments': [{'start': 7100, 'end': 7190, 'text': 'The end of a long recording.'}], 'artifacts': [{'name': 'audio.wav', 'kind': 'audio'}]}
    assert client.post(f'/api/worker/jobs/{job["job_id"]}/complete', headers=headers, json=body).json()['status'] == 'ready'
    assert client.get(f'/api/recordings/{rid}/transcript').json()['segments'][0]['start'] == 7100
