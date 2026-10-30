import asyncio
from contextlib import asynccontextmanager
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import shutil
import time

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.exceptions import RequestValidationError

from fastapi.staticfiles import StaticFiles

from .events import ProjectEvents
from .accounts import Accounts, AccountRequestBudget
from .account_routes import install as install_accounts

from .contracts import (ClaimRequest, Completion, Failure, Heartbeat, Project, ProjectCreate,
                        Recording, RecordingCreate, SearchHit, Transcript, Strict)
from .embeddings import Embeddings
from .corrections import install as install_corrections
from .store import Store, identity, lease_row


class WorkerCapabilities(Strict):
    diarization_model: str | None = None
    embedding_model: str | None = None


class Settings:
    def __init__(self, data=None, worker_token=None, max_upload=None, reserve=1024**3, embeddings=None, auth_mode=None, public_url=None, allow_totp=None, worker_tokens=None, worker_limit=1, chunk_seconds=180, overlap_seconds=2):
        value = os.environ.get('RECORDINGS_ALLOW_TOTP', 'false') if allow_totp is None else allow_totp
        if type(value) is not bool and (not isinstance(value, str) or value not in {'true', 'false'}):
            raise ValueError('RECORDINGS_ALLOW_TOTP must be true or false')
        self.allow_totp = value is True or value == 'true'
        self.worker_tokens = dict(worker_tokens or {})
        if type(worker_limit) is not int or not 1 <= worker_limit <= 8:
            raise ValueError('worker_limit must be 1..8')
        if type(chunk_seconds) is not int or not 30 <= chunk_seconds <= 600 or not .5 <= overlap_seconds <= 5:
            raise ValueError('Invalid bounded chunk configuration')
        self.worker_limit, self.chunk_seconds, self.overlap_seconds = worker_limit, chunk_seconds, overlap_seconds
        self.auth_mode = auth_mode or os.environ.get("RECORDINGS_AUTH_MODE", "local")
        self.public_url = public_url if public_url is not None else os.environ.get("RECORDINGS_PUBLIC_URL", "")
        from .remote_embeddings import RemoteEmbeddings
        profile = os.environ.get('RECORDINGS_EMBEDDING_PROFILE', 'large')
        self.embeddings = embeddings or (RemoteEmbeddings(profile) if os.environ.get('RECORDINGS_REMOTE_EMBEDDINGS') == 'true' else Embeddings(os.environ.get('RECORDINGS_EMBEDDING_DIR'), profile))
        self.data = Path(data or os.environ.get('RECORDINGS_DATA', 'data'))
        self.worker_token = worker_token if worker_token is not None else os.environ.get('RECORDINGS_WORKER_TOKEN', '')
        if max_upload is None:
            upload_mb = int(os.environ.get('RECORDINGS_MAX_UPLOAD_MB', '256'))
            if not 1 <= upload_mb <= (2**53 - 1) // 1024**2:
                raise ValueError('RECORDINGS_MAX_UPLOAD_MB must be a positive whole MiB value within the supported byte range')
            max_upload = upload_mb * 1024**2
        if type(max_upload) is not int or not 0 < max_upload <= 2**53 - 1:
            raise ValueError('max_upload must be a positive supported byte count')
        self.max_upload, self.reserve = max_upload, reserve


def create_app(settings=None):
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app):
        app.state.store = Store(settings.data)
        app.state.store.worker_limit = settings.worker_limit
        app.state.store.chunk_seconds = settings.chunk_seconds
        app.state.store.overlap_seconds = settings.overlap_seconds
        app.state.worker_seen = {}
        app.state.worker_capabilities = {}
        app.state.accounts = Accounts(app.state.store, settings.auth_mode, settings.public_url, settings.allow_totp)
        if app.state.accounts.enabled:
            app.state.accounts.oidc_config()
        app.state.upload_lock = asyncio.Lock()
        app.state.events = ProjectEvents(lambda pid: [Recording.model_validate(row).model_dump(mode='json') for row in recording_snapshot(pid)])
        app.state.store.on_commit = app.state.events.notify
        try:
            yield
        finally:
            app.state.store.on_commit = None
            await app.state.events.close()

    app = FastAPI(title='Argon Recordings', version='0.1.0', lifespan=lifespan)
    app.add_middleware(AccountRequestBudget)

    @app.exception_handler(HTTPException)
    async def account_error(request, exc):
        if request.scope['path'] == '/api/auth/oidc/callback':
            from fastapi.responses import RedirectResponse
            return RedirectResponse('/?auth_error=oidc', status_code=303)
        return JSONResponse({'detail': exc.detail}, status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, exc):
        # Non-finite JSON numbers and validator contexts are not serializable.
        # Return field diagnostics without reflecting credentials or raw input.
        return JSONResponse({'detail': [{k: e[k] for k in ('loc','msg','type')}
                                       for e in exc.errors()]}, status_code=422)


    @app.middleware('http')
    async def local_boundary(request, call_next):
        manager = app.state.accounts
        from urllib.parse import urlsplit
        hosts = {'localhost', '127.0.0.1', '::1', 'testserver'}
        if request.scope.get('worker_interface'):
            hosts.add('api')
        if manager.public_url:
            hosts.add(urlsplit(manager.public_url).hostname)
        raw_hosts = [value.decode('latin-1') for key, value in request.scope['headers'] if key.lower() == b'host']
        try:
            raw_host = raw_hosts[0] if len(raw_hosts) == 1 else ''
            authority = urlsplit('//' + raw_host)
            port = authority.port  # Validate port syntax as well as hostname.
            valid_host = (bool(raw_host) and not any(char.isspace() for char in raw_host)
                          and not any(char in raw_host for char in '/\\?#@')
                          and authority.hostname in hosts and not authority.username and not authority.password
                          and not authority.path and not authority.query and not authority.fragment)
        except ValueError:
            valid_host = False
        if not valid_host or not request.scope['path'].startswith('/'):
            return JSONResponse({'detail': 'Host is not allowed'}, status_code=403)
        callback = request.method == 'GET' and request.scope['path'] == '/api/auth/oidc/callback'
        origin = request.headers.get('origin')
        allowed_origins = {str(request.base_url).rstrip('/')}
        if manager.public_url:
            allowed_origins.add(manager.public_url)
        if origin and origin not in allowed_origins:
            return JSONResponse({'detail': 'Cross-origin access denied'}, status_code=403)
        if request.headers.get('sec-fetch-site') == 'cross-site' and not callback:
            return JSONResponse({'detail': 'Cross-site access denied'}, status_code=403)
        public = {'/api/health', '/api/auth/session', '/api/auth/login', '/api/auth/accept-invitation',
                  '/api/auth/reset-password', '/api/auth/oidc/start', '/api/auth/oidc/callback'}
        path = request.scope['path']
        if path.startswith('/api/agent/'):
            try:
                from .agent_grants import authenticate
                await asyncio.to_thread(authenticate, manager, request)
            except HTTPException as exc:
                return JSONResponse({'detail': exc.detail}, status_code=exc.status_code,
                                    headers={'Cache-Control':'no-store'})
        elif manager.enabled and path.startswith('/api/') and not path.startswith('/api/worker/') and path not in public:
            try:
                await asyncio.to_thread(manager.authorize, request)
            except HTTPException as exc:
                return JSONResponse({'detail': exc.detail}, status_code=exc.status_code)
        response = await call_next(request)
        if manager.enabled and path.startswith('/api/'):
            response.headers['Cache-Control'] = 'no-store'
            response.headers['X-Content-Type-Options'] = 'nosniff'
        return response

    def store():
        return app.state.store

    def worker_auth(request: Request, authorization: str | None = Header(default=None)):
        request.state.worker_id = None
        if settings.worker_tokens:
            if request.scope['path'] == '/api/worker/claim' or re.fullmatch(r'/api/worker/jobs/[^/]+/complete', request.scope['path']):
                raise HTTPException(404, 'Legacy worker protocol is disabled in isolated mode')
            worker_id = next((key for key, value in settings.worker_tokens.items()
                              if authorization and hmac.compare_digest(authorization, 'Bearer ' + value)), None)
            if worker_id is None:
                raise HTTPException(401, 'Worker credentials required')
            request.state.worker_id = worker_id
            app.state.worker_seen[worker_id] = time.monotonic()
            match = re.fullmatch(r'/api/worker/(jobs|corrections)/([^/]+)/.*', request.scope['path'])
            if match:
                table = 'jobs' if match[1] == 'jobs' else 'corrections'
                with store().db() as db:
                    row = db.execute('SELECT worker FROM ' + table + ' WHERE id=?', (match[2],)).fetchone()
                if not row or row['worker'] != worker_id:
                    raise HTTPException(404, 'Assigned job not found')
        else:
            if not settings.worker_token:
                raise HTTPException(503, 'Worker access is not configured')
            if not authorization or not hmac.compare_digest(authorization, 'Bearer ' + settings.worker_token):
                raise HTTPException(401, 'Worker credentials required')

    from .remote_embeddings import RemoteEmbeddings, InferenceResult
    if isinstance(settings.embeddings, RemoteEmbeddings):
        settings.embeddings.require_worker = True

    @app.post('/api/worker/inference/claim', dependencies=[Depends(worker_auth)])
    async def inference_claim(request: Request, body: WorkerCapabilities):
        if not isinstance(settings.embeddings, RemoteEmbeddings):
            raise HTTPException(404, 'Remote inference is not enabled')
        if body.embedding_model != settings.embeddings.model:
            raise HTTPException(409, 'Embedding profiles must match on coordinator and worker')
        settings.embeddings.last_seen = time.monotonic()
        return settings.embeddings.claim(request.state.worker_id or 'legacy')

    @app.post('/api/worker/inference/{tid}/{attempt}', dependencies=[Depends(worker_auth)])
    async def inference_complete(tid: str, attempt: str, body: InferenceResult, request: Request):
        if not isinstance(settings.embeddings, RemoteEmbeddings):
            raise HTTPException(404, 'Remote inference is not enabled')
        settings.embeddings.complete(tid, attempt, request.state.worker_id or 'legacy', body)
        return {'ok': True}

    @app.post('/api/worker/ready', dependencies=[Depends(worker_auth)])
    def worker_ready(request: Request, body: WorkerCapabilities | None = None):
        from .diarization_runtime import MODEL
        worker = getattr(request.state, 'worker_id', None) or 'legacy'
        app.state.worker_seen[worker] = time.monotonic()
        app.state.worker_capabilities[worker] = body.diarization_model if body and body.diarization_model == MODEL else None
        return {'ok': True}


    def recording_or_404(rid):
        row = store().recording(rid)
        if not row:
            raise HTTPException(404, 'Recording not found')
        return row

    def project_or_404(pid):
        if not store().project(pid):
            raise HTTPException(404, 'Project not found')

    def view(row):
        row = dict(row)
        with store().db() as db:
            job = db.execute('SELECT j.id,j.state,j.progress,j.stage,p.payload,p.updated FROM jobs j LEFT JOIN job_progress p ON p.job_id=j.id WHERE j.recording_id=?', (row['id'],)).fetchone()
            artifacts = db.execute('SELECT name,time,kind FROM artifacts WHERE recording_id=? ORDER BY time', (row['id'],)).fetchall()
            speaker_job = db.execute('SELECT id,state,progress,error FROM diarization_jobs WHERE recording_id=? ORDER BY created DESC LIMIT 1', (row['id'],)).fetchone()
            visual_job = db.execute('SELECT id,state,progress,error FROM visual_jobs WHERE recording_id=? ORDER BY created DESC LIMIT 1', (row['id'],)).fetchone()
            analysis_job = db.execute('SELECT id,state,progress,error FROM analysis_jobs WHERE recording_id=? ORDER BY created DESC LIMIT 1', (row['id'],)).fetchone()
            versions = db.execute('SELECT transcript,speakers,annotations,extraction FROM evidence_versions WHERE recording_id=?', (row['id'],)).fetchone()
            report_version = db.execute('SELECT revision FROM report_versions WHERE recording_id=?',(row['id'],)).fetchone()
        row['speaker_job'] = dict(speaker_job) if speaker_job else None
        row['visual_job'] = dict(visual_job) if visual_job else None
        row['analysis_job'] = dict(analysis_job) if analysis_job else None
        row['evidence_versions'] = dict(versions) if versions else None
        row['report_revision'] = report_version['revision'] if report_version else 0
        row['job'] = dict(job) if job else None
        if row['job']:
            payload = row['job'].pop('payload')
            row['job']['updated_at'] = row['job'].pop('updated')
            row['job']['transcription'] = json.loads(payload) if payload and job['state'] in {'running', 'prepared'} else None
        row['artifacts'] = [dict(a, url=f'/api/recordings/{row["id"]}/media/{a["name"]}') for a in artifacts]
        return row

    def require_lease(db, jid, lease):
        row = lease_row(db, jid, lease)
        if row is None:
            raise HTTPException(409, 'Lease expired or superseded')
        return row

    async def receive(request, path, limit):
        length = request.headers.get('content-length')
        if length and limit is not None and int(length) > limit:
            raise HTTPException(413, 'File exceeds size limit')
        total, digest = 0, hashlib.sha256()
        try:
            with path.open('xb') as output:
                async for block in request.stream():
                    total += len(block)
                    if limit is not None and total > limit:
                        raise HTTPException(413, 'File exceeds size limit')
                    if shutil.disk_usage(settings.data).free < settings.reserve + len(block):
                        raise HTTPException(507, 'Insufficient workspace capacity')
                    output.write(block)
                    digest.update(block)
                output.flush()
                os.fsync(output.fileno())
            if total == 0:
                raise HTTPException(400, 'Empty file')
            return total, digest.hexdigest()
        except BaseException:
            path.unlink(missing_ok=True)
            raise

    @app.get('/api/health')
    def health():
        with store().db() as db:
            needs_admin = app.state.accounts.enabled and not db.execute('SELECT 1 FROM users').fetchone()
        return {'status': 'ok', 'profile': settings.auth_mode, 'model': 'large-v3-turbo', 'search': 'hybrid' if settings.embeddings.configured else 'keyword',
                'embedding_profile': getattr(settings.embeddings, 'profile', None) if settings.embeddings.configured else 'off',
                'worker_configured': bool(settings.worker_token or settings.worker_tokens), 'worker_count': settings.worker_limit, 'workers_online': sum(time.monotonic() - seen < 130 for seen in app.state.worker_seen.values()), 'max_upload_bytes': settings.max_upload,
                'max_duration_seconds': None, 'accounts_setup_required': bool(needs_admin),
                'diarization_available': any(model and time.monotonic()-app.state.worker_seen.get(worker,0)<130 for worker,model in app.state.worker_capabilities.items())}

    @app.get('/api/projects', response_model=list[Project])
    def projects(request: Request):
        with store().db() as db:
            if app.state.accounts.enabled:
                return [dict(r) for r in db.execute('SELECT p.*,m.role,count(r.id) AS recording_count FROM projects p JOIN memberships m ON m.project_id=p.id LEFT JOIN recordings r ON r.project_id=p.id WHERE m.user_id=? GROUP BY p.id ORDER BY p.created DESC', (request.state.account['id'],))]
            return [dict(r) for r in db.execute('SELECT p.*,count(r.id) AS recording_count FROM projects p LEFT JOIN recordings r ON r.project_id=p.id GROUP BY p.id ORDER BY p.created DESC')]

    @app.post('/api/projects', response_model=Project, status_code=201)
    def create_project(body: ProjectCreate, request: Request):
        row = {'id': identity(), 'name': body.name.strip(), 'created': time.time()}
        with store().db() as db:
            db.execute('INSERT INTO projects(id,name,created) VALUES(:id,:name,:created)', row)
            if app.state.accounts.enabled:
                db.execute("INSERT INTO memberships VALUES(?,?,'owner')", (row['id'], request.state.account['id']))
        return dict(row, recording_count=0)

    @app.get('/api/projects/{pid}/recordings', response_model=list[Recording])
    def recordings(pid: str):
        return recording_snapshot(pid)

    def recording_snapshot(pid):
        project_or_404(pid)
        with store().db() as db:
            rows = db.execute('SELECT * FROM recordings WHERE project_id=? ORDER BY created DESC', (pid,)).fetchall()
        return [view(row) for row in rows]

    @app.get('/api/projects/{pid}/events', response_class=StreamingResponse,
             responses={200: {'content': {'text/event-stream': {'schema': {'type': 'string'}}},
                              'description': 'SSE recordings snapshots; initial state and committed changes. Reconnect for current state.'}})
    def project_events(pid: str, request: Request):
        project_or_404(pid)
        guard = (lambda: app.state.accounts.allowed(request.cookies.get('recordings_session', ''), pid)) if app.state.accounts.enabled else None
        return StreamingResponse(app.state.events.stream(pid, guard), media_type='text/event-stream',
                                 headers={'Cache-Control': 'no-cache, no-transform', 'X-Accel-Buffering': 'no'})

    @app.post('/api/projects/{pid}/recordings', response_model=Recording, status_code=201)
    def create_recording(pid: str, body: RecordingCreate):
        project_or_404(pid)
        rid = identity()
        with store().db() as db:
            db.execute('INSERT INTO recordings(id,project_id,title,filename,language,status,created) VALUES(?,?,?,?,?,?,?)',
                       (rid, pid, body.title.strip(), body.filename, body.language, 'awaiting_upload', time.time()))
            # Priority is captured before upload; the job becomes claimable only after source commit.
            db.execute('INSERT INTO jobs(id,recording_id,state,priority,created) VALUES(?,?,?,?,?)',
                       (identity(), rid, 'awaiting_upload', int(body.priority == 'urgent'), time.time()))
        return view(recording_or_404(rid))

    @app.get('/api/recordings/{rid}', response_model=Recording)
    def recording(rid: str):
        return view(recording_or_404(rid))

    @app.put('/api/recordings/{rid}/source', response_model=Recording)
    async def upload(rid: str, request: Request):
        recording_or_404(rid)
        async with app.state.upload_lock:
            with store().db() as db:
                changed = db.execute("UPDATE recordings SET status='uploading' WHERE id=? AND status='awaiting_upload'", (rid,)).rowcount
                if not changed:
                    raise HTTPException(409, 'Source already uploaded or processing')
            temporary = settings.data / 'staging' / (identity() + '.part')
            try:
                size, sha = await receive(request, temporary, settings.max_upload)
                target = settings.data / 'sources' / rid
                temporary.replace(target)
                with store().db() as db:
                    db.execute("UPDATE recordings SET status='queued',bytes=?,sha256=? WHERE id=?", (size, sha, rid))
                    db.execute("UPDATE jobs SET state='queued' WHERE recording_id=?", (rid,))
            except BaseException:
                temporary.unlink(missing_ok=True)
                with store().db() as db:
                    db.execute("UPDATE recordings SET status='awaiting_upload' WHERE id=? AND status='uploading'", (rid,))
                raise
        return view(recording_or_404(rid))

    @app.post('/api/recordings/{rid}/retry', response_model=Recording)
    def retry(rid: str, background_tasks: BackgroundTasks):
        recording_or_404(rid)
        with store().db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT r.status,j.state FROM recordings r JOIN jobs j ON j.recording_id=r.id WHERE r.id=?", (rid,)).fetchone()
            if row['status'] != 'failed' or row['state'] != 'failed':
                raise HTTPException(409, 'Only a failed recording can be retried')
            if not (settings.data / 'sources' / rid).is_file():
                raise HTTPException(409, 'Retained source is unavailable; upload a new recording')
            job_id = db.execute('SELECT id FROM jobs WHERE recording_id=?', (rid,)).fetchone()[0]
            if db.execute('SELECT 1 FROM preparations WHERE job_id=?', (job_id,)).fetchone():
                try:
                    published = app.state.finish_prepared(db, job_id)
                except ValueError as exc:
                    raise HTTPException(409, 'Saved chunk results failed validation; original results are preserved') from exc
                if published:
                    if settings.embeddings.configured:
                        background_tasks.add_task(settings.embeddings.refresh, store(), published)
                else:
                    db.execute("UPDATE chunks SET state='queued',attempts=0,lease_id=NULL,lease_until=NULL,worker=NULL WHERE job_id=? AND state!='completed'", (job_id,))
                    db.execute("UPDATE jobs SET state='prepared',stage='Resuming unfinished chunks',result_sha=NULL WHERE id=?", (job_id,))
                    db.execute("UPDATE recordings SET status='running',error=NULL WHERE id=?", (rid,))
            else:
                db.execute("UPDATE recordings SET status='queued',error=NULL WHERE id=?", (rid,))
                db.execute("UPDATE jobs SET state='queued',attempts=0,lease_id=NULL,lease_until=NULL,worker=NULL,progress=0,stage='Waiting for worker',result_sha=NULL,created=? WHERE recording_id=?", (time.time(), rid))
        return view(recording_or_404(rid))

    @app.get('/api/recordings/{rid}/transcript', response_model=Transcript)
    def transcript(rid: str):
        recording_or_404(rid)
        with store().db() as db:
            rows = db.execute('SELECT id,start,end,text,words FROM segments WHERE recording_id=? ORDER BY start', (rid,)).fetchall()
        return {'recording_id': rid, 'segments': [dict(r, words=json.loads(r['words'])) for r in rows]}

    @app.get('/api/recordings/{rid}/media/{name}')
    def media(rid: str, name: str):
        recording_or_404(rid)
        with store().db() as db:
            row = db.execute('SELECT path,kind FROM artifacts WHERE recording_id=? AND name=?', (rid, name)).fetchone()
        if not row:
            raise HTTPException(404, 'Artifact not available')
        return FileResponse(row['path'], media_type='audio/wav' if row['kind'] == 'audio' else 'image/jpeg', headers={'Cache-Control': 'private, max-age=3600', 'X-Content-Type-Options': 'nosniff'})

    def perform_search(project_id: str, q: str, mode: str = 'hybrid'):
        project_or_404(project_id)
        if mode not in {'hybrid', 'keyword', 'semantic'}:
            raise HTTPException(422, 'Unknown search mode')
        if mode == 'semantic' and not settings.embeddings.configured:
            raise HTTPException(503, 'Embedding model is not configured')
        terms = re.findall(r'\w+', q[:300], re.UNICODE)[:20]
        if not terms:
            return []
        match = ' AND '.join('"' + t + '"' for t in terms)
        with store().db() as db:
            lexical = [dict(r) for r in db.execute('SELECT r.id AS recording_id,r.title,s.start,s.end,s.text FROM segment_search f JOIN segments s ON s.id=f.segment_id JOIN recordings r ON r.id=s.recording_id WHERE segment_search MATCH ? AND r.project_id=? ORDER BY bm25(segment_search) LIMIT 30', (match, project_id))]
        if mode == 'keyword' or not settings.embeddings.configured:
            return lexical
        try:
            semantic = settings.embeddings.search(store(), project_id, q[:300])
        except Exception as exc:
            import logging
            logging.getLogger(__name__).exception('Semantic search failed')
            # Explicit failure keeps a broken model visible; users can select keyword search.
            raise HTTPException(503, 'Semantic index unavailable; use keyword search or check model configuration') from exc
        if mode == 'semantic':
            return semantic
        # Reciprocal-rank fusion; timestamps provide the stable jump target.
        ranks, values = {}, {}
        for ranking in [lexical, semantic]:
            for i, hit in enumerate(ranking):
                key = (hit['recording_id'], hit['start'])
                ranks[key] = ranks.get(key, 0) + 1 / (60 + i + 1)
                values.setdefault(key, hit)
        return [values[k] for k in sorted(ranks, key=ranks.get, reverse=True)[:30]]

    install_corrections(app, store, recording_or_404, worker_auth, settings.embeddings)

    import threading
    from starlette.concurrency import run_in_threadpool
    search_slots = threading.BoundedSemaphore(4)

    def bounded_agent_search(project_id, q, mode):
        if not search_slots.acquire(blocking=False):
            raise HTTPException(429, 'Search is busy; try again shortly')
        try:
            return perform_search(project_id, q, mode)
        finally:
            search_slots.release()

    @app.get('/api/search', response_model=list[SearchHit])
    async def search(project_id: str, q: str, mode: str = 'hybrid'):
        # Fail before consuming a request thread while another query waits for inference.
        if not search_slots.acquire(blocking=False):
            raise HTTPException(429, 'Search is busy; try again shortly')
        try:
            return await run_in_threadpool(perform_search, project_id, q, mode)
        finally:
            search_slots.release()

    @app.post('/api/worker/claim', dependencies=[Depends(worker_auth)])
    def claim(body: ClaimRequest, request: Request):
        return store().claim(request.state.worker_id or body.worker)

    @app.post('/api/worker/jobs/{jid}/heartbeat', dependencies=[Depends(worker_auth)])
    def heartbeat(jid: str, body: Heartbeat, x_lease_id: str = Header()):
        with store().db() as db:
            db.execute('BEGIN IMMEDIATE')
            require_lease(db, jid, x_lease_id)
            db.execute('UPDATE jobs SET lease_until=?,progress=?,stage=? WHERE id=?', (time.time() + 120, body.progress, body.stage, jid))
            if body.transcription is not None:
                db.execute('INSERT INTO job_progress VALUES(?,?,?) ON CONFLICT(job_id) DO UPDATE SET payload=excluded.payload,updated=excluded.updated',
                           (jid, body.transcription.model_dump_json(), time.time()))
            else:
                db.execute('DELETE FROM job_progress WHERE job_id=?', (jid,))
        return {'lease_seconds': 120}

    @app.get('/api/worker/jobs/{jid}/source', dependencies=[Depends(worker_auth)])
    def worker_source(jid: str, x_lease_id: str = Header()):
        with store().db() as db:
            row = require_lease(db, jid, x_lease_id)
        return FileResponse(settings.data / 'sources' / row['recording_id'], media_type='application/octet-stream')

    @app.put('/api/worker/jobs/{jid}/artifacts/{name}', dependencies=[Depends(worker_auth)])
    async def artifact_upload(jid: str, name: str, request: Request, x_lease_id: str = Header()):
        if not re.fullmatch(r'audio\.wav|frame-[0-9]{4}\.jpg', name):
            raise HTTPException(400, 'Invalid artifact name')
        with store().db() as db:
            require_lease(db, jid, x_lease_id)
        directory = settings.data / 'results' / jid / x_lease_id
        directory.mkdir(parents=True, exist_ok=True)
        temporary = directory / (identity() + '.part')
        await receive(request, temporary, None if name == 'audio.wav' else 2 * 1024**2)
        try:
            with store().db() as db:
                db.execute('BEGIN IMMEDIATE')
                require_lease(db, jid, x_lease_id)
                temporary.replace(directory / name)
        finally:
            temporary.unlink(missing_ok=True)
        return {'name': name}

    @app.post('/api/worker/jobs/{jid}/complete', response_model=Recording, dependencies=[Depends(worker_auth)])
    def complete(jid: str, body: Completion, background_tasks: BackgroundTasks, x_lease_id: str = Header()):
        payload_sha = hashlib.sha256(body.model_dump_json().encode()).hexdigest()
        with store().db() as db:
            db.execute('BEGIN IMMEDIATE')
            previous = db.execute('SELECT * FROM jobs WHERE id=?', (jid,)).fetchone()
            if previous and previous['state'] == 'completed' and previous['lease_id'] == x_lease_id and previous['result_sha'] == payload_sha:
                rid = previous['recording_id']
            else:
                job = require_lease(db, jid, x_lease_id)
                rid = job['recording_id']
                directory = settings.data / 'results' / jid / x_lease_id
                for artifact in body.artifacts:
                    path = directory / artifact.name
                    if not path.is_file():
                        raise HTTPException(409, 'Artifact upload is incomplete')
                    with path.open('rb') as stream:
                        signature = stream.read(12)
                    if artifact.kind == 'audio' and not (signature[:4] == b'RIFF' and signature[8:12] == b'WAVE'):
                        raise HTTPException(422, 'Invalid WAV artifact')
                    if artifact.kind == 'frame' and signature[:3] != b'\xff\xd8\xff':
                        raise HTTPException(422, 'Invalid JPEG artifact')
                for segment in body.segments:
                    sid = identity()
                    db.execute('INSERT INTO segments VALUES(?,?,?,?,?,?)', (sid, rid, segment.start, segment.end, segment.text, json.dumps([w.model_dump() for w in segment.words])))
                    db.execute('INSERT INTO segment_search(text,segment_id,recording_id) VALUES(?,?,?)', (segment.text, sid, rid))
                for a in body.artifacts:
                    db.execute('INSERT INTO artifacts VALUES(?,?,?,?,?)', (rid, a.name, a.time, a.kind, str(directory / a.name)))
                db.execute("UPDATE recordings SET status='ready',duration=? WHERE id=?", (body.duration, rid))
                db.execute("UPDATE jobs SET state='completed',progress=1,stage='Ready',result_sha=? WHERE id=?", (payload_sha, jid))
        if settings.embeddings.configured:
            background_tasks.add_task(settings.embeddings.refresh, store(), rid)
        return view(recording_or_404(rid))

    @app.post('/api/worker/jobs/{jid}/fail', dependencies=[Depends(worker_auth)])
    def fail(jid: str, body: Failure, x_lease_id: str = Header()):
        with store().db() as db:
            db.execute('BEGIN IMMEDIATE')
            job = require_lease(db, jid, x_lease_id)
            db.execute("UPDATE jobs SET state='failed',stage='Failed' WHERE id=?", (jid,))
            db.execute("UPDATE recordings SET status='failed',error=? WHERE id=?", (body.message, job['recording_id']))
        return {'status': 'failed'}

    from .chunk_tasks import install as install_chunks
    install_chunks(app, store, worker_auth, receive, settings.embeddings)

    install_accounts(app, lambda: app.state.accounts)

    from .evidence import install as install_evidence
    install_evidence(app, store)
    from .diarization import install as install_diarization
    speaker_readers = install_diarization(app, store, worker_auth)
    from .speaker_review import install as install_speaker_review
    install_speaker_review(app, store)
    from .visual_jobs import install as install_visual_jobs
    visual_actions = install_visual_jobs(app, store, worker_auth, receive, settings.reserve)
    from .analysis_jobs import install as install_analysis_jobs
    install_analysis_jobs(app, store, worker_auth, settings.reserve)
    from .reports import install as install_reports
    install_reports(app, store, settings.reserve)
    from .agent_grants import install as install_agent_grants
    install_agent_grants(app, lambda: app.state.accounts)
    from .agent_api import install as install_agent_api
    install_agent_api(app, lambda: app.state.accounts, view, bounded_agent_search, speaker_readers, visual_actions)

    static = os.environ.get('RECORDINGS_STATIC')
    if static:
        app.mount('/', StaticFiles(directory=static, html=True), name='web')
    return app
