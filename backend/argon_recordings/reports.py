"""Explicit editor-owned report snapshots and private, bounded evidence exports."""
import asyncio
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import threading
import time
from typing import Literal

from fastapi import HTTPException, Request
from fastapi.responses import StreamingResponse, FileResponse
from pydantic import Field

from .contracts import Strict
from .evidence import snapshot
from .report_contract import Report, Reference, Segment, InspectedImage, Generator, ReportDraft, ContractError, validate_citations
from .store import identity

MAX_IMAGE_BYTES = 4*1024**2
MAX_SNAPSHOT_BYTES = 4*1024**2
MAX_BUNDLE_BYTES = 64*1024**2
IMAGE_ID = r'^(?:asset:[a-f0-9]{32}|frame:frame-[0-9]{4}\.jpg)$'
SCHEMA = '''
CREATE TABLE IF NOT EXISTS report_versions(
 recording_id TEXT PRIMARY KEY REFERENCES recordings(id),revision INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS reports(
 id TEXT PRIMARY KEY,recording_id TEXT NOT NULL REFERENCES recordings(id),
 title TEXT NOT NULL,generator TEXT NOT NULL,payload TEXT NOT NULL,snapshot TEXT NOT NULL,
 status TEXT NOT NULL,revision INTEGER NOT NULL,created REAL NOT NULL,reviewed REAL);
CREATE INDEX IF NOT EXISTS reports_recording ON reports(recording_id);
CREATE TABLE IF NOT EXISTS report_archive(report_id TEXT PRIMARY KEY REFERENCES reports(id) ON DELETE CASCADE,archived REAL NOT NULL);
'''


class ExportResponse(StreamingResponse):
    """Release export slots/files even when ASGI send fails before background tasks."""
    def __init__(self,*args,cleanup,**kwargs):
        self.cleanup=cleanup
        super().__init__(*args,**kwargs)

    async def __call__(self,scope,receive,send):
        try: await super().__call__(scope,receive,send)
        finally: self.cleanup()


class SaveReport(ReportDraft):
    expected_revision: int = Field(ge=0)


class ReviewReport(Strict):
    expected_revision: int = Field(ge=0)


class RenameReport(Strict):
    title: str = Field(min_length=1, max_length=150, pattern=r'\S')
    expected_revision: int = Field(ge=0)


class ArchiveReport(Strict):
    archived: bool
    expected_revision: int = Field(ge=0)


class DeleteReport(Strict):
    confirm: Literal['delete-permanently']
    expected_revision: int = Field(ge=0)


class ReportView(Strict):
    id: str
    title: str
    status: Literal['draft','reviewed']
    revision: int
    stale: bool
    created: float
    reviewed: float | None
    archived: bool = False
    generator: Generator


class ReportsList(Strict):
    archived_count: int = 0
    revision: int
    reports: list[ReportView]


class ReportDetail(ReportView):
    report: Report
    selected_image_count: int


def reference_matches(left,right):
    """Appending derived media does not change existing transcript/image evidence."""
    def stable(ref):
        value=ref.model_dump() if hasattr(ref,'model_dump') else json.loads(json.dumps(ref))
        value['versions'].pop('extraction',None)
        return value
    return stable(left)==stable(right)


def current_reference(db, store, rid):
    state = snapshot(db,store,rid)
    if state.source_sha256 is None or state.duration is None:
        raise HTTPException(409,'Recording evidence is not ready')
    return Reference(recording_id=rid,source_sha256=state.source_sha256,
                     interval={'start':0,'end':state.duration},versions=state.versions.model_dump())


def image_record(db, rid, image_id):
    """Resolve registered evidence only. Paths stay inside this server boundary."""
    if image_id.startswith('asset:'):
        row = db.execute('SELECT * FROM visual_assets WHERE recording_id=? AND id=?',(rid,image_id[6:])).fetchone()
        if not row: raise HTTPException(422,'Selected image is not registered for this recording')
        manifest=json.loads(row['manifest'])
        if manifest['kind']!='image': raise HTTPException(422,'Select a screenshot rather than a clip')
        return Path(row['path']),{'id':image_id,'source_sha256':row['source_sha256'],
            'timestamp':manifest['actual_start'],'recipe_version':manifest['recipe_version'],
            'recipe':json.loads(row['recipe']),'manifest':manifest}
    name=image_id[6:]
    row=db.execute("SELECT path,time FROM artifacts WHERE recording_id=? AND name=? AND kind='frame'",(rid,name)).fetchone()
    if not row: raise HTTPException(422,'Selected image is not registered for this recording')
    source=db.execute('SELECT sha256 FROM recordings WHERE id=?',(rid,)).fetchone()
    return Path(row['path']),{'id':image_id,'source_sha256':source['sha256'],'timestamp':row['time'],
        'recipe_version':'legacy-sparse-v1','recipe':{'kind':'image','time':row['time']},'manifest':None}


def checked_image(path):
    try:
        with path.open('rb') as stream:
            data=stream.read(MAX_IMAGE_BYTES+1)
    except OSError: raise HTTPException(410,'Selected image is unavailable; regenerate it before importing a new draft') from None
    if len(data)>MAX_IMAGE_BYTES: raise HTTPException(422,'Selected image exceeds four MiB; choose a smaller screenshot')
    if not data.startswith(b'\xff\xd8\xff'): raise HTTPException(422,'Selected image is not a JPEG')
    return data,hashlib.sha256(data).hexdigest()


def speaker_snapshot(db,rid,reference,scope):
    raw=db.execute('SELECT * FROM diarization_results WHERE recording_id=?',(rid,)).fetchone()
    stale=bool(raw and (raw['source_sha256']!=reference.source_sha256 or raw['transcript_version']!=reference.versions.transcript))
    turns=[t for t in json.loads(raw['turns']) if t['start']<scope.end and t['end']>scope.start] if raw and not stale else []
    overrides=[]
    for row in db.execute('SELECT id,payload FROM speaker_overrides WHERE recording_id=? AND deleted=0 ORDER BY revision,id',(rid,)):
        item=json.loads(row['payload']);interval=item['interval']
        if interval['start']<scope.end and interval['end']>scope.start:
            overrides.append({'id':row['id'],**item})
    ids={t['speaker'] for t in turns}|{o['speaker'] for o in overrides if o['speaker']}
    names=[dict(row) for row in db.execute('SELECT id,label FROM speaker_identities WHERE recording_id=? ORDER BY id',(rid,)) if row['id'] in ids]
    return {'model':raw['model'] if raw else None,'automatic_stale':stale,'labels_are_user_assertions':True,
            'speakers':names,'turns':turns,'overrides':overrides,'scope':scope.model_dump()}


def install(app,get_store,reserve):
    exports=threading.BoundedSemaphore(2)

    def version(db,rid):
        row=db.execute('SELECT revision FROM report_versions WHERE recording_id=?',(rid,)).fetchone()
        return row['revision'] if row else 0

    def advance(db,rid,expected):
        if version(db,rid)!=expected: raise HTTPException(409,'Reports changed; refresh before saving')
        db.execute('INSERT INTO report_versions VALUES(?,?) ON CONFLICT(recording_id) DO UPDATE SET revision=excluded.revision',(rid,expected+1))
        return expected+1

    def report_row(db,rid,report_id):
        row=db.execute('SELECT r.*,EXISTS(SELECT 1 FROM report_archive a WHERE a.report_id=r.id) AS archived FROM reports r WHERE recording_id=? AND id=?',(rid,report_id)).fetchone()
        if not row: raise HTTPException(404,'Report not found')
        return row

    def view(row,current):
        report=Report.model_validate_json(row['payload'])
        return ReportView(**{k:row[k] for k in ('id','title','status','revision','created','reviewed')},
                          generator=json.loads(row['generator']),stale=not reference_matches(report.reference,current),archived=bool(row['archived']) if 'archived' in row.keys() else False)

    @app.get('/api/recordings/{rid}/reports',response_model=ReportsList)
    def reports(rid: str,include_archived: bool=False):
        store=get_store()
        with store.db() as db:
            db.execute('BEGIN IMMEDIATE');current=current_reference(db,store,rid)
            return ReportsList(archived_count=db.execute('SELECT count(*) FROM report_archive a JOIN reports r ON r.id=a.report_id WHERE r.recording_id=?',(rid,)).fetchone()[0],revision=version(db,rid),reports=[view(row,current) for row in db.execute('SELECT r.*,EXISTS(SELECT 1 FROM report_archive a WHERE a.report_id=r.id) AS archived FROM reports r WHERE recording_id=? AND (? OR NOT EXISTS(SELECT 1 FROM report_archive a WHERE a.report_id=r.id)) ORDER BY created DESC,id',(rid,include_archived))])

    def persist(rid: str,body: SaveReport,generation_id=None):
        store=get_store();report=body.report
        with store.db() as db:
            db.execute('BEGIN IMMEDIATE');current=current_reference(db,store,rid)
            if generation_id:
                job=db.execute('SELECT * FROM report_generation WHERE id=? AND recording_id=?',(generation_id,rid)).fetchone()
                if not job or job['state']!='running': raise HTTPException(409,'Generation cancelled')
                if app.state.accounts.enabled:
                    permission=db.execute('SELECT m.role FROM memberships m JOIN users u ON u.id=m.user_id JOIN recordings r ON r.project_id=m.project_id WHERE r.id=? AND m.user_id=? AND u.disabled=0',(rid,job['requester'])).fetchone()
                    if not permission or permission['role'] not in {'editor','owner'}: raise HTTPException(403,'Generation access was removed')
            if report.reference!=current: raise HTTPException(409,'Recording evidence changed; regenerate or reconcile the draft')
            if db.execute('SELECT count(*) FROM reports WHERE recording_id=?',(rid,)).fetchone()[0]>=100:
                raise HTTPException(409,'Report history limit reached')
            segments={}
            for sid in {c.evidence_id for c in report.citations if c.kind=='transcript'}:
                row=db.execute('SELECT id,start,end,text FROM segments WHERE recording_id=? AND id=?',(rid,sid)).fetchone()
                if row: segments[sid]=Segment(**dict(row))
            selected={c.evidence_id for c in report.citations if c.kind=='image'}
            if len({i.id for i in body.inspected_images})!=len(body.inspected_images) or selected!={i.id for i in body.inspected_images}:
                raise HTTPException(422,'Acknowledge exactly the selected inspected images')
            images=[];snap_images=[]
            for acknowledgement in body.inspected_images:
                path,item=image_record(db,rid,acknowledgement.id);_,digest=checked_image(path)
                if digest!=acknowledgement.sha256: raise HTTPException(409,'Selected image changed; inspect it again')
                item['sha256']=digest
                images.append(InspectedImage(**{k:item[k] for k in ('id','source_sha256','timestamp','recipe_version','sha256')},inspected=True))
                snap_images.append(item)
            for video in report.video_references:
                asset=db.execute('SELECT * FROM visual_assets WHERE recording_id=? AND id=?',(rid,video.asset_id)).fetchone()
                if not asset or asset['source_sha256']!=current.source_sha256:
                    raise HTTPException(422,'Video reference is not registered for this source')
                manifest=json.loads(asset['manifest'])
                if (manifest['kind']!='clip' or manifest['sha256']!=video.sha256 or
                        video.interval.start!=manifest['actual_start'] or video.interval.end!=manifest['actual_end']):
                    raise HTTPException(422,'Video reference does not match registered clip')
            try: validate_citations(report,current,segments,images)
            except ContractError as exc: raise HTTPException(422,str(exc)) from None
            captured={'reference':current.model_dump(),'segments':[s.model_dump() for s in sorted(segments.values(),key=lambda s:(s.start,s.id))],
                      'project_id':db.execute('SELECT project_id FROM recordings WHERE id=?',(rid,)).fetchone()['project_id'],
                      'speakers':speaker_snapshot(db,rid,current,report.scope),'images':snap_images,
                      'coverage_is_importer_assertion':True,'inspection_is_importer_assertion':True}
            encoded=json.dumps(captured,ensure_ascii=False)
            if len(encoded.encode())>MAX_SNAPSHOT_BYTES: raise HTTPException(422,'Report snapshot exceeds four MiB; select a shorter scope')
            if shutil.disk_usage(store.root).free < reserve+MAX_BUNDLE_BYTES:
                raise HTTPException(507,'Insufficient reserved space for report storage/export')
            revision=advance(db,rid,body.expected_revision);report_id=identity();now=time.time()
            db.execute('INSERT INTO reports VALUES(?,?,?,?,?,?,?,?,?,NULL)',(report_id,rid,body.title,body.generator.model_dump_json(),report.model_dump_json(),encoded,'draft',revision,now))
            if generation_id:
                db.execute("UPDATE report_generation SET state='completed',stage='Draft ready',report_id=?,reserved=0,updated=? WHERE id=?",(report_id,now,generation_id))
            return ReportDetail(**view(report_row(db,rid,report_id),current).model_dump(),report=report,selected_image_count=len(images))

    @app.post('/api/recordings/{rid}/reports',response_model=ReportDetail,status_code=201)
    def save(rid: str,body: SaveReport):
        return persist(rid,body)

    @app.get('/api/recordings/{rid}/reports/{report_id}',response_model=ReportDetail)
    def detail(rid: str,report_id: str):
        store=get_store()
        with store.db() as db:
            db.execute('BEGIN IMMEDIATE');current=current_reference(db,store,rid);row=report_row(db,rid,report_id)
            return ReportDetail(**view(row,current).model_dump(),report=json.loads(row['payload']),selected_image_count=len(json.loads(row['snapshot'])['images']))

    @app.patch('/api/recordings/{rid}/reports/{report_id}',response_model=ReportView)
    def rename(rid: str, report_id: str, body: RenameReport):
        store=get_store()
        with store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            current=current_reference(db,store,rid)
            report_row(db,rid,report_id)
            revision=advance(db,rid,body.expected_revision)
            db.execute('UPDATE reports SET title=?,revision=? WHERE id=?',
                       (body.title.strip(),revision,report_id))
            return view(report_row(db,rid,report_id),current)

    @app.post('/api/recordings/{rid}/reports/{report_id}/archive',response_model=ReportView)
    def archive(rid: str,report_id: str,body: ArchiveReport):
        store=get_store()
        with store.db() as db:
            db.execute('BEGIN IMMEDIATE');current=current_reference(db,store,rid)
            report_row(db,rid,report_id)
            revision=advance(db,rid,body.expected_revision)
            if body.archived:db.execute('INSERT OR REPLACE INTO report_archive VALUES(?,?)',(report_id,time.time()))
            else:db.execute('DELETE FROM report_archive WHERE report_id=?',(report_id,))
            db.execute('UPDATE reports SET revision=? WHERE id=?',(revision,report_id))
            return view(report_row(db,rid,report_id),current)

    @app.delete('/api/recordings/{rid}/reports/{report_id}')
    def delete(rid: str,report_id: str,body: DeleteReport):
        store=get_store()
        with store.db() as db:
            db.execute('BEGIN IMMEDIATE');report_row(db,rid,report_id)
            for job in db.execute("SELECT request FROM report_generation WHERE recording_id=? AND state IN ('queued','running')",(rid,)):
                if json.loads(job['request']).get('previous_report_id')==report_id:
                    raise HTTPException(409,'Wait for the active refinement to finish or cancel it before deleting this report')
            revision=advance(db,rid,body.expected_revision)
            # Usage and generation receipts remain; deleted drafts cannot be reopened.
            db.execute('UPDATE report_generation SET report_id=NULL WHERE report_id=?',(report_id,))
            db.execute('DELETE FROM reports WHERE id=?',(report_id,))
            return {'deleted':True,'revision':revision}

    @app.post('/api/recordings/{rid}/reports/{report_id}/review',response_model=ReportView)
    def review(rid: str,report_id: str,body: ReviewReport):
        store=get_store()
        with store.db() as db:
            db.execute('BEGIN IMMEDIATE');current=current_reference(db,store,rid);row=report_row(db,rid,report_id)
            if view(row,current).stale: raise HTTPException(409,'Stale report cannot be marked reviewed; import a reconciled draft')
            if version(db,rid)!=body.expected_revision: raise HTTPException(409,'Reports changed; refresh before review')
            if row['status']=='reviewed': return view(row,current)
            revision=advance(db,rid,body.expected_revision)
            db.execute("UPDATE reports SET status='reviewed',revision=?,reviewed=? WHERE id=?",(revision,time.time(),report_id))
            return view(report_row(db,rid,report_id),current)

    @app.get('/api/recordings/{rid}/reports/{report_id}/clips/{asset_id}')
    def report_clip(rid: str,report_id: str,asset_id: str):
        with get_store().db() as db:
            report=Report.model_validate_json(report_row(db,rid,report_id)['payload'])
            video=next((v for v in report.video_references if v.asset_id==asset_id),None)
            asset=db.execute('SELECT * FROM visual_assets WHERE recording_id=? AND id=?',(rid,asset_id)).fetchone()
            if not video or not asset: raise HTTPException(404,'Report clip not found')
            if asset['source_sha256']!=report.reference.source_sha256: raise HTTPException(409,'Report clip source changed')
            path=Path(asset['path'])
            try:
                if path.stat().st_size>128*1024**2: raise HTTPException(422,'Report clip exceeds media limit')
                digest=hashlib.sha256()
                with path.open('rb') as stream:
                    for block in iter(lambda:stream.read(65536),b''): digest.update(block)
            except OSError: raise HTTPException(410,'Report clip is unavailable') from None
            if digest.hexdigest()!=video.sha256: raise HTTPException(409,'Report clip integrity changed')
            return FileResponse(path,media_type='video/mp4',headers={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'})

    @app.get('/api/recordings/{rid}/reports/{report_id}/export.html')
    @app.get('/api/recordings/{rid}/reports/{report_id}/export.zip')
    def export(rid: str,report_id: str,request: Request):
        store=get_store()
        if not exports.acquire(blocking=False): raise HTTPException(429,'Two report exports are already in progress')
        temporary=None;released=False;lock=threading.Lock()

        def cleanup():
            nonlocal released
            with lock:
                if not released:
                    released=True
                    try:
                        if temporary is not None: temporary.close()
                    finally: exports.release()
        try:
            if shutil.disk_usage(store.root).free<reserve+MAX_BUNDLE_BYTES:
                raise HTTPException(507,'Insufficient reserved space for export')
            with store.db() as db:
                db.execute('BEGIN IMMEDIATE');current=current_reference(db,store,rid);row=report_row(db,rid,report_id)
                metadata=view(row,current).model_dump();report=Report.model_validate_json(row['payload']);captured=json.loads(row['snapshot'])
                metadata['checked_reference']=current.model_dump();metadata['checked_at']=time.time()
                paths={}
                for image in captured['images']:
                    path,item=image_record(db,rid,image['id'])
                    if item['source_sha256']!=image['source_sha256'] or item['timestamp']!=image['timestamp']:
                        raise HTTPException(409,'Selected media metadata changed; import a new draft')
                    paths[image['id']]=path
            temporary=tempfile.TemporaryFile(dir=store.root/'results')
            from .report_bundle import write_bundle, standalone
            is_html=request.url.path.endswith('/export.html')
            if is_html:
                temporary.write(standalone(report,captured,metadata,paths))
            else:
                write_bundle(temporary,report,captured,metadata,paths)
            size=temporary.tell();temporary.seek(0)

            async def chunks():
                try:
                    while block:=await asyncio.to_thread(temporary.read,65536): yield block
                finally: cleanup()
            return ExportResponse(chunks(),cleanup=cleanup,media_type='text/html' if is_html else 'application/zip',headers={
                'Content-Disposition':f'attachment; filename="argon-report-{report_id}.{"html" if is_html else "zip"}"',
                'Content-Length':str(size),'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'})
        except BaseException:
            cleanup();raise

    return persist
