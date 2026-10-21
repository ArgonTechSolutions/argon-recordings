"""Synthetic UI integration fixture; never runs inference or uses customer media."""
import io
from pathlib import Path
import sys
import wave
from fastapi.testclient import TestClient
from argon_recordings.api import Settings, create_app

root = Path(sys.argv[1])
auth = {'Authorization': 'Bearer synthetic-browser-only'}
with TestClient(create_app(Settings(root, worker_token='synthetic-browser-only'))) as c:
    pid = c.post('/api/projects', json={'name': 'Integration workspace'}).json()['id']
    rid = c.post(f'/api/projects/{pid}/recordings', json={'title': 'Synthetic demo', 'filename': 'synthetic.wav', 'language': 'en'}).json()['id']
    target = io.BytesIO()
    with wave.open(target, 'wb') as audio:
        audio.setnchannels(1); audio.setsampwidth(2); audio.setframerate(16000)
        audio.writeframes(b'\0\0' * 16000 * 31)
    source = target.getvalue()
    assert c.put(f'/api/recordings/{rid}/source', content=source).status_code == 200
    job = c.post('/api/worker/claim', headers=auth, json={'worker': 'synthetic-browser'}).json()
    headers = dict(auth, **{'X-Lease-Id': job['lease_id']})
    assert c.put(f'/api/worker/jobs/{job["job_id"]}/artifacts/audio.wav', content=source, headers=headers).status_code == 200
    response = c.post(f'/api/worker/jobs/{job["job_id"]}/complete', headers=headers, json={
        'duration': 31, 'segments': [
            {'start': 0, 'end': 10, 'text': 'Welcome to a synthetic recording for interface validation.'},
            {'start': 10, 'end': 20, 'text': 'Workspace permissions are visible before an invitation is sent.'},
            {'start': 20, 'end': 31, 'text': 'Search the transcript and return to the relevant moment.'},
        ], 'artifacts': [{'name': 'audio.wav', 'kind': 'audio', 'time': None}]})
    assert response.status_code == 200, response.text
