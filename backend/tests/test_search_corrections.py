import time
import httpx
import pytest
from argon_recordings.api import Settings, create_app
from argon_recordings.contracts import Segment
from argon_recordings.worker import run_once
from fastapi.testclient import TestClient
from test_workflow import AUTH, project, upload, claim, completion


def ready(c):
    pid = project(c)
    rid = upload(c, pid)
    job = claim(c)
    headers, body = completion(c, job)
    assert c.post(f'/api/worker/jobs/{job["job_id"]}/complete', headers=headers, json=body).status_code == 200
    sid = c.get(f'/api/recordings/{rid}/transcript').json()['segments'][0]['id']
    return pid, rid, sid


@pytest.fixture
def c(tmp_path):
    with TestClient(create_app(Settings(tmp_path, worker_token='synthetic-test-only', reserve=0))) as client:
        yield client


def queue(c, rid, sid):
    body = {'segment_id': sid, 'phrase': 'PostgreSQL', 'hint': 'Postgres', 'segment_ids': [sid]}
    r = c.post(f'/api/recordings/{rid}/corrections', json=body)
    assert r.status_code == 201, r.text
    return body


def test_candidates_scope_selection_and_review(c):
    pid, rid, sid = ready(c)
    _, other, other_sid = ready(c)
    body = {'segment_id': sid, 'phrase': 'PostgreSQL', 'hint': 'Postgres'}
    hits = c.post(f'/api/recordings/{rid}/correction-candidates', json=body).json()
    assert [h['id'] for h in hits] == [sid]
    assert c.post(f'/api/recordings/{rid}/corrections', json=dict(body, segment_ids=[other_sid])).status_code == 422
    queue(c, rid, sid)
    assert c.post(f'/api/recordings/{rid}/corrections', json=dict(body, segment_ids=[sid])).status_code == 409
    assert c.post('/api/worker/corrections/claim').status_code == 401
    job = c.post('/api/worker/corrections/claim', headers=AUTH).json()
    assert job['recording_id'] == rid
    upload(c, pid)
    assert claim(c) is None  # ASR and corrections share one heavy-work slot.
    h = dict(AUTH, **{'X-Lease-Id': job['lease_id']})
    url = f'/api/worker/corrections/{job["id"]}/complete'
    assert c.post(url, headers=h, json={'segments': [{'start': 0, 'end': 2, 'text': 'outside'}]}).status_code == 422
    proposal = {'segments': [{'start': 0, 'end': 1, 'text': 'Postgres deployment is ready.'}]}
    assert c.post(url, headers=h, json=proposal).status_code == 200
    assert 'PostgreSQL' in c.get(f'/api/recordings/{rid}/transcript').json()['segments'][0]['text']
    assert c.post(f'/api/recordings/{other}/corrections/{job["id"]}/accept').status_code == 409
    assert c.post(f'/api/recordings/{rid}/corrections/{job["id"]}/accept').status_code == 200
    hits = c.get('/api/search', params={'project_id': pid, 'q': 'Postgres', 'mode': 'keyword'}).json()
    assert len(hits) == 1
    assert c.get(f'/api/recordings/{rid}/corrections').json()[0]['original'].startswith('PostgreSQL')
    assert c.post(f'/api/recordings/{rid}/corrections/{job["id"]}/accept').status_code == 409


def test_expiry_dismissal_and_stale_revision(c):
    _, rid, sid = ready(c)
    queue(c, rid, sid)
    old = c.post('/api/worker/corrections/claim', headers=AUTH).json()
    with c.app.state.store.db() as db:
        db.execute('UPDATE corrections SET lease_until=? WHERE id=?', (time.time() - 1, old['id']))
    new = c.post('/api/worker/corrections/claim', headers=AUTH).json()
    url = f'/api/worker/corrections/{old["id"]}'
    h = dict(AUTH, **{'X-Lease-Id': old['lease_id']})
    assert c.get(url + '/audio', headers=h).status_code == 409
    assert c.post(url + '/complete', headers=h, json={'segments': [{'start': 0, 'end': 1, 'text': 'stale'}]}).status_code == 409
    h['X-Lease-Id'] = new['lease_id']
    assert c.post(url + '/complete', headers=h, json={'segments': [{'start': 0, 'end': 1, 'text': 'proposal'}]}).status_code == 200
    with c.app.state.store.db() as db:
        db.execute('UPDATE segments SET text=? WHERE id=?', ('A newer correction', sid))
    assert c.post(f'/api/recordings/{rid}/corrections/{old["id"]}/accept').status_code == 409
    assert c.post(f'/api/recordings/{rid}/corrections/{old["id"]}/dismiss').status_code == 200


def test_actual_retry_protocol_with_hint_and_crop(c, tmp_path):
    _, rid, sid = ready(c)
    with c.app.state.store.db() as db:
        db.execute('UPDATE segments SET start=.5 WHERE id=?', (sid,))
    queue(c, rid, sid)
    def forward(request):
        return c.request(request.method, str(request.url), headers=dict(request.headers), content=request.read())
    class Synthetic:
        def transcribe(self, audio, language, advance, hint=None):
            import wave
            with wave.open(str(audio)) as source:
                assert source.getnframes() == 8000
            assert hint == 'Postgres'
            assert language == 'es'
            advance(1)
            return [Segment(start=0, end=.5, text='Postgres')]
    with httpx.Client(base_url='http://testserver', headers=AUTH, transport=httpx.MockTransport(forward)) as transport:
        assert run_once(transport, Synthetic(), tmp_path)
    history = c.get(f'/api/recordings/{rid}/corrections').json()
    assert history[0]['state'] == 'proposed'
    assert history[0]['proposal'][0]['text'] == 'Postgres'
    assert history[0]['proposal'][0]['start'] == .5
    assert history[0]['proposal'][0]['end'] == 1


def test_hybrid_search_uses_semantics_and_remains_project_scoped(tmp_path):
    class SyntheticEmbeddings:
        configured = True
        def refresh(self, store, rid):
            pass
        def search(self, store, pid, q):
            with store.db() as db:
                return [dict(r) for r in db.execute('SELECT r.id AS recording_id,r.title,s.start,s.end,s.text FROM recordings r JOIN segments s ON s.recording_id=r.id WHERE r.project_id=?', (pid,))]
    with TestClient(create_app(Settings(tmp_path, worker_token='synthetic-test-only', reserve=0, embeddings=SyntheticEmbeddings()))) as c:
        pid, rid, _ = ready(c)
        other = project(c)
        assert c.get('/api/search', params={'project_id': pid, 'q': 'database release'}).json()[0]['recording_id'] == rid
        assert c.get('/api/search', params={'project_id': other, 'q': 'database release'}).json() == []
        assert c.get('/api/search', params={'project_id': pid, 'q': 'database release', 'mode': 'keyword'}).json() == []


def test_model_absence_is_explicit(c):
    pid = project(c)
    assert c.get('/api/search', params={'project_id': pid, 'q': 'algo', 'mode': 'semantic'}).status_code == 503
    assert c.get('/api/search', params={'project_id': pid, 'q': 'algo', 'mode': 'invalid'}).status_code == 422


def test_fuzzy_candidates_find_similar_misheard_words(c):
    _, rid, sid = ready(c)
    with c.app.state.store.db() as db:
        db.execute('UPDATE segments SET text=? WHERE id=?', ('Usamos postgre sequel para guardar los datos.', sid))
        db.execute('INSERT INTO segments VALUES(?,?,?,?,?,?)', ('similar', rid, .5, 1, 'El servidor de postgre sequal sigue activo.', '[]'))
        db.execute('INSERT INTO segments VALUES(?,?,?,?,?,?)', ('unrelated', rid, .5, 1, 'Mañana revisaremos las facturas pendientes.', '[]'))
    result = c.post(f'/api/recordings/{rid}/correction-candidates', json={'segment_id': sid, 'phrase': 'postgre sequel', 'hint': 'PostgreSQL'})
    assert result.status_code == 200
    assert {r['id'] for r in result.json()} == {sid, 'similar'}


def test_semantic_results_deduplicate_shared_jump_targets(c):
    import numpy as np
    from argon_recordings.embeddings import Embeddings
    pid, rid, _ = ready(c)
    class StoredOnly(Embeddings):
        def encode(self, texts, query=False):
            return [np.array([1, 0], dtype='<f4')]
    engine = StoredOnly('unused-explicit-test-profile')
    with c.app.state.store.db() as db:
        db.execute('INSERT INTO embedding_versions VALUES(?,?,?)', (rid, engine.model, 'test'))
        for text in ['best matching window', 'overlapping window at the same moment']:
            db.execute('INSERT INTO embeddings VALUES(?,?,?,?,?,?)', (rid, 0, 1, text, np.array([1, 0], dtype='<f4').tobytes(), engine.model))
    assert len(engine.search(c.app.state.store, pid, 'query')) == 1


def test_dismissing_running_retry_fences_publication(c):
    _, rid, sid = ready(c)
    queue(c, rid, sid)
    job = c.post('/api/worker/corrections/claim', headers=AUTH).json()
    assert c.post(f'/api/recordings/{rid}/corrections/{job["id"]}/dismiss').status_code == 200
    h = dict(AUTH, **{'X-Lease-Id': job['lease_id']})
    assert c.post(f'/api/worker/corrections/{job["id"]}/complete', headers=h, json={'segments': [{'start': 0, 'end': 1, 'text': 'cancelled'}]}).status_code == 409
    assert c.get(f'/api/recordings/{rid}/transcript').json()['segments'][0]['text'].startswith('PostgreSQL')
