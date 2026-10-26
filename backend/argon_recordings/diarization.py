"""Optional recording-wide, lease-fenced speaker enrichment.

ASR text and IDs remain authoritative; this layer publishes independent turns.
"""
import hashlib
import json
import time
from typing import Literal

from fastapi import Depends, Header, HTTPException, Request, Query
from fastapi.responses import FileResponse
from pydantic import Field, model_validator

from .contracts import Strict, Failure, Heartbeat
from .evidence import snapshot
from .store import identity
from .timeline import SourceReference, TimelineRange
from .diarization_runtime import MODEL
from .speaker_review import SpeakerName, OverrideView, PassageSpeakers, review_projection, stable_turns


SCHEMA = '''
CREATE TABLE IF NOT EXISTS diarization_jobs(
 id TEXT PRIMARY KEY,recording_id TEXT NOT NULL REFERENCES recordings(id),
 reference TEXT NOT NULL,options TEXT NOT NULL,cache_key TEXT NOT NULL UNIQUE,
 state TEXT NOT NULL,worker TEXT,lease_id TEXT,lease_until REAL,
 attempts INTEGER NOT NULL DEFAULT 0,progress REAL NOT NULL DEFAULT 0,
 result TEXT,result_sha TEXT,error TEXT,created REAL NOT NULL,model TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS diarization_results(
 recording_id TEXT PRIMARY KEY REFERENCES recordings(id),job_id TEXT NOT NULL,
 source_sha256 TEXT NOT NULL,transcript_version INTEGER NOT NULL,model TEXT NOT NULL,
 turns TEXT NOT NULL);
'''


class SpeakerOptions(Strict):
    min_speakers: int = Field(default=1, ge=1, le=32)
    max_speakers: int = Field(default=16, ge=1, le=32)

    @model_validator(mode='after')
    def bounds(self):
        if self.max_speakers < self.min_speakers:
            raise ValueError('Maximum speakers precedes minimum')
        return self


class DiarizationRequest(Strict):
    reference: SourceReference
    options: SpeakerOptions = SpeakerOptions()


class SpeakerTurn(TimelineRange):
    speaker: str = Field(pattern=r'^speaker-[0-9]{1,4}$')


class DiarizationCompletion(Strict):
    model: str = Field(min_length=1, max_length=256)
    turns: list[SpeakerTurn] = Field(max_length=100000)


class DiarizationJob(Strict):
    id: str
    recording_id: str
    state: Literal['queued', 'running', 'completed', 'failed', 'cancelled']
    progress: float
    error: str | None


class SpeakersSnapshot(Strict):
    revision: int
    transcript_revision: int
    model: str | None
    stale: bool
    turns: list[SpeakerTurn]
    speakers: list[SpeakerName]
    overrides: list[OverrideView]
    passages: list[PassageSpeakers]
    next_passage_offset: int | None
    next_turn_offset: int | None


def current_reference(db, store, rid):
    state = snapshot(db, store, rid)
    if state.duration is None or not state.source_sha256:
        raise HTTPException(409, 'Recording timeline is not ready')
    return SourceReference(recording_id=rid, source_sha256=state.source_sha256,
                           interval=TimelineRange(start=0, end=state.duration), versions=state.versions)


def compatible(expected, actual):
    # Independent visual marks and previous speaker publications do not change
    # audio or word alignment. Transcript/source drift does invalidate a run.
    return (expected.recording_id == actual.recording_id and
            expected.source_sha256 == actual.source_sha256 and
            expected.interval == actual.interval and
            expected.versions.transcript == actual.versions.transcript)


def job_view(row):
    return DiarizationJob(**{k: row[k] for k in ('id', 'recording_id', 'state', 'progress', 'error')})


def install(app, get_store, worker_auth):
    def leased(db, jid, token, request, completed=False):
        row = db.execute('SELECT * FROM diarization_jobs WHERE id=?', (jid,)).fetchone()
        worker = getattr(request.state, 'worker_id', None)
        if not row or row['lease_id'] != token or (worker is not None and row['worker'] != worker):
            raise HTTPException(409, 'Speaker task lease expired or superseded')
        if completed and row['state'] == 'completed':
            return row
        if row['state'] != 'running' or row['lease_until'] <= time.time():
            raise HTTPException(409, 'Speaker task lease expired or superseded')
        return row

    @app.get('/api/recordings/{rid}/speakers', response_model=SpeakersSnapshot)
    def speakers(rid: str, passage_offset: int = Query(default=0, ge=0), turn_offset: int = Query(default=0, ge=0),
                 limit: int = Query(default=500, ge=1, le=1000), expected_revision: int | None = Query(default=None, ge=0),
                 expected_transcript_revision: int | None = Query(default=None, ge=0)):
        store = get_store()
        with store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            state = snapshot(db, store, rid)
            if expected_revision is not None and expected_revision != state.versions.speakers:
                raise HTTPException(409, 'Speaker annotations changed; restart pagination')
            if expected_transcript_revision is not None and expected_transcript_revision != state.versions.transcript:
                raise HTTPException(409, 'Transcript changed; restart pagination')
            result = db.execute('SELECT * FROM diarization_results WHERE recording_id=?', (rid,)).fetchone()
            stale = bool(result and (result['source_sha256'] != state.source_sha256 or result['transcript_version'] != state.versions.transcript))
            turns = json.loads(result['turns']) if result and not stale else []
            names, overrides, passages, next_passage = review_projection(db, rid, turns, passage_offset, limit)
            return SpeakersSnapshot(revision=state.versions.speakers, transcript_revision=state.versions.transcript, model=result['model'] if result else None,
                                    stale=stale, turns=turns[turn_offset:turn_offset+limit], speakers=names, overrides=overrides, passages=passages,
                                    next_passage_offset=next_passage, next_turn_offset=turn_offset+limit if turn_offset+limit<len(turns) else None)

    @app.post('/api/recordings/{rid}/diarization', response_model=DiarizationJob)
    def schedule(rid: str, body: DiarizationRequest):
        store = get_store()
        with store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            actual = current_reference(db, store, rid)
            if not compatible(body.reference, actual):
                raise HTTPException(409, 'Transcript or source changed; refresh before diarizing')
            if not db.execute("SELECT 1 FROM artifacts WHERE recording_id=? AND name='audio.wav'", (rid,)).fetchone():
                raise HTTPException(409, 'Retained audio is unavailable')
            # Freeze semantic identity independently of annotations/speaker edits.
            payload = dict(source=actual.source_sha256, transcript=actual.versions.transcript,
                           recording=rid, model=MODEL, options=body.options.model_dump())
            key = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
            previous = db.execute('SELECT * FROM diarization_jobs WHERE cache_key=?', (key,)).fetchone()
            if previous and previous['model'] == MODEL and previous['state'] in {'queued', 'running', 'completed'}:
                return job_view(previous)
            if db.execute("SELECT count(*) FROM diarization_jobs WHERE state IN ('queued','running')").fetchone()[0] >= 32:
                raise HTTPException(429, 'Speaker task queue is full')
            db.execute("UPDATE diarization_jobs SET state='cancelled',lease_id=NULL,lease_until=NULL WHERE recording_id=? AND state IN ('queued','running')", (rid,))
            if previous:
                db.execute("UPDATE diarization_jobs SET state='queued',attempts=0,lease_id=NULL,lease_until=NULL,error=NULL,model=?,reference=?,progress=0,worker=NULL,result_sha=NULL WHERE id=?", (MODEL,actual.model_dump_json(),previous['id']))
                jid = previous['id']
            else:
                if db.execute('SELECT count(*) FROM diarization_jobs WHERE recording_id=?', (rid,)).fetchone()[0] >= 100:
                    raise HTTPException(409, 'Speaker task history limit reached')
                jid = identity()
                db.execute('INSERT INTO diarization_jobs(id,recording_id,reference,options,cache_key,state,created,model) VALUES(?,?,?,?,?,?,?,?)',
                           (jid, rid, actual.model_dump_json(), body.options.model_dump_json(), key, 'queued', time.time(), MODEL))
            return job_view(db.execute('SELECT * FROM diarization_jobs WHERE id=?', (jid,)).fetchone())

    @app.get('/api/recordings/{rid}/diarization', response_model=list[DiarizationJob])
    def list_jobs(rid: str):
        with get_store().db() as db:
            if not db.execute('SELECT 1 FROM recordings WHERE id=?', (rid,)).fetchone():
                raise HTTPException(404, 'Recording not found')
            return [job_view(r) for r in db.execute('SELECT * FROM diarization_jobs WHERE recording_id=? ORDER BY created DESC LIMIT 100', (rid,))]

    @app.post('/api/recordings/{rid}/diarization/{jid}/cancel', response_model=DiarizationJob)
    def cancel(rid: str, jid: str):
        with get_store().db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM diarization_jobs WHERE id=? AND recording_id=?', (jid, rid)).fetchone()
            if not row:
                raise HTTPException(404, 'Speaker task not found')
            if row['state'] not in {'queued', 'running', 'cancelled'}:
                raise HTTPException(409, 'Speaker task already finished')
            db.execute("UPDATE diarization_jobs SET state='cancelled',lease_id=NULL,lease_until=NULL WHERE id=?", (jid,))
            return job_view(db.execute('SELECT * FROM diarization_jobs WHERE id=?', (jid,)).fetchone())

    @app.post('/api/worker/diarization/claim', dependencies=[Depends(worker_auth)])
    def claim(request: Request):
        store = get_store()
        worker = getattr(request.state, 'worker_id', None)
        with store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            now = time.time()
            db.execute("UPDATE diarization_jobs SET state='failed',error='Speaker model configuration changed; request analysis again',lease_id=NULL,lease_until=NULL WHERE state IN ('queued','running') AND model!=?", (MODEL,))
            db.execute("UPDATE diarization_jobs SET state='failed',error='Speaker lease retries exhausted' WHERE state='running' AND lease_until<=? AND attempts>=3", (now,))
            if not store.capacity_available(db, worker):
                return None
            row = db.execute("SELECT * FROM diarization_jobs WHERE state='queued' OR (state='running' AND lease_until<=? AND attempts<3) ORDER BY created LIMIT 1", (now,)).fetchone()
            if not row:
                return None
            token = identity()
            db.execute("UPDATE diarization_jobs SET state='running',worker=?,lease_id=?,lease_until=?,attempts=attempts+1 WHERE id=?", (worker, token, now+120, row['id']))
            return {'id': row['id'], 'lease_id': token, 'reference': json.loads(row['reference']), 'options': json.loads(row['options']), 'model': row['model']}

    @app.post('/api/worker/diarization/{jid}/heartbeat', dependencies=[Depends(worker_auth)])
    def heartbeat(jid: str, body: Heartbeat, request: Request, x_lease_id: str = Header()):
        with get_store().db() as db:
            db.execute('BEGIN IMMEDIATE')
            leased(db, jid, x_lease_id, request)
            db.execute('UPDATE diarization_jobs SET lease_until=?,progress=? WHERE id=?', (time.time()+120, body.progress, jid))
        return {'ok': True}

    @app.get('/api/worker/diarization/{jid}/audio', dependencies=[Depends(worker_auth)])
    def audio(jid: str, request: Request, x_lease_id: str = Header()):
        with get_store().db() as db:
            row = leased(db, jid, x_lease_id, request)
            artifact = db.execute("SELECT path FROM artifacts WHERE recording_id=? AND name='audio.wav'", (row['recording_id'],)).fetchone()
            if not artifact:
                raise HTTPException(409, 'Retained audio unavailable')
            return FileResponse(artifact['path'], media_type='audio/wav')

    @app.post('/api/worker/diarization/{jid}/complete', dependencies=[Depends(worker_auth)])
    def complete(jid: str, body: DiarizationCompletion, request: Request, x_lease_id: str = Header()):
        store = get_store()
        digest = hashlib.sha256(body.model_dump_json().encode()).hexdigest()
        with store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = leased(db, jid, x_lease_id, request, completed=True)
            if row['state'] == 'completed':
                if row['result_sha'] != digest:
                    raise HTTPException(409, 'Speaker publication already differs')
                return {'ok': True}
            if body.model != MODEL or body.model != row['model']:
                raise HTTPException(422, 'Unrecognized speaker model/configuration identity')
            expected = SourceReference.model_validate_json(row['reference'])
            actual = current_reference(db, store, row['recording_id'])
            if not compatible(expected, actual):
                raise HTTPException(409, 'Transcript changed during speaker analysis')
            last = 0
            options = SpeakerOptions.model_validate_json(row['options'])
            count = len({t.speaker for t in body.turns})
            if count and not options.min_speakers <= count <= options.max_speakers:
                raise HTTPException(422, 'Speaker count is outside requested bounds')
            for turn in body.turns:
                if turn.start < last or turn.end <= turn.start or turn.end > actual.interval.end:
                    raise HTTPException(422, 'Invalid speaker timeline')
                last = turn.start
            turns = stable_turns(db, row['recording_id'], [t.model_dump() for t in body.turns])
            db.execute('INSERT INTO diarization_results VALUES(?,?,?,?,?,?) ON CONFLICT(recording_id) DO UPDATE SET job_id=excluded.job_id,source_sha256=excluded.source_sha256,transcript_version=excluded.transcript_version,model=excluded.model,turns=excluded.turns',
                       (row['recording_id'], jid, actual.source_sha256, actual.versions.transcript, body.model, json.dumps(turns)))
            db.execute('UPDATE evidence_versions SET speakers=speakers+1 WHERE recording_id=?', (row['recording_id'],))
            db.execute("UPDATE diarization_jobs SET state='completed',progress=1,result_sha=? WHERE id=?", (digest, jid))
        return {'ok': True}

    @app.post('/api/worker/diarization/{jid}/fail', dependencies=[Depends(worker_auth)])
    def fail(jid: str, body: Failure, request: Request, x_lease_id: str = Header()):
        with get_store().db() as db:
            db.execute('BEGIN IMMEDIATE')
            leased(db, jid, x_lease_id, request)
            db.execute("UPDATE diarization_jobs SET state='failed',error=? WHERE id=?", (body.message, jid))
        return {'ok': True}

    return {'speakers': speakers}
