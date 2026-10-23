import concurrent.futures

from argon_recordings.api import Settings, create_app
from fastapi.testclient import TestClient
from test_workflow import client, project, upload
from test_accounts import team, login, invited


def ready(client, pid):
    rid = upload(client, pid)
    with client.app.state.store.db() as db:
        db.execute("UPDATE recordings SET duration=30,status='ready' WHERE id=?", (rid,))
    return rid


def body(client, rid, **mark):
    snap = client.get(f'/api/recordings/{rid}/evidence').json()
    return {'expected_revision': snap['versions']['annotations'],
            'expected_source_sha256': snap['source_sha256'],
            'mark': {'interval': {'start': 2, 'end': 4}, 'label': 'Code', **mark}}


def test_marks_revision_history_undo_and_restart(client, tmp_path):
    rid = ready(client, project(client))
    url = f'/api/recordings/{rid}/marks'
    initial = body(client, rid, pinned=True)
    created = client.post(url, json=initial)
    assert created.status_code == 201, created.text
    mark = created.json()['marks'][0]
    assert mark['pinned'] and created.json()['revision'] == 1
    assert client.post(url, json=initial).status_code == 409
    change = body(client, rid, label='Readable code', policy='still')
    updated = client.put(f'{url}/{mark["id"]}', json=change)
    assert updated.json()['revision'] == 2
    stale_delete = {k: v for k, v in change.items() if k != 'mark'}
    assert client.post(f'{url}/{mark["id"]}/delete', json=stale_delete).status_code == 409
    delete = {k: v for k, v in body(client, rid).items() if k != 'mark'}
    assert client.post(f'{url}/{mark["id"]}/delete', json=delete).json()['marks'] == []
    undo = {k: v for k, v in body(client, rid).items() if k != 'mark'}
    restored = client.post(f'{url}/undo', json=undo).json()
    assert restored['revision'] == 4 and restored['marks'][0]['label'] == 'Readable code'
    # Reopen the actual on-disk database with the same application configuration.
    app = create_app(Settings(client.app.state.store.root, worker_token='synthetic-test-only', reserve=0))
    with TestClient(app) as restarted:
        assert restarted.get(url).json() == restored


def test_snapshot_tracks_word_and_time_changes_not_annotation_text(client):
    rid = ready(client, project(client))
    url = f'/api/recordings/{rid}/evidence'
    before = client.get(url).json()
    assert before['source_available'] and before['diarization'] == 'not_requested'
    assert client.get(url).json() == before
    with client.app.state.store.db() as db:
        db.execute('INSERT INTO segments VALUES(?,?,?,?,?,?)', ('seg', rid, 0, 2, 'hello', '[]'))
    first = client.get(url).json()
    assert first['versions']['transcript'] == before['versions']['transcript'] + 1
    with client.app.state.store.db() as db:
        db.execute('UPDATE segments SET end=3 WHERE id=?', ('seg',))
    assert client.get(url).json()['versions']['transcript'] == first['versions']['transcript'] + 1
    client.post(f'/api/recordings/{rid}/marks', json=body(client, rid))
    after = client.get(url).json()
    assert after['versions']['annotations'] == 1
    assert after['versions']['transcript'] == first['versions']['transcript'] + 1


def test_invalid_and_cross_recording_marks_cannot_change_data(client):
    pid = project(client)
    rid, other = ready(client, pid), ready(client, pid)
    url = f'/api/recordings/{rid}/marks'
    for interval in [{'start': 31, 'end': 31}, {'start': 2, 'end': 1}, {'start': -1, 'end': 2}]:
        assert client.post(url, json=body(client, rid, interval=interval)).status_code == 422
    wrong = body(client, rid)
    wrong['expected_source_sha256'] = 'f' * 64
    assert client.post(url, json=wrong).status_code == 409
    mid = client.post(url, json=body(client, rid)).json()['marks'][0]['id']
    assert client.put(f'/api/recordings/{other}/marks/{mid}', json=body(client, other)).status_code == 404
    assert len(client.get(url).json()['marks']) == 1
    with client.app.state.store.db() as db:
        db.execute('UPDATE recordings SET duration=NULL WHERE id=?', (other,))
    assert client.post(f'/api/recordings/{other}/marks', json=body(client, other)).status_code == 409


def test_racing_editors_only_one_can_publish_at_same_revision(client):
    rid = ready(client, project(client))
    payload = body(client, rid)
    def write(_):
        return client.post(f'/api/recordings/{rid}/marks', json=payload).status_code
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(write, range(2))) == [201, 409]


def test_viewer_and_other_project_isolation(team):
    app, owner, manager, uid = team
    headers = login(owner)
    pid = owner.post('/api/projects', json={'name': 'Private'}, headers=headers).json()['id']
    rid = owner.post(f'/api/projects/{pid}/recordings', json={'title': 'Demo', 'filename': 'demo.wav'}, headers=headers).json()['id']
    owner.put(f'/api/recordings/{rid}/source', content=b'synthetic', headers=headers)
    with app.state.store.db() as db:
        db.execute("UPDATE recordings SET duration=30,status='ready' WHERE id=?", (rid,))
    guest, guest_headers = invited(app, manager, uid, 'viewer@example.com')
    url = f'/api/recordings/{rid}/marks'
    assert guest.get(url).status_code == 404
    owner.put(f'/api/projects/{pid}/members', json={'email': 'viewer@example.com', 'role': 'viewer'}, headers=headers)
    assert guest.get(url).status_code == 200
    assert guest.post(url, json=body(owner, rid), headers=guest_headers).status_code == 403
    assert owner.post(url, json=body(owner, rid), headers=headers).status_code == 201
