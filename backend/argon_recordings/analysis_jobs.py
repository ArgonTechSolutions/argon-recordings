"""Durable adaptive proposals and explicit review, separate from media publication."""
import hashlib
import json
import time
from typing import Literal

from fastapi import Depends, Header, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import Field, model_validator

from .contracts import Strict, Heartbeat, Failure
from .evidence import MarkInput, MarkMutation, check_revision, snapshot
from .store import identity
from .timeline import SourceReference, TimelineRange
from .visual_analysis import ANALYSIS_RECIPE_VERSION, MAX_ANALYSIS_BYTES, MAX_ANALYSIS_FRAMES
from .visual_jobs import admit_visual, job_view, VisualJob

SCHEMA = '''
CREATE TABLE IF NOT EXISTS analysis_jobs(
 id TEXT PRIMARY KEY,recording_id TEXT NOT NULL REFERENCES recordings(id),
 source_sha256 TEXT NOT NULL,interval TEXT NOT NULL,cache_key TEXT NOT NULL UNIQUE,
 state TEXT NOT NULL,worker TEXT,lease_id TEXT,lease_until REAL,
 attempts INTEGER NOT NULL DEFAULT 0,progress REAL NOT NULL DEFAULT 0,
 error TEXT,result_sha TEXT,result TEXT,created REAL NOT NULL,requester TEXT);
CREATE INDEX IF NOT EXISTS analysis_jobs_recording ON analysis_jobs(recording_id);
CREATE TABLE IF NOT EXISTS analysis_decisions(
 job_id TEXT NOT NULL REFERENCES analysis_jobs(id),ordinal INTEGER NOT NULL,
 choice TEXT NOT NULL,mark_id TEXT REFERENCES visual_marks(id),
 PRIMARY KEY(job_id,ordinal));
'''


class AnalysisRequest(Strict):
    reference: SourceReference


class ProposalMetrics(Strict):
    global_change: float | None = Field(default=None, ge=0, le=255)
    changed_region_ratio: float | None = Field(default=None, ge=0, le=1)
    rolling_changed_cells: int | None = Field(default=None, ge=0, le=336)
    event_type: Literal['scene', 'localized-change'] | None = None
    sustained_motion: bool | None = None
    stability_score: float | None = Field(default=None, ge=0, le=1)


class Proposal(Strict):
    type: Literal['image', 'clip']
    source_start: float = Field(ge=0)
    source_end: float = Field(ge=0)
    timestamp: float = Field(ge=0)
    score: float = Field(ge=0, le=1)
    reason: str = Field(min_length=1, max_length=256)
    metrics: ProposalMetrics
    recipe_version: Literal['visual-analysis-v1']
    extraction_recipe_version: Literal['visual-v1']
    fallback: bool

    @model_validator(mode='after')
    def bounded(self):
        if (not self.source_start <= self.timestamp <= self.source_end or
            (self.type == 'image' and self.source_start != self.source_end) or
            (self.type == 'clip' and not 0 < self.source_end-self.source_start <= 30)):
            raise ValueError('Proposal requires a source point or clip of at most 30 seconds')
        return self


class AnalysisCounts(Strict):
    detected_events: int = Field(ge=0, le=MAX_ANALYSIS_FRAMES)
    cursor_suppressed: int = Field(ge=0, le=MAX_ANALYSIS_FRAMES)
    deduplicated_static: int = Field(ge=0, le=MAX_ANALYSIS_FRAMES)


class AnalysisBudgets(Strict):
    frames_decoded: int = Field(ge=0, le=MAX_ANALYSIS_FRAMES)
    frame_limit: Literal[602]
    analysis_bytes: int = Field(ge=0, le=MAX_ANALYSIS_BYTES)
    analysis_byte_limit: Literal[22228992]
    elapsed_seconds: float = Field(ge=0, le=180)
    wall_seconds_limit: Literal[45]
    analysis_width: int = Field(ge=1, le=256)
    analysis_height: int = Field(ge=1, le=144)
    sample_fps: Literal[2]


class DecodedInterval(Strict):
    start: float | None = Field(ge=0)
    end: float | None = Field(ge=0)


class AnalysisResult(Strict):
    status: Literal['complete', 'partial']
    partial_reason: str | None = Field(max_length=256)
    analysis_recipe_version: Literal['visual-analysis-v1']
    extraction_recipe_version: Literal['visual-v1']
    requested_interval: TimelineRange
    scanned_interval: TimelineRange
    decoded_interval: DecodedInterval
    proposals: list[Proposal] = Field(max_length=48)
    counts: AnalysisCounts
    budgets: AnalysisBudgets

    @model_validator(mode='after')
    def bounded(self):
        r, s, d = self.requested_interval, self.scanned_interval, self.decoded_interval
        if (r.start >= r.end or s.start != r.start or s.end > min(r.end, r.start+300) or
            (self.status == 'partial' and not self.partial_reason) or
            (self.status == 'complete' and (s.end < r.end or self.partial_reason))):
            raise ValueError('Analysis coverage does not match its bounded interval')
        if (d.start is None) != (d.end is None):
            raise ValueError('Decoded coverage requires both endpoints')
        if d.start is not None and not r.start <= d.start <= d.end <= s.end:
            raise ValueError('Decoded coverage exceeds scan')
        for p in self.proposals:
            if (not r.start <= p.source_start <= p.source_end <= r.end or
                (p.type == 'image' and p.source_start >= r.end)):
                raise ValueError('Proposal exceeds requested source interval')
            if not p.fallback and (p.source_start < s.start or p.source_end > s.end):
                raise ValueError('Adaptive proposal exceeds scanned source interval')
        return self


class ProposalDecision(MarkMutation):
    choice: Literal['accept', 'reject']


class ReviewedProposal(Proposal):
    ordinal: int
    choice: Literal['accept', 'reject'] | None = None
    mark_id: str | None = None
    mark_active: bool = False
    manual_conflict: bool = False


class AnalysisDetail(VisualJob):
    source_sha256: str
    interval: TimelineRange
    result: AnalysisResult | None
    proposals: list[ReviewedProposal]


def overlaps(a, b):
    if a.start == a.end: return b.start <= a.start <= b.end if b.start == b.end else b.start <= a.start < b.end
    if b.start == b.end: return a.start <= b.start < a.end
    return a.start < b.end and b.start < a.end


def protected(db, rid, interval):
    rows = db.execute('SELECT payload FROM visual_marks WHERE recording_id=? AND deleted=0', (rid,))
    return any((m.pinned or m.policy != 'auto') and overlaps(m.interval, interval)
               for m in (MarkInput.model_validate_json(row['payload']) for row in rows))


def install(app, get_store, worker_auth, reserve):
    def row_for(db, rid, jid):
        row = db.execute('SELECT * FROM analysis_jobs WHERE recording_id=? AND id=?', (rid,jid)).fetchone()
        if not row: raise HTTPException(404, 'Analysis not found')
        return row

    def leased(db, jid, token, request, completed=False):
        row = db.execute('SELECT * FROM analysis_jobs WHERE id=?', (jid,)).fetchone()
        worker = getattr(request.state,'worker_id',None)
        if not row or row['lease_id'] != token or (worker is not None and row['worker'] != worker):
            raise HTTPException(409, 'Analysis lease expired or superseded')
        if completed and row['state'] == 'completed': return row
        if row['state'] != 'running' or row['lease_until'] <= time.time():
            raise HTTPException(409, 'Analysis lease expired or superseded')
        return row

    def detail(db, row):
        result = AnalysisResult.model_validate_json(row['result']) if row['result'] else None
        proposals = []
        for ordinal, p in enumerate(result.proposals if result else []):
            decision = db.execute('SELECT * FROM analysis_decisions WHERE job_id=? AND ordinal=?', (row['id'],ordinal)).fetchone()
            mark = db.execute('SELECT deleted FROM visual_marks WHERE id=?', (decision['mark_id'],)).fetchone() if decision and decision['mark_id'] else None
            proposals.append(ReviewedProposal(**p.model_dump(),ordinal=ordinal,
                choice=decision['choice'] if decision else None,mark_id=decision['mark_id'] if decision else None,
                mark_active=bool(mark and not mark['deleted']),
                manual_conflict=protected(db,row['recording_id'],TimelineRange(start=p.source_start,end=p.source_end)) if not decision else False))
        return AnalysisDetail(**job_view(row).model_dump(),source_sha256=row['source_sha256'],
                              interval=json.loads(row['interval']),result=result,proposals=proposals)

    @app.post('/api/recordings/{rid}/analysis-jobs', response_model=VisualJob)
    def schedule(rid: str, body: AnalysisRequest, request: Request):
        store = get_store()
        with store.db() as db:
            db.execute('BEGIN IMMEDIATE'); state = snapshot(db,store,rid); ref = body.reference
            if ref.recording_id != rid or ref.source_sha256 != state.source_sha256:
                raise HTTPException(409, 'Source changed; refresh before analysis')
            if state.duration is None or not 0 <= ref.interval.start < ref.interval.end <= state.duration:
                raise HTTPException(422, 'Select an interval within the recording')
            key = hashlib.sha256(json.dumps([rid,state.source_sha256,ANALYSIS_RECIPE_VERSION,ref.interval.model_dump()],sort_keys=True).encode()).hexdigest()
            previous = db.execute('SELECT * FROM analysis_jobs WHERE cache_key=?', (key,)).fetchone()
            if previous and previous['state'] in {'queued','running','completed'}: return job_view(previous)
            source = store.root/'sources'/rid
            if not state.source_available: raise HTTPException(409, 'Original source unavailable; restoration is required')
            if source.stat().st_size > 8*1024**3: raise HTTPException(422, 'Source exceeds the eight GiB visual input limit')
            if not db.execute("SELECT 1 FROM artifacts WHERE recording_id=? AND kind='frame'", (rid,)).fetchone():
                raise HTTPException(409, 'No video references are available')
            account = getattr(request.state,'account',None); requester = account['id'] if account else 'local'
            admit_visual(db,store,rid,requester,reserve)
            if previous:
                jid = previous['id']
                db.execute("UPDATE analysis_jobs SET state='queued',attempts=0,progress=0,error=NULL,lease_id=NULL,lease_until=NULL,worker=NULL,requester=? WHERE id=?", (requester,jid))
            else:
                if db.execute('SELECT count(*) FROM analysis_jobs WHERE recording_id=?', (rid,)).fetchone()[0] >= 100:
                    raise HTTPException(409, 'Analysis history limit reached')
                jid = identity()
                db.execute('INSERT INTO analysis_jobs(id,recording_id,source_sha256,interval,cache_key,state,created,requester) VALUES(?,?,?,?,?,?,?,?)',
                           (jid,rid,state.source_sha256,ref.interval.model_dump_json(),key,'queued',time.time(),requester))
            return job_view(row_for(db,rid,jid))

    @app.get('/api/recordings/{rid}/analysis-jobs', response_model=list[VisualJob])
    def jobs(rid: str):
        store = get_store()
        if not store.recording(rid): raise HTTPException(404, 'Recording not found')
        with store.db() as db:
            return [job_view(r) for r in db.execute('SELECT * FROM analysis_jobs WHERE recording_id=? ORDER BY created DESC',(rid,))]

    @app.get('/api/recordings/{rid}/analysis-jobs/{jid}', response_model=AnalysisDetail)
    def get_job(rid: str, jid: str):
        with get_store().db() as db: return detail(db,row_for(db,rid,jid))

    @app.post('/api/recordings/{rid}/analysis-jobs/{jid}/cancel', response_model=VisualJob)
    def cancel(rid: str, jid: str):
        with get_store().db() as db:
            db.execute('BEGIN IMMEDIATE'); row = row_for(db,rid,jid)
            if row['state'] not in {'queued','running','cancelled'}: raise HTTPException(409,'Analysis already finished')
            db.execute("UPDATE analysis_jobs SET state='cancelled',lease_id=NULL,lease_until=NULL WHERE id=?",(jid,))
            return job_view(row_for(db,rid,jid))

    @app.post('/api/recordings/{rid}/analysis-jobs/{jid}/proposals/{ordinal}', response_model=AnalysisDetail)
    def decide(rid: str, jid: str, ordinal: int, body: ProposalDecision):
        store = get_store()
        with store.db() as db:
            db.execute('BEGIN IMMEDIATE'); state = check_revision(db,store,rid,body); row = row_for(db,rid,jid)
            if row['state'] != 'completed' or state.source_sha256 != row['source_sha256']:
                raise HTTPException(409,'Analysis is incomplete or source changed')
            result = AnalysisResult.model_validate_json(row['result'])
            if not 0 <= ordinal < len(result.proposals): raise HTTPException(404,'Proposal not found')
            existing = db.execute('SELECT choice FROM analysis_decisions WHERE job_id=? AND ordinal=?',(jid,ordinal)).fetchone()
            if existing:
                if existing['choice'] != body.choice: raise HTTPException(409,'Proposal already reviewed')
                return detail(db,row)
            p = result.proposals[ordinal]; interval = TimelineRange(start=p.source_start,end=p.source_end); mid = None
            if body.choice == 'accept':
                if protected(db,rid,interval): raise HTTPException(409,'A manual or pinned mark already covers this proposal')
                if state.duration is None or interval.end > state.duration: raise HTTPException(409,'Recording timeline changed')
                if (db.execute('SELECT count(*) FROM visual_marks WHERE recording_id=?',(rid,)).fetchone()[0] >= 1000 or
                    db.execute('SELECT count(*) FROM visual_mark_history WHERE recording_id=?',(rid,)).fetchone()[0] >= 5000):
                    raise HTTPException(409,'Annotation history limit reached')
                mid = identity(); revision = state.versions.annotations+1
                mark = MarkInput(interval=interval,label='Suggested still' if p.type=='image' else 'Suggested motion',policy='still' if p.type=='image' else 'motion')
                db.execute('INSERT INTO visual_mark_history VALUES(?,?,?,?,?,?,?,0)',(identity(),rid,mid,None,None,revision,time.time()))
                db.execute('INSERT INTO visual_marks VALUES(?,?,?,0)',(mid,rid,mark.model_dump_json()))
                db.execute('UPDATE evidence_versions SET annotations=? WHERE recording_id=?',(revision,rid))
            db.execute('INSERT INTO analysis_decisions VALUES(?,?,?,?)',(jid,ordinal,body.choice,mid))
            if body.choice == 'reject':
                db.execute('UPDATE evidence_versions SET annotations=annotations+1 WHERE recording_id=?',(rid,))
            return detail(db,row)

    @app.post('/api/worker/analysis/claim', dependencies=[Depends(worker_auth)])
    def claim(request: Request):
        store = get_store(); now = time.time(); worker = getattr(request.state,'worker_id',None)
        with store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute("UPDATE analysis_jobs SET state='failed',error='Analysis lease retries exhausted' WHERE state='running' AND lease_until<=? AND attempts>=3",(now,))
            if not store.capacity_available(db,worker): return None
            row = db.execute("SELECT * FROM analysis_jobs WHERE state='queued' OR (state='running' AND lease_until<=? AND attempts<3) ORDER BY created LIMIT 1",(now,)).fetchone()
            if not row: return None
            token = identity()
            db.execute("UPDATE analysis_jobs SET state='running',worker=?,lease_id=?,lease_until=?,attempts=attempts+1 WHERE id=?",(worker,token,now+120,row['id']))
            recording = db.execute('SELECT bytes FROM recordings WHERE id=?',(row['recording_id'],)).fetchone()
            return {'id':row['id'],'lease_id':token,'source_sha256':row['source_sha256'],'source_bytes':recording['bytes'],
                    'interval':json.loads(row['interval']),'analysis_recipe_version':ANALYSIS_RECIPE_VERSION}

    @app.post('/api/worker/analysis/{jid}/heartbeat', dependencies=[Depends(worker_auth)])
    def heartbeat(jid: str, body: Heartbeat, request: Request, x_lease_id: str = Header()):
        with get_store().db() as db:
            db.execute('BEGIN IMMEDIATE'); leased(db,jid,x_lease_id,request)
            db.execute('UPDATE analysis_jobs SET lease_until=?,progress=? WHERE id=?',(time.time()+120,body.progress,jid))
        return {'ok':True}

    @app.get('/api/worker/analysis/{jid}/source', dependencies=[Depends(worker_auth)])
    def source(jid: str, request: Request, x_lease_id: str = Header()):
        store = get_store()
        with store.db() as db:
            row = leased(db,jid,x_lease_id,request); path = store.root/'sources'/row['recording_id']
            if not path.is_file(): raise HTTPException(409,'Original source unavailable; restoration is required')
            return FileResponse(path,media_type='application/octet-stream')

    @app.post('/api/worker/analysis/{jid}/complete', dependencies=[Depends(worker_auth)])
    def complete(jid: str, body: AnalysisResult, request: Request, x_lease_id: str = Header()):
        encoded = body.model_dump_json()
        if len(encoded.encode()) > 256*1024: raise HTTPException(422,'Analysis result exceeds budget')
        digest = hashlib.sha256(encoded.encode()).hexdigest(); store = get_store()
        with store.db() as db:
            db.execute('BEGIN IMMEDIATE'); row = leased(db,jid,x_lease_id,request,completed=True)
            if row['state']=='completed':
                if row['result_sha'] != digest: raise HTTPException(409,'Analysis publication already differs')
                return {'ok':True}
            recording = db.execute('SELECT sha256,duration FROM recordings WHERE id=?',(row['recording_id'],)).fetchone()
            if recording['sha256'] != row['source_sha256']: raise HTTPException(409,'Source changed during analysis')
            if body.requested_interval != TimelineRange.model_validate_json(row['interval']) or body.requested_interval.end > recording['duration']:
                raise HTTPException(422,'Analysis does not match requested source interval')
            db.execute("UPDATE analysis_jobs SET state='completed',progress=1,result_sha=?,result=? WHERE id=?",(digest,encoded,jid))
        return {'ok':True}

    @app.post('/api/worker/analysis/{jid}/fail', dependencies=[Depends(worker_auth)])
    def fail(jid: str, body: Failure, request: Request, x_lease_id: str = Header()):
        with get_store().db() as db:
            db.execute('BEGIN IMMEDIATE'); leased(db,jid,x_lease_id,request)
            db.execute("UPDATE analysis_jobs SET state='failed',error=? WHERE id=?",(body.message,jid))
        return {'ok':True}
