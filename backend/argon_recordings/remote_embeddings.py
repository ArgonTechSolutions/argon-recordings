"""Bounded, ephemeral inference requests; durable indexes remain coordinator-owned."""
import math
import secrets
import threading
import time

from fastapi import HTTPException
from pydantic import BaseModel, Field

from .embeddings import Embeddings


class InferenceResult(BaseModel):
    model: str = Field(max_length=256)
    vectors: list[list[float]] | None = Field(default=None, max_length=4)
    windows: list[dict] | None = Field(default=None, max_length=10000)
    failed: bool = False


class RemoteEmbeddings(Embeddings):
    def __init__(self, profile):
        super().__init__(None, profile)
        self.pending = {}
        self.require_worker = False
        self.last_seen = 0.
        self.condition = threading.Condition()

    @property
    def configured(self):
        return True

    def request(self, kind, payload, query=False, timeout=120):
        if self.require_worker and time.monotonic()-self.last_seen > 15:
            raise RuntimeError('No embedding worker with the configured profile is online')
        task = dict(id=secrets.token_hex(16), kind=kind, payload=payload,
                    query=query, model=self.model, profile=self.profile,
                    deadline=time.monotonic()+timeout, owner=None, result=None)
        with self.condition:
            if len(self.pending) >= 16:
                raise RuntimeError('Remote inference queue is full')
            self.pending[task['id']] = task
            try:
                while task['result'] is None:
                    remaining = task['deadline'] - time.monotonic()
                    if remaining <= 0:
                        raise RuntimeError('Remote embedding worker unavailable or busy; retry search')
                    self.condition.wait(remaining)
                result = task['result']
                if result.failed:
                    raise RuntimeError('Remote embedding inference failed; check worker logs')
                return result
            finally:
                self.pending.pop(task['id'], None)

    def claim(self, owner):
        with self.condition:
            now = time.monotonic()
            candidates = [t for t in self.pending.values() if t['result'] is None
                          and t['deadline'] > now and (t['owner'] is None or t['lease'] < now)]
            if not candidates:
                return None
            task = min(candidates, key=lambda t: (not t['query'], t['deadline']))
            task.update(owner=owner, lease=task['deadline'], attempt=secrets.token_hex(16))
            return {k:task[k] for k in ('id','attempt','kind','payload','query','model','profile')}

    def complete(self, tid, attempt, owner, result):
        with self.condition:
            task = self.pending.get(tid)
            if not task or task['deadline'] <= time.monotonic() or task['owner'] != owner or task['attempt'] != attempt or task['result'] is not None:
                raise HTTPException(409, 'Inference request expired or reassigned')
            if result.model != self.model:
                raise HTTPException(422, 'Embedding model must match the coordinator profile')
            if not result.failed:
                if task['kind'] == 'encode':
                    dimensions = {'small':384, 'base':768, 'large':1024}[self.profile]
                    if result.vectors is None or len(result.vectors) != len(task['payload']) or any(
                        len(v) != dimensions or any(not math.isfinite(x) for x in v)
                        or abs(sum(x*x for x in v)-1) > .02 for v in result.vectors):
                        raise HTTPException(422, 'Invalid normalized embedding vectors')
                else:
                    # Only source-derived windows may enter the durable index.
                    rows = task['payload']
                    source = ''.join(row['text']+' ' for row in rows)
                    source_end = max(row['end'] for row in rows)
                    if result.windows is None or not result.windows or sum(
                        len(w.get('text','')) if isinstance(w.get('text'),str) else 500001
                        for w in result.windows) > 500000:
                        raise HTTPException(422, 'Invalid transcript window budget')
                    position = 0
                    for w in result.windows:
                        if (set(w) != {'start','end','text'} or
                            not isinstance(w['text'],str) or not 0 < len(w['text']) <= 8192 or
                            not isinstance(w['start'], (int,float)) or not isinstance(w['end'], (int,float)) or
                            not math.isfinite(w['start']) or not math.isfinite(w['end']) or
                            not 0 <= w['start'] <= w['end'] <= source_end):
                            raise HTTPException(422, 'Invalid transcript windows')
                        position = source.find(w['text'], position)
                        if position < 0:
                            raise HTTPException(422, 'Windows must follow the source transcript')
            task['result'] = result
            self.condition.notify_all()

    def refresh(self, store, rid):
        # Background refresh is best-effort; never occupy request threads waiting
        # behind a remote inference call. Search rebuilds any skipped stale index.
        if not self.lock.acquire(blocking=False):
            return
        try:
            super().refresh(store, rid)
        finally:
            self.lock.release()

    def chunks(self, rows):
        payload = [dict(start=r['start'], end=r['end'], text=r['text']) for r in rows]
        if sum(len(r['text'])+1 for r in payload) > 250000:
            raise RuntimeError('Transcript exceeds the local indexing budget')
        return self.request('chunks', payload).windows

    def encode(self, texts, query=False):
        import numpy as np
        if query and len(texts) == 1:
            with self.condition:
                if texts[0] in self.query_cache:
                    self.query_cache.move_to_end(texts[0])
                    return [self.query_cache[texts[0]]]
        vectors = []
        for offset in range(0, len(texts), 4):
            result = self.request('encode', texts[offset:offset+4], query=query)
            vectors.extend(np.asarray(result.vectors, dtype='<f4'))
        if query and len(texts) == 1:
            with self.condition:
                self.query_cache[texts[0]] = vectors[0]
                if len(self.query_cache) > 64:
                    self.query_cache.popitem(last=False)
        return vectors


def inference_loop(stopping):
    """One small E5 execution lane in the existing worker; no public listener."""
    import logging
    import os
    import httpx
    runtime = Embeddings(os.environ['RECORDINGS_EMBEDDING_DIR'],
                         os.environ.get('RECORDINGS_EMBEDDING_PROFILE','large'))
    with httpx.Client(base_url='http://api:8001', timeout=60, trust_env=False) as client:
        while not stopping.is_set():
            try:
                response = client.post('/api/worker/inference/claim', json={'embedding_model':runtime.model})
                response.raise_for_status()
                task = response.json()
                if not task:
                    stopping.wait(.5)
                    continue
                result = dict(model=runtime.model)
                try:
                    if task['model'] != runtime.model:
                        raise ValueError('Coordinator and worker embedding profiles differ')
                    if task['kind'] == 'encode':
                        result['vectors'] = [v.tolist() for v in runtime.encode(task['payload'], query=task['query'])]
                    elif task['kind'] == 'chunks':
                        result['windows'] = runtime.chunks(task['payload'])
                    else:
                        raise ValueError('Unknown inference operation')
                except Exception:
                    logging.getLogger(__name__).exception('Remote embedding inference failed')
                    result = dict(model=task['model'], failed=True)
                client.post('/api/worker/inference/'+task['id']+'/'+task['attempt'], json=result).raise_for_status()
            except Exception as exc:
                logging.warning('Remote embedding connection failed (%s)', type(exc).__name__)
                stopping.wait(3)
