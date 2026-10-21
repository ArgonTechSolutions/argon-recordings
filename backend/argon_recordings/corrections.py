"""Selected, lease-fenced passage retries with explicit acceptance and text history."""
from difflib import SequenceMatcher
import json
import time
import unicodedata
import re

from fastapi import BackgroundTasks, Depends, Header, HTTPException
from fastapi.responses import FileResponse

from .contracts import CorrectionCandidates, CorrectionCreate, CorrectionCompletion, CorrectionCandidateHit, CorrectionView, Segment, Heartbeat, Failure
from .store import identity


def normalized(text):
    return ''.join(c for c in unicodedata.normalize('NFKD', text.lower()) if not unicodedata.combining(c))


def similarity(phrase, text):
    words = re.findall(r'\w+', normalized(text))
    target = re.findall(r'\w+', normalized(phrase))
    if not target:
        return 0
    return max((SequenceMatcher(None, ' '.join(target), ' '.join(words[i:i + len(target)])).ratio()
                for i in range(len(words))), default=0)


def install(app, store, recording_or_404, worker_auth, embeddings):
    def lease(db, cid, token):
        row = db.execute('SELECT c.*,s.start,s.end FROM corrections c JOIN segments s ON s.id=c.segment_id WHERE c.id=?', (cid,)).fetchone()
        if not row or row['state'] != 'running' or row['lease_id'] != token or row['lease_until'] <= time.time():
            raise HTTPException(409, 'Correction lease expired or superseded')
        return row

    def candidates(rid, body, db):
        rows = db.execute('SELECT id,start,end,text FROM segments WHERE recording_id=? ORDER BY start', (rid,)).fetchall()
        marked = next((r for r in rows if r['id'] == body.segment_id), None)
        if not marked or normalized(body.phrase.strip()) not in normalized(marked['text']):
            raise HTTPException(422, 'Mark text from the selected passage')
        hits = []
        for row in rows:
            score = max(similarity(body.phrase, row['text']), similarity(body.hint, row['text']))
            if score >= .55 or row['id'] == body.segment_id:
                hits.append(dict(row, score=round(score, 3)))
        return sorted(hits, key=lambda r: (r['id'] != body.segment_id, -r['score'], r['start']))[:30]

    @app.post('/api/recordings/{rid}/correction-candidates', response_model=list[CorrectionCandidateHit])
    def find(rid: str, body: CorrectionCandidates):
        recording_or_404(rid)
        with store().db() as db:
            return candidates(rid, body, db)

    @app.post('/api/recordings/{rid}/corrections', status_code=201)
    def create(rid: str, body: CorrectionCreate):
        recording = recording_or_404(rid)
        if recording['status'] != 'ready':
            raise HTTPException(409, 'A ready transcript is required')
        with store().db() as db:
            db.execute('BEGIN IMMEDIATE')
            allowed = {r['id'] for r in candidates(rid, body, db)}
            selected = set(body.segment_ids)
            if not selected <= allowed:
                raise HTTPException(422, 'Select passages from the current candidate list')
            if db.execute("SELECT 1 FROM corrections WHERE recording_id=? AND state IN ('queued','running','proposed')", (rid,)).fetchone():
                raise HTTPException(409, 'Review the existing correction first')
            if not db.execute("SELECT 1 FROM artifacts WHERE recording_id=? AND name='audio.wav'", (rid,)).fetchone():
                raise HTTPException(409, 'Retained audio is required')
            for sid in selected:
                row = db.execute('SELECT * FROM segments WHERE id=?', (sid,)).fetchone()
                if row['end'] - row['start'] > 60 or row['end'] <= row['start']:
                    raise HTTPException(422, 'Correction passages must be between zero and 60 seconds')
                db.execute('INSERT INTO corrections(id,recording_id,segment_id,original,hint,state,created) VALUES(?,?,?,?,?,?,?)',
                           (identity(), rid, sid, row['text'], body.hint.strip(), 'queued', time.time()))
        return {'queued': len(selected)}

    @app.get('/api/recordings/{rid}/corrections', response_model=list[CorrectionView])
    def list_corrections(rid: str):
        recording_or_404(rid)
        with store().db() as db:
            return [dict(r, proposal=json.loads(r['proposal']) if r['proposal'] else None) for r in db.execute(
                'SELECT c.id,c.segment_id,c.original,c.hint,c.state,c.proposal,c.error,s.start,s.end FROM corrections c JOIN segments s ON s.id=c.segment_id WHERE c.recording_id=? ORDER BY c.created DESC', (rid,))]

    @app.post('/api/recordings/{rid}/corrections/{cid}/{action}')
    def review(rid: str, cid: str, action: str, background_tasks: BackgroundTasks):
        recording_or_404(rid)
        if action not in {'accept', 'dismiss'}:
            raise HTTPException(404)
        with store().db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM corrections WHERE id=? AND recording_id=?', (cid, rid)).fetchone()
            if not row or (action == 'accept' and row['state'] != 'proposed') or row['state'] not in {'queued', 'running', 'proposed', 'failed'}:
                raise HTTPException(409, 'Correction is no longer reviewable')
            if action == 'accept':
                original = db.execute('SELECT * FROM segments WHERE id=?', (row['segment_id'],)).fetchone()
                if original['text'] != row['original']:
                    raise HTTPException(409, 'Transcript changed; dismiss and retry this correction')
                proposal = json.loads(row['proposal'])
                text = ' '.join(s['text'].strip() for s in proposal)
                words = [w for s in proposal for w in s['words']]
                try:
                    Segment(start=original['start'], end=original['end'], text=text, words=words)
                except ValueError as exc:
                    raise HTTPException(422, 'Proposal exceeds passage text or word limits') from exc
                db.execute('UPDATE segments SET text=?,words=? WHERE id=?', (text, json.dumps(words), row['segment_id']))
                db.execute('DELETE FROM segment_search WHERE segment_id=?', (row['segment_id'],))
                db.execute('INSERT INTO segment_search VALUES(?,?,?)', (text, row['segment_id'], rid))
                db.execute('DELETE FROM embedding_versions WHERE recording_id=?', (rid,))
                db.execute('DELETE FROM embeddings WHERE recording_id=?', (rid,))
            db.execute('UPDATE corrections SET state=? WHERE id=?', ('accepted' if action == 'accept' else 'dismissed', cid))
        if action == 'accept' and embeddings.configured:
            background_tasks.add_task(embeddings.refresh, store(), rid)
        return {'status': 'accepted' if action == 'accept' else 'dismissed'}

    @app.post('/api/worker/corrections/claim', dependencies=[Depends(worker_auth)])
    def claim():
        now = time.time()
        with store().db() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute("SELECT 1 FROM jobs WHERE state='running' AND lease_until>?", (now,)).fetchone() or db.execute("SELECT 1 FROM corrections WHERE state='running' AND lease_until>?", (now,)).fetchone():
                return None
            db.execute("UPDATE corrections SET state='failed',error='Lease retries exhausted' WHERE state='running' AND lease_until<=? AND attempts>=3", (now,))
            row = db.execute("SELECT c.*,s.start,s.end,r.language,r.duration AS recording_duration FROM corrections c JOIN segments s ON s.id=c.segment_id JOIN recordings r ON r.id=c.recording_id WHERE c.state='queued' OR (c.state='running' AND c.lease_until<=? AND c.attempts<3) ORDER BY c.created LIMIT 1", (now,)).fetchone()
            if not row:
                return None
            token = identity()
            db.execute("UPDATE corrections SET state='running',lease_id=?,lease_until=?,attempts=attempts+1 WHERE id=?", (token, now + 120, row['id']))
            return dict(row, lease_id=token)

    @app.post('/api/worker/corrections/{cid}/heartbeat', dependencies=[Depends(worker_auth)])
    def heartbeat(cid: str, body: Heartbeat, x_lease_id: str = Header()):
        with store().db() as db:
            db.execute('BEGIN IMMEDIATE')
            lease(db, cid, x_lease_id)
            db.execute('UPDATE corrections SET lease_until=? WHERE id=?', (time.time() + 120, cid))
        return {'lease_seconds': 120}

    @app.get('/api/worker/corrections/{cid}/audio', dependencies=[Depends(worker_auth)])
    def audio(cid: str, x_lease_id: str = Header()):
        with store().db() as db:
            row = lease(db, cid, x_lease_id)
            artifact = db.execute("SELECT path FROM artifacts WHERE recording_id=? AND name='audio.wav'", (row['recording_id'],)).fetchone()
            if not artifact:
                raise HTTPException(409, 'Retained audio is unavailable')
        return FileResponse(artifact['path'], media_type='audio/wav')

    @app.post('/api/worker/corrections/{cid}/complete', dependencies=[Depends(worker_auth)])
    def complete(cid: str, body: CorrectionCompletion, x_lease_id: str = Header()):
        with store().db() as db:
            db.execute('BEGIN IMMEDIATE')
            previous = db.execute('SELECT * FROM corrections WHERE id=?', (cid,)).fetchone()
            proposal = json.dumps([s.model_dump() for s in body.segments])
            if previous and previous['state'] == 'proposed' and previous['lease_id'] == x_lease_id and previous['proposal'] == proposal:
                return {'status': 'proposed'}
            row = lease(db, cid, x_lease_id)
            if any(s.start < row['start'] - .01 or s.end > row['end'] + .01 for s in body.segments) or any(b.start < a.start for a, b in zip(body.segments, body.segments[1:])):
                raise HTTPException(422, 'Proposal must remain inside the selected passage')
            try:
                Segment(start=row['start'], end=row['end'], text=' '.join(s.text.strip() for s in body.segments), words=[w for s in body.segments for w in s.words])
            except ValueError as exc:
                raise HTTPException(422, 'Proposal exceeds passage text or word limits') from exc
            db.execute("UPDATE corrections SET state='proposed',proposal=? WHERE id=?", (json.dumps([s.model_dump() for s in body.segments]), cid))
        return {'status': 'proposed'}

    @app.post('/api/worker/corrections/{cid}/fail', dependencies=[Depends(worker_auth)])
    def fail(cid: str, body: Failure, x_lease_id: str = Header()):
        with store().db() as db:
            db.execute('BEGIN IMMEDIATE')
            lease(db, cid, x_lease_id)
            db.execute("UPDATE corrections SET state='failed',error=? WHERE id=?", (body.message, cid))
        return {'status': 'failed'}
