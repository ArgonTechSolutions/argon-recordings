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

from fastapi.staticfiles import StaticFiles

from .events import ProjectEvents
from .accounts import Accounts, AccountRequestBudget
from .account_routes import install as install_accounts

from .contracts import (ClaimRequest, Completion, Failure, Heartbeat, Project, ProjectCreate,
                        Recording, RecordingCreate, SearchHit, Transcript)
from .embeddings import Embeddings
from .corrections import install as install_corrections
from .store import Store, identity, lease_row


class Settings:
    def __init__(self, data=None, worker_token=None, max_upload=None, reserve=1024**3, embeddings=None, auth_mode=None, public_url=None, allow_totp=None):
        value = os.environ.get('RECORDINGS_ALLOW_TOTP', 'false') if allow_totp is None else allow_totp
        if type(value) is not bool and (not isinstance(value, str) or value not in {'true', 'false'}):
            raise ValueError('RECORDINGS_ALLOW_TOTP must be true or false')
        self.allow_totp = value is True or value == 'true'
        self.auth_mode = auth_mode or os.environ.get("RECORDINGS_AUTH_MODE", "local")
        self.public_url = public_url if public_url is not None else os.environ.get("RECORDINGS_PUBLIC_URL", "")
        self.embeddings = embeddings or Embeddings(os.environ.get('RECORDINGS_EMBEDDING_DIR'), os.environ.get('RECORDINGS_EMBEDDING_PROFILE', 'large'))
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


    @app.middleware('http')
    async def local_boundary(request, call_next):
        manager = app.state.accounts
        from urllib.parse import urlsplit
        hosts = {'localhost', '127.0.0.1', '::1', 'testserver'}
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
        if manager.enabled and path.startswith('/api/') and not path.startswith('/api/worker/') and path not in public:
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

    def worker_auth(authorization: str | None = Header(default=None)):
        if not settings.worker_token:
            raise HTTPException(503, 'Worker access is not configured')
        if not authorization or not hmac.compare_digest(authorization, 'Bearer ' + settings.worker_token):
            raise HTTPException(401, 'Worker credentials required')

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
        row['job'] = dict(job) if job else None
        if row['job']:
            payload = row['job'].pop('payload')
            row['job']['updated_at'] = row['job'].pop('updated')
            row['job']['transcription'] = json.loads(payload) if payload and job['state'] == 'running' else None
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
                'worker_configured': bool(settings.worker_token), 'max_upload_bytes': settings.max_upload,
                'max_duration_seconds': None, 'accounts_setup_required': bool(needs_admin)}

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
    def retry(rid: str):
        recording_or_404(rid)
        with store().db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT r.status,j.state FROM recordings r JOIN jobs j ON j.recording_id=r.id WHERE r.id=?", (rid,)).fetchone()
            if row['status'] != 'failed' or row['state'] != 'failed':
                raise HTTPException(409, 'Only a failed recording can be retried')
            if not (settings.data / 'sources' / rid).is_file():
                raise HTTPException(409, 'Retained source is unavailable; upload a new recording')
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

    @app.get('/api/search', response_model=list[SearchHit])
    def search(project_id: str, q: str, mode: str = 'hybrid'):
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

    @app.post('/api/worker/claim', dependencies=[Depends(worker_auth)])
    def claim(body: ClaimRequest):
        return store().claim(body.worker)

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

    install_accounts(app, lambda: app.state.accounts)

    static = os.environ.get('RECORDINGS_STATIC')
    if static:
        app.mount('/', StaticFiles(directory=static, html=True), name='web')
    return app
