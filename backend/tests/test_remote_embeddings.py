import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from argon_recordings.api import Settings, create_app
from argon_recordings.embeddings import Embeddings
from argon_recordings.remote_embeddings import RemoteEmbeddings, InferenceResult


def wait_claim(runtime, owner='worker-1'):
    for _ in range(100):
        task = runtime.claim(owner)
        if task:
            return task
        time.sleep(.01)
    raise AssertionError('No inference task queued')


def result(runtime, vectors=None, **kw):
    return InferenceResult(model=runtime.model, vectors=vectors, **kw)


def test_same_model_identity_and_existing_index_compatibility():
    for profile in ('small','base','large'):
        assert RemoteEmbeddings(profile).model == Embeddings('/unused',profile).model


def test_roundtrip_auth_assignment_normalization_and_cleanup():
    runtime = RemoteEmbeddings('small')
    with ThreadPoolExecutor() as pool:
        future = pool.submit(runtime.encode, ['screen sizes'], True)
        task = wait_claim(runtime)
        assert runtime.claim('worker-2') is None
        for owner, attempt, body, code in [
            ('worker-2',task['attempt'],result(runtime,[[1.]+[0.]*383]),409),
            ('worker-1','wrong',result(runtime,[[1.]+[0.]*383]),409),
            ('worker-1',task['attempt'],result(runtime,[[float('nan')]*384]),422),
            ('worker-1',task['attempt'],result(runtime,[[0.]*384]),422),
            ('worker-1',task['attempt'],InferenceResult(model='wrong',failed=True),422),
        ]:
            with pytest.raises(HTTPException) as error:
                runtime.complete(task['id'],attempt,owner,body)
            assert error.value.status_code == code
        runtime.complete(task['id'],task['attempt'],'worker-1',result(runtime,[[1.]+[0.]*383]))
        assert future.result(timeout=2)[0].shape == (384,)
    assert not runtime.pending


def test_expired_queue_and_late_completion_fail_closed():
    runtime = RemoteEmbeddings('small')
    with ThreadPoolExecutor() as pool:
        future = pool.submit(runtime.request,'encode',['hello'],timeout=.1)
        task = wait_claim(runtime)
        with pytest.raises(RuntimeError,match='unavailable or busy'):
            future.result(timeout=2)
    assert not runtime.pending
    with pytest.raises(HTTPException) as error:
        runtime.complete(task['id'],task['attempt'],'worker-1',result(runtime,failed=True))
    assert error.value.status_code == 409


def test_queries_prioritized_and_transcript_windows_validated():
    runtime = RemoteEmbeddings('small')
    rows = [dict(text='hello world',start=0.,end=3.)]
    with ThreadPoolExecutor() as pool:
        background = pool.submit(runtime.chunks,rows)
        for _ in range(100):
            if runtime.pending: break
            time.sleep(.01)
        query = pool.submit(runtime.encode,['hello'],True)
        for _ in range(100):
            if len(runtime.pending)==2: break
            time.sleep(.01)
        task = wait_claim(runtime)
        assert task['query']
        runtime.complete(task['id'],task['attempt'],'worker-1',result(runtime,[[1.]+[0.]*383]))
        query.result(timeout=2)
        task = wait_claim(runtime)
        with pytest.raises(HTTPException):
            runtime.complete(task['id'],task['attempt'],'worker-1',result(runtime,windows=[dict(text='fabricated',start=0,end=3)]))
        runtime.complete(task['id'],task['attempt'],'worker-1',result(runtime,windows=rows))
        assert background.result(timeout=2)==rows


def test_private_worker_api_requires_auth_and_matching_assignment(tmp_path):
    runtime = RemoteEmbeddings('small')
    app = create_app(Settings(data=tmp_path,worker_tokens={'worker-1':'secret'},embeddings=runtime))
    with TestClient(app) as client, ThreadPoolExecutor() as pool:
        assert client.post('/api/worker/inference/claim',json={}).status_code==401
        assert client.post('/api/worker/inference/claim',headers={'Authorization':'Bearer secret'},json={'embedding_model':runtime.model}).status_code==200
        future=pool.submit(runtime.encode,['hello'],True)
        headers={'Authorization':'Bearer secret'}
        task=None
        for _ in range(100):
            response=client.post('/api/worker/inference/claim',headers=headers,json={'embedding_model':runtime.model})
            assert response.status_code==200
            task=response.json()
            if task: break
            time.sleep(.01)
        url='/api/worker/inference/'+task['id']+'/'+task['attempt']
        body=result(runtime,[[1.]+[0.]*383]).model_dump()
        assert client.post(url,json=body).status_code==401
        assert client.post(url,json=body,headers=headers).status_code==200
        future.result(timeout=2)


def test_background_refresh_does_not_wait_for_remote_index_lock():
    runtime = RemoteEmbeddings('small')
    acquired = threading.Event()
    release = threading.Event()
    def hold():
        with runtime.lock:
            acquired.set()
            release.wait(2)
    thread = threading.Thread(target=hold)
    thread.start()
    acquired.wait(1)
    try:
        started = time.monotonic()
        runtime.refresh(None, 'unused')
        assert time.monotonic()-started < .2
    finally:
        release.set()
        thread.join()


def test_search_saturation_leaves_worker_claim_responsive(tmp_path):
    runtime = RemoteEmbeddings('small')
    app = create_app(Settings(data=tmp_path, worker_tokens={'worker-1':'secret'},embeddings=runtime))
    released = threading.Event()
    entered = []
    def slow_search(*_):
        entered.append(True)
        released.wait(3)
        return []
    runtime.search = slow_search
    with TestClient(app) as client, ThreadPoolExecutor(max_workers=8) as pool:
        pid = client.post('/api/projects',json={'name':'Test'}).json()['id']
        params={'project_id':pid,'q':'hello','mode':'semantic'}
        futures=[pool.submit(client.get,'/api/search',params=params) for _ in range(4)]
        try:
            for _ in range(100):
                if len(entered)==4:break
                time.sleep(.01)
            assert len(entered)==4
            assert client.get('/api/search',params=params).status_code==429
            assert client.post('/api/worker/inference/claim',headers={'Authorization':'Bearer secret'},json={'embedding_model':runtime.model}).status_code==200
        finally:
            released.set()
        assert all(f.result(timeout=2).status_code==200 for f in futures)
