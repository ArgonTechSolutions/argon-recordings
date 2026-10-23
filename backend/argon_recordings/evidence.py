"""Revisioned source-time annotations, independent of ASR publication."""
import hashlib
import json
import time

from fastapi import HTTPException
from pydantic import Field

from .contracts import Strict
from .timeline import EvidenceVersions, TimelineRange
from .store import identity
from typing import Literal


SCHEMA = '''
CREATE TABLE IF NOT EXISTS evidence_versions(
 recording_id TEXT PRIMARY KEY REFERENCES recordings(id),
 transcript INTEGER NOT NULL DEFAULT 0,speakers INTEGER NOT NULL DEFAULT 0,
 annotations INTEGER NOT NULL DEFAULT 0,extraction INTEGER NOT NULL DEFAULT 0,
 transcript_fingerprint TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS visual_marks(
 id TEXT PRIMARY KEY,recording_id TEXT NOT NULL REFERENCES recordings(id),
 payload TEXT NOT NULL,deleted INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS visual_marks_recording ON visual_marks(recording_id);
CREATE TABLE IF NOT EXISTS visual_mark_history(
 id TEXT PRIMARY KEY,recording_id TEXT NOT NULL REFERENCES recordings(id),
 mark_id TEXT NOT NULL,before_payload TEXT,before_deleted INTEGER,
 revision INTEGER NOT NULL,created REAL NOT NULL,undone INTEGER NOT NULL DEFAULT 0);
'''


class EvidenceSnapshot(Strict):
    recording_id: str
    source_sha256: str | None
    duration: float | None
    versions: EvidenceVersions
    source_available: bool
    diarization: Literal['not_requested'] = 'not_requested'
    visual_extraction: Literal['not_requested'] = 'not_requested'


class MarkInput(Strict):
    interval: TimelineRange
    label: str = Field(default='', max_length=150)
    policy: Literal['auto', 'still', 'motion'] = 'auto'
    pinned: bool = False


class MarkMutation(Strict):
    expected_revision: int = Field(ge=0)
    expected_source_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')


class MarkWrite(MarkMutation):
    mark: MarkInput


class MarkView(MarkInput):
    id: str


class MarksSnapshot(Strict):
    revision: int
    marks: list[MarkView]


def snapshot(db, store, rid):
    row = db.execute('SELECT sha256,duration FROM recordings WHERE id=?', (rid,)).fetchone()
    if not row:
        raise HTTPException(404, 'Recording not found')
    # Include timing and word changes: text-only fingerprints miss evidence drift.
    segments = [tuple(r) for r in db.execute(
        'SELECT id,start,end,text,words FROM segments WHERE recording_id=? ORDER BY start,id', (rid,))]
    fingerprint = hashlib.sha256(json.dumps(segments, ensure_ascii=False).encode()).hexdigest()
    db.execute('INSERT OR IGNORE INTO evidence_versions(recording_id) VALUES(?)', (rid,))
    db.execute('UPDATE evidence_versions SET transcript=transcript+1,transcript_fingerprint=? '
               'WHERE recording_id=? AND transcript_fingerprint!=?', (fingerprint, rid, fingerprint))
    versions = dict(db.execute('SELECT transcript,speakers,annotations,extraction FROM evidence_versions WHERE recording_id=?', (rid,)).fetchone())
    return EvidenceSnapshot(recording_id=rid, source_sha256=row['sha256'], duration=row['duration'],
                            versions=EvidenceVersions(**versions),
                            source_available=(store.root / 'sources' / rid).is_file())


def marks(db, rid):
    version = db.execute('SELECT annotations FROM evidence_versions WHERE recording_id=?', (rid,)).fetchone()
    rows = db.execute('SELECT id,payload FROM visual_marks WHERE recording_id=? AND deleted=0 ORDER BY id', (rid,))
    return MarksSnapshot(revision=version[0], marks=[MarkView(id=r['id'], **json.loads(r['payload'])) for r in rows])


def check_revision(db, store, rid, body):
    state = snapshot(db, store, rid)
    if state.versions.annotations != body.expected_revision or state.source_sha256 != body.expected_source_sha256:
        raise HTTPException(409, 'Evidence changed; refresh before editing')
    return state


def install(app, get_store):
    @app.get('/api/recordings/{rid}/evidence', response_model=EvidenceSnapshot)
    def get_evidence(rid: str):
        store = get_store()
        with store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            return snapshot(db, store, rid)

    @app.get('/api/recordings/{rid}/marks', response_model=MarksSnapshot)
    def get_marks(rid: str):
        store = get_store()
        with store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            snapshot(db, store, rid)
            return marks(db, rid)

    def mutate(rid, mid, body, deleting=False):
        store = get_store()
        with store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            state = check_revision(db, store, rid, body)
            old = db.execute('SELECT * FROM visual_marks WHERE id=? AND recording_id=?', (mid, rid)).fetchone() if mid else None
            if mid and (not old or old['deleted']):
                raise HTTPException(404, 'Mark not found')
            if not deleting:
                if state.duration is None:
                    raise HTTPException(409, 'Recording timeline is not ready')
                try:
                    body.mark.interval.within(state.duration)
                except ValueError as exc:
                    raise HTTPException(422, str(exc)) from exc
            count = db.execute('SELECT count(*) FROM visual_marks WHERE recording_id=?', (rid,)).fetchone()[0]
            events = db.execute('SELECT count(*) FROM visual_mark_history WHERE recording_id=?', (rid,)).fetchone()[0]
            if (not mid and count >= 1000) or events >= 5000:
                raise HTTPException(409, 'Annotation history limit reached')
            mid = mid or identity()
            revision = state.versions.annotations + 1
            db.execute('INSERT INTO visual_mark_history VALUES(?,?,?,?,?,?,?,0)',
                       (identity(), rid, mid, old['payload'] if old else None,
                        old['deleted'] if old else None, revision, time.time()))
            if deleting:
                db.execute('UPDATE visual_marks SET deleted=1 WHERE id=?', (mid,))
            else:
                db.execute('INSERT INTO visual_marks VALUES(?,?,?,0) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,deleted=0',
                           (mid, rid, body.mark.model_dump_json()))
            db.execute('UPDATE evidence_versions SET annotations=? WHERE recording_id=?', (revision, rid))
            return marks(db, rid)

    @app.post('/api/recordings/{rid}/marks', response_model=MarksSnapshot, status_code=201)
    def create_mark(rid: str, body: MarkWrite):
        return mutate(rid, None, body)

    @app.put('/api/recordings/{rid}/marks/{mid}', response_model=MarksSnapshot)
    def update_mark(rid: str, mid: str, body: MarkWrite):
        return mutate(rid, mid, body)

    @app.post('/api/recordings/{rid}/marks/{mid}/delete', response_model=MarksSnapshot)
    def delete_mark(rid: str, mid: str, body: MarkMutation):
        return mutate(rid, mid, body, deleting=True)

    @app.post('/api/recordings/{rid}/marks/undo', response_model=MarksSnapshot)
    def undo_mark(rid: str, body: MarkMutation):
        store = get_store()
        with store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            state = check_revision(db, store, rid, body)
            event = db.execute('SELECT * FROM visual_mark_history WHERE recording_id=? AND undone=0 ORDER BY revision DESC LIMIT 1', (rid,)).fetchone()
            if not event:
                raise HTTPException(409, 'No annotation change to undo')
            if event['before_payload'] is None:
                db.execute('UPDATE visual_marks SET deleted=1 WHERE id=?', (event['mark_id'],))
            else:
                db.execute('UPDATE visual_marks SET payload=?,deleted=? WHERE id=?',
                           (event['before_payload'], event['before_deleted'], event['mark_id']))
            db.execute('UPDATE visual_mark_history SET undone=1 WHERE id=?', (event['id'],))
            db.execute('UPDATE evidence_versions SET annotations=? WHERE recording_id=?', (state.versions.annotations + 1, rid))
            return marks(db, rid)
