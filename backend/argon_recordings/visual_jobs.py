"""Bounded source-derived visual tasks. Legacy ASR artifacts stay unchanged."""
import hashlib
import json
import shutil
import time
from pathlib import Path
from typing import Literal

from fastapi import Depends, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import Field, model_validator

from .contracts import Strict, Heartbeat, Failure
from .evidence import snapshot
from .store import identity
from .timeline import SourceReference

RECIPE_VERSION = 'visual-v1'
MAX_BYTES = 128 * 1024**2
SCHEMA = '''
CREATE TABLE IF NOT EXISTS visual_jobs(
 id TEXT PRIMARY KEY,recording_id TEXT NOT NULL REFERENCES recordings(id),
 source_sha256 TEXT NOT NULL,recipe TEXT NOT NULL,cache_key TEXT NOT NULL UNIQUE,
 state TEXT NOT NULL,worker TEXT,lease_id TEXT,lease_until REAL,
 attempts INTEGER NOT NULL DEFAULT 0,progress REAL NOT NULL DEFAULT 0,
 error TEXT,result_sha TEXT,created REAL NOT NULL,requester TEXT);
CREATE TABLE IF NOT EXISTS visual_assets(
 id TEXT PRIMARY KEY REFERENCES visual_jobs(id),recording_id TEXT NOT NULL REFERENCES recordings(id),
 source_sha256 TEXT NOT NULL,recipe TEXT NOT NULL,manifest TEXT NOT NULL,path TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS visual_assets_recording ON visual_assets(recording_id);
'''


class Crop(Strict):
    x: float = Field(ge=0, lt=1)
    y: float = Field(ge=0, lt=1)
    width: float = Field(gt=0, le=1)
    height: float = Field(gt=0, le=1)

    @model_validator(mode='after')
    def fits(self):
        if self.x+self.width > 1 or self.y+self.height > 1:
            raise ValueError('Crop exceeds the source image')
        return self


class VisualRecipe(Strict):
    kind: Literal['image', 'clip'] = 'image'
    start: float = Field(ge=0)
    end: float = Field(ge=0)
    max_width: int = Field(default=1920, ge=64, le=3840)
    source_resolution: bool = False
    fps: int = Field(default=15, ge=1, le=30)
    crop: Crop | None = None

    @model_validator(mode='after')
    def bounds(self):
        if self.kind == 'image' and self.start != self.end:
            raise ValueError('A screenshot requires a point in time')
        if self.kind == 'clip' and not 0 < self.end-self.start <= 30:
            raise ValueError('Choose a clip between zero and 30 seconds')
        if self.kind == 'clip' and self.source_resolution:
            raise ValueError('Source resolution is available for screenshots only')
        if self.kind == 'image' and self.fps != 15:
            raise ValueError('Frame rate applies to clips only')
        return self


class VisualRequest(Strict):
    reference: SourceReference
    recipe: VisualRecipe


class VisualManifest(Strict):
    filename: Literal['image.jpg', 'clip.mp4']
    kind: Literal['image', 'clip']
    requested_start: float = Field(ge=0)
    requested_end: float = Field(ge=0)
    actual_start: float = Field(ge=0)
    actual_end: float = Field(ge=0)
    width: int = Field(ge=1, le=8192)
    height: int = Field(ge=1, le=8192)
    fps: float | None = Field(default=None, gt=0, le=30)
    source_width: int = Field(ge=1, le=16384)
    source_height: int = Field(ge=1, le=16384)
    source_fps: float | None = Field(default=None, gt=0, le=1000)
    rotation: float = 0
    output_frame_count: int = Field(default=1, ge=1, le=1000)
    source_avg_frame_rate: float | None = Field(default=None, gt=0, le=1000)
    source_nominal_frame_rate: float | None = Field(default=None, gt=0, le=1000)
    source_variable_frame_rate: bool = False
    source_rotation_degrees: int = Field(default=0, ge=-360, le=360)
    rotation_applied: bool = False
    source_sample_aspect_ratio: str = Field(default='1:1', max_length=32, pattern=r'^[0-9]+:[0-9]+$')
    source_display_sample_aspect_ratio: float = Field(default=1, gt=0)
    source_display_aspect_ratio: float = Field(default=1, gt=0)
    source_coded_width: int = Field(default=1, ge=1, le=16384)
    source_coded_height: int = Field(default=1, ge=1, le=16384)
    source_pts_start: float = Field(default=0, ge=0)
    source_pts_end: float = Field(default=0, ge=0)
    crop: Crop | None = None
    sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    bytes: int = Field(gt=0, le=MAX_BYTES)
    recipe_version: Literal['visual-v1']
    output_sample_aspect_ratio: str = Field(default='1:1', max_length=32, pattern=r'^[0-9]+:[0-9]+$')
    output_display_aspect_ratio: float = Field(default=1, gt=0)
    requested_fps: float | None = Field(default=None, gt=0, le=30)
    sampling_fps_ceiling: float | None = Field(default=None, gt=0, le=30)
    source_origin_time: float = 0
    source_video_start_time: float = 0


class VisualJob(Strict):
    id: str
    recording_id: str
    state: Literal['queued', 'running', 'completed', 'failed', 'cancelled']
    progress: float
    error: str | None


class VisualAsset(Strict):
    id: str
    recording_id: str
    source_sha256: str
    recipe: VisualRecipe
    manifest: VisualManifest
    url: str


def job_view(row):
    return VisualJob(**{k: row[k] for k in ('id','recording_id','state','progress','error')})


def admit_visual(db, store, rid, requester, reserve):
    """Extraction and analysis share admission and disk reservation budgets."""
    active = project_active = user_active = 0
    for table in ('visual_jobs', 'analysis_jobs'):
        active += db.execute(f"SELECT count(*) FROM {table} WHERE state IN ('queued','running')").fetchone()[0]
        project_active += db.execute(f"SELECT count(*) FROM {table} v JOIN recordings r ON r.id=v.recording_id WHERE v.state IN ('queued','running') AND r.project_id=(SELECT project_id FROM recordings WHERE id=?)", (rid,)).fetchone()[0]
        user_active += db.execute(f"SELECT count(*) FROM {table} WHERE state IN ('queued','running') AND requester=?", (requester,)).fetchone()[0]
    if active >= 32 or project_active >= 8 or user_active >= 8:
        raise HTTPException(429, 'Visual queue is full; wait for existing tasks')
    if shutil.disk_usage(store.root).free < reserve+(active+1)*MAX_BYTES:
        raise HTTPException(507, 'Insufficient reserved space for visual processing')


def install(app, get_store, worker_auth, receive, reserve):
    def leased(db, jid, token, request, completed=False):
        row = db.execute('SELECT * FROM visual_jobs WHERE id=?', (jid,)).fetchone()
        worker = getattr(request.state, 'worker_id', None)
        if not row or row['lease_id'] != token or (worker is not None and row['worker'] != worker):
            raise HTTPException(409, 'Visual task lease expired or superseded')
        if completed and row['state'] == 'completed': return row
        if row['state'] != 'running' or row['lease_until'] <= time.time():
            raise HTTPException(409, 'Visual task lease expired or superseded')
        return row

    @app.post('/api/recordings/{rid}/visual-jobs', response_model=VisualJob)
    def schedule(rid: str, body: VisualRequest, request: Request):
        store = get_store()
        with store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            state = snapshot(db, store, rid)
            if body.reference.recording_id != rid or body.reference.source_sha256 != state.source_sha256:
                raise HTTPException(409, 'Recording source changed; refresh before extraction')
            recipe = body.recipe
            if state.duration is None or recipe.start >= state.duration or recipe.end > state.duration or recipe.start < body.reference.interval.start or recipe.end > body.reference.interval.end:
                raise HTTPException(422, 'Select a time within the available recording')
            key = hashlib.sha256(json.dumps([rid,state.source_sha256,RECIPE_VERSION,recipe.model_dump()],sort_keys=True).encode()).hexdigest()
            previous = db.execute('SELECT * FROM visual_jobs WHERE cache_key=?', (key,)).fetchone()
            if previous and previous['state'] in {'queued','running'}:
                return job_view(previous)
            if previous and previous['state'] == 'completed':
                asset = db.execute('SELECT path FROM visual_assets WHERE id=?',(previous['id'],)).fetchone()
                if asset and Path(asset['path']).is_file(): return job_view(previous)
            if not state.source_available:
                raise HTTPException(409, 'Original source is unavailable; restoration is required')
            if (store.root/'sources'/rid).stat().st_size > 8*1024**3:
                raise HTTPException(422, 'Original source exceeds the eight GiB visual input limit')
            if not db.execute("SELECT 1 FROM artifacts WHERE recording_id=? AND kind='frame'", (rid,)).fetchone():
                raise HTTPException(409, 'No video references are available for this recording')
            account = getattr(request.state,'account',None)
            requester = account['id'] if account else 'local'
            admit_visual(db,store,rid,requester,reserve)
            if previous:
                jid = previous['id']
                db.execute("UPDATE visual_jobs SET state='queued',attempts=0,progress=0,error=NULL,lease_id=NULL,lease_until=NULL,result_sha=NULL,worker=NULL,requester=? WHERE id=?", (requester,jid))
            else:
                if db.execute('SELECT count(*) FROM visual_jobs WHERE recording_id=?', (rid,)).fetchone()[0] >= 100:
                    raise HTTPException(409, 'Visual task history limit reached')
                jid = identity()
                db.execute('INSERT INTO visual_jobs(id,recording_id,source_sha256,recipe,cache_key,state,created,requester) VALUES(?,?,?,?,?,?,?,?)',
                           (jid,rid,state.source_sha256,recipe.model_dump_json(),key,'queued',time.time(),requester))
            return job_view(db.execute('SELECT * FROM visual_jobs WHERE id=?', (jid,)).fetchone())

    @app.get('/api/recordings/{rid}/visual-jobs', response_model=list[VisualJob])
    def jobs(rid: str):
        store = get_store()
        if not store.recording(rid): raise HTTPException(404, 'Recording not found')
        with store.db() as db:
            return [job_view(r) for r in db.execute('SELECT * FROM visual_jobs WHERE recording_id=? ORDER BY created DESC', (rid,))]

    @app.post('/api/recordings/{rid}/visual-jobs/{jid}/cancel', response_model=VisualJob)
    def cancel(rid: str, jid: str):
        with get_store().db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM visual_jobs WHERE recording_id=? AND id=?', (rid,jid)).fetchone()
            if not row: raise HTTPException(404, 'Visual task not found')
            if row['state'] not in {'queued','running','cancelled'}:
                raise HTTPException(409, 'Visual task already finished')
            db.execute("UPDATE visual_jobs SET state='cancelled',lease_id=NULL,lease_until=NULL WHERE id=?", (jid,))
            return job_view(db.execute('SELECT * FROM visual_jobs WHERE id=?', (jid,)).fetchone())

    @app.get('/api/recordings/{rid}/visual-evidence', response_model=list[VisualAsset])
    def assets(rid: str, offset: int = Query(default=0,ge=0), limit: int = Query(default=50,ge=1,le=100)):
        store = get_store()
        if not store.recording(rid): raise HTTPException(404, 'Recording not found')
        with store.db() as db:
            return [VisualAsset(id=r['id'],recording_id=rid,source_sha256=r['source_sha256'],
                recipe=json.loads(r['recipe']),manifest=json.loads(r['manifest']),
                url=f'/api/recordings/{rid}/visual-evidence/{r["id"]}/media')
                for r in db.execute("SELECT * FROM visual_assets WHERE recording_id=? ORDER BY json_extract(manifest,'$.actual_start'),id LIMIT ? OFFSET ?", (rid,limit,offset))]

    @app.get('/api/recordings/{rid}/visual-evidence/{aid}/media')
    def media(rid: str, aid: str):
        with get_store().db() as db:
            row = db.execute('SELECT * FROM visual_assets WHERE recording_id=? AND id=?', (rid,aid)).fetchone()
            if not row: raise HTTPException(404, 'Visual evidence not found')
            if not Path(row['path']).is_file(): raise HTTPException(410, 'Visual media unavailable; request extraction again')
            kind = json.loads(row['manifest'])['kind']
            return FileResponse(row['path'],media_type='image/jpeg' if kind=='image' else 'video/mp4',
                headers={'Cache-Control':'private, max-age=3600','X-Content-Type-Options':'nosniff'})

    @app.post('/api/worker/visual/claim', dependencies=[Depends(worker_auth)])
    def claim(request: Request):
        store = get_store(); now = time.time()
        worker = getattr(request.state,'worker_id',None)
        with store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute("UPDATE visual_jobs SET state='failed',error='Visual lease retries exhausted' WHERE state='running' AND lease_until<=? AND attempts>=3", (now,))
            if not store.capacity_available(db,worker): return None
            row = db.execute("SELECT * FROM visual_jobs WHERE state='queued' OR (state='running' AND lease_until<=? AND attempts<3) ORDER BY created LIMIT 1", (now,)).fetchone()
            if not row: return None
            token = identity()
            db.execute("UPDATE visual_jobs SET state='running',worker=?,lease_id=?,lease_until=?,attempts=attempts+1 WHERE id=?", (worker,token,now+120,row['id']))
            recording = db.execute('SELECT bytes FROM recordings WHERE id=?',(row['recording_id'],)).fetchone()
            return {'id':row['id'],'lease_id':token,'source_sha256':row['source_sha256'],'source_bytes':recording['bytes'],
                    'recipe':json.loads(row['recipe']),'recipe_version':RECIPE_VERSION}

    @app.post('/api/worker/visual/{jid}/heartbeat', dependencies=[Depends(worker_auth)])
    def heartbeat(jid: str, body: Heartbeat, request: Request, x_lease_id: str = Header()):
        with get_store().db() as db:
            db.execute('BEGIN IMMEDIATE'); leased(db,jid,x_lease_id,request)
            db.execute('UPDATE visual_jobs SET lease_until=?,progress=? WHERE id=?',(time.time()+120,body.progress,jid))
        return {'ok':True}

    @app.get('/api/worker/visual/{jid}/source', dependencies=[Depends(worker_auth)])
    def source(jid: str, request: Request, x_lease_id: str = Header()):
        store = get_store()
        with store.db() as db:
            row = leased(db,jid,x_lease_id,request)
            path = store.root/'sources'/row['recording_id']
            if not path.is_file(): raise HTTPException(409,'Original source unavailable; restoration is required')
            return FileResponse(path,media_type='application/octet-stream')

    @app.put('/api/worker/visual/{jid}/media', dependencies=[Depends(worker_auth)])
    async def upload(jid: str, request: Request, x_lease_id: str = Header()):
        store = get_store()
        with store.db() as db: leased(db,jid,x_lease_id,request)
        directory = store.root/'results'/'visual'/jid/x_lease_id
        directory.mkdir(parents=True,exist_ok=True)
        temporary = directory/(identity()+'.part')
        try:
            await receive(request,temporary,MAX_BYTES)
            with store.db() as db:
                db.execute('BEGIN IMMEDIATE'); leased(db,jid,x_lease_id,request)
                temporary.replace(directory/'media')
        finally: temporary.unlink(missing_ok=True)
        return {'ok':True}

    @app.post('/api/worker/visual/{jid}/complete', dependencies=[Depends(worker_auth)])
    def complete(jid: str, body: VisualManifest, request: Request, x_lease_id: str = Header()):
        store = get_store(); digest = hashlib.sha256(body.model_dump_json().encode()).hexdigest()
        with store.db() as db:
            db.execute('BEGIN IMMEDIATE'); row = leased(db,jid,x_lease_id,request,completed=True)
            if row['state']=='completed':
                if row['result_sha'] != digest: raise HTTPException(409,'Visual publication already differs')
                return {'ok':True}
            recipe = VisualRecipe.model_validate_json(row['recipe'])
            recording = db.execute('SELECT sha256,duration FROM recordings WHERE id=?',(row['recording_id'],)).fetchone()
            if recording['sha256'] != row['source_sha256']: raise HTTPException(409,'Source changed during extraction')
            if (body.kind != recipe.kind or body.filename != ('image.jpg' if recipe.kind=='image' else 'clip.mp4') or
                body.requested_start != recipe.start or body.requested_end != recipe.end or
                body.actual_end < body.actual_start or body.actual_end > recording['duration']+.1 or
                abs(body.actual_start-recipe.start)>1 or (not recipe.source_resolution and body.width>recipe.max_width) or
                body.crop != recipe.crop or
                body.width*body.height>16_777_216 or
                (body.kind=='clip' and (body.fps is None or body.fps>recipe.fps or body.actual_end-body.actual_start>30.1))):
                raise HTTPException(422,'Visual manifest does not match bounded recipe')
            path = store.root/'results'/'visual'/jid/x_lease_id/'media'
            if not path.is_file() or path.stat().st_size != body.bytes: raise HTTPException(422,'Visual media size mismatch')
            with path.open('rb') as stream:
                prefix = stream.read(12); stream.seek(0); actual_hash = hashlib.file_digest(stream,'sha256').hexdigest()
            if actual_hash != body.sha256 or (body.kind=='image' and not prefix.startswith(b'\xff\xd8\xff')) or (body.kind=='clip' and prefix[4:8]!=b'ftyp'):
                raise HTTPException(422,'Visual media integrity/type mismatch')
            db.execute('INSERT INTO visual_assets VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET manifest=excluded.manifest,path=excluded.path',
                       (jid,row['recording_id'],row['source_sha256'],row['recipe'],body.model_dump_json(),str(path)))
            db.execute('UPDATE evidence_versions SET extraction=extraction+1 WHERE recording_id=?',(row['recording_id'],))
            db.execute("UPDATE visual_jobs SET state='completed',progress=1,result_sha=? WHERE id=?",(digest,jid))
        return {'ok':True}

    @app.post('/api/worker/visual/{jid}/fail', dependencies=[Depends(worker_auth)])
    def fail(jid: str, body: Failure, request: Request, x_lease_id: str = Header()):
        with get_store().db() as db:
            db.execute('BEGIN IMMEDIATE'); leased(db,jid,x_lease_id,request)
            db.execute("UPDATE visual_jobs SET state='failed',error=? WHERE id=?",(body.message,jid))
        return {'ok':True}

    return {'schedule': schedule, 'assets': assets, 'media': media, 'cancel': cancel}
