"""Measured progress, lease isolation and compatibility with older workers."""
from argon_recordings.worker import TranscriptionTracker
from argon_recordings.store import Store
from test_workflow import client, project, upload, claim, AUTH


def test_tracker_waits_for_evidence_and_counts_real_passages():
    now = [0]
    tracker = TranscriptionTracker(600, clock=lambda: now[0])
    assert tracker.snapshot()['eta_seconds'] is None
    tracker.advance(10)
    now[0] = 60
    tracker.advance(20)
    assert tracker.snapshot()['eta_seconds'] is None
    tracker.advance(60)
    assert tracker.snapshot() == dict(audio_processed_seconds=60, audio_total_seconds=600,
                                      passages=3, elapsed_seconds=60, eta_seconds=540)
    tracker.advance(50)  # overlapping timestamps never move the bar backwards
    assert tracker.snapshot()['audio_processed_seconds'] == 60
    tracker.advance(601)
    assert tracker.snapshot()['audio_processed_seconds'] == 600
    assert tracker.snapshot()['eta_seconds'] == 0


def test_progress_roundtrip_and_lease_reset(client):
    rid = upload(client, project(client))
    job = claim(client)
    headers = dict(AUTH, **{'X-Lease-Id': job['lease_id']})
    url = f'/api/worker/jobs/{job["job_id"]}/heartbeat'
    detail = dict(audio_processed_seconds=60, audio_total_seconds=600, passages=3, elapsed_seconds=60, eta_seconds=540)
    body = dict(progress=.18, stage='Transcribing with Turbo', transcription=detail)
    assert client.post(url, json=body, headers=headers).status_code == 200
    view = client.get(f'/api/recordings/{rid}').json()['job']
    assert view['transcription'] == detail and view['updated_at'] > 0
    with client.app.state.store.db() as db:
        db.execute('UPDATE jobs SET lease_until=0')
    replacement = claim(client)
    assert client.post(url, json=body, headers=headers).status_code == 409
    assert client.get(f'/api/recordings/{rid}').json()['job']['transcription'] is None
    headers['X-Lease-Id'] = replacement['lease_id']
    assert client.post(url, json=body, headers=headers).status_code == 200
    # Old workers and publication stages clear previously reported details.
    assert client.post(url, json=dict(progress=.95, stage='Publishing'), headers=headers).status_code == 200
    assert client.get(f'/api/recordings/{rid}').json()['job']['transcription'] is None
    body['transcription']['audio_processed_seconds'] = 601
    assert client.post(url, json=body, headers=headers).status_code == 422


def test_existing_foundation_database_gets_additive_progress_table(tmp_path):
    import sqlite3
    from argon_recordings.store import SCHEMA
    path = tmp_path / 'recordings.sqlite'
    with sqlite3.connect(path) as db:
        db.executescript(SCHEMA)
        db.execute("INSERT INTO projects VALUES('existing','Retained project',0)")
    store = Store(tmp_path)
    with store.db() as db:
        assert db.execute('SELECT name FROM projects').fetchone()[0] == 'Retained project'
        assert db.execute('SELECT COUNT(*) FROM job_progress').fetchone()[0] == 0
        assert db.execute('PRAGMA user_version').fetchone()[0] == 1
