"""Opt-in check using the real prepared model, never downloads during testing."""
import os
import pytest
from fastapi.testclient import TestClient
from argon_recordings.api import Settings, create_app
from argon_recordings.embeddings import Embeddings
from test_workflow import AUTH, project, upload, claim, completion


@pytest.mark.skipif(not os.environ.get('TEST_EMBEDDING_DIR'), reason='Explicit prepared model required')
def test_real_cross_language_index_persistence_and_correction(tmp_path):
    engine = Embeddings(os.environ['TEST_EMBEDDING_DIR'], 'large')
    with TestClient(create_app(Settings(tmp_path, worker_token='synthetic-test-only', reserve=0, embeddings=engine))) as c:
        pid = project(c)
        ids = []
        for text in ['Acordamos restaurar PostgreSQL desde la copia de seguridad antes de volver a habilitar escrituras.',
                     'We will send invitations only after the administrator assigns a reviewer role to the new member.',
                     'Las facturas de este mes vencen el viernes. Finanzas confirmará los pagos pendientes.']:
            rid = upload(c, pid)
            job = claim(c)
            headers, body = completion(c, job)
            body['segments'][0]['text'] = text
            assert c.post(f'/api/worker/jobs/{job["job_id"]}/complete', headers=headers, json=body).status_code == 200
            ids.append(rid)
        hits = c.get('/api/search', params={'project_id': pid, 'q': 'Where did we discuss recovering the database from backup?', 'mode': 'semantic'})
        assert hits.status_code == 200, hits.text
        assert hits.json()[0]['recording_id'] == ids[0]
        with c.app.state.store.db() as db:
            before = db.execute('SELECT sha FROM embedding_versions WHERE recording_id=?', (ids[0],)).fetchone()[0]
        sid = c.get(f'/api/recordings/{ids[0]}/transcript').json()['segments'][0]['id']
        assert c.post(f'/api/recordings/{ids[0]}/corrections', json={'segment_id': sid, 'phrase': 'PostgreSQL', 'hint': 'Postgres', 'segment_ids': [sid]}).status_code == 201
        job = c.post('/api/worker/corrections/claim', headers=AUTH).json()
        h = dict(AUTH, **{'X-Lease-Id': job['lease_id']})
        assert c.post(f'/api/worker/corrections/{job["id"]}/complete', headers=h, json={'segments': [{'start': 0, 'end': 1, 'text': 'Restaurar Postgres desde la copia de seguridad.'}]}).status_code == 200
        assert c.post(f'/api/recordings/{ids[0]}/corrections/{job["id"]}/accept').status_code == 200
        with c.app.state.store.db() as db:
            after = db.execute('SELECT sha FROM embedding_versions WHERE recording_id=?', (ids[0],)).fetchone()[0]
            texts = [r[0] for r in db.execute('SELECT text FROM embeddings WHERE recording_id=?', (ids[0],))]
        assert before != after
        assert all('PostgreSQL' not in t for t in texts)
        assert any('Postgres' in t for t in texts)
        unrelated = project(c)
        assert c.get('/api/search', params={'project_id': unrelated, 'q': 'backup', 'mode': 'semantic'}).json() == []
    # Reopen the actual SQLite index; don't re-encode documents after a process restart.
    from argon_recordings.store import Store
    reopened = Store(tmp_path)
    with reopened.db() as db:
        assert db.execute('SELECT count(*) FROM embedding_versions').fetchone()[0] == 3
    windows = engine.chunks([dict(id='long', start=0, end=100, text='palabra English ' * 500 + 'UNIQUE_TAIL')])
    assert 'UNIQUE_TAIL' in windows[-1]['text']
