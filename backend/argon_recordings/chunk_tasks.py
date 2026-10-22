"""Durable preparation/child leases. No transcript is public before reconciliation."""
import hashlib
import json
import time
import wave

from fastapi import BackgroundTasks, Depends, Header, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import Field

from .contracts import ArtifactInput, Completion, Failure, Heartbeat, Segment, Strict
from .store import identity, lease_row
from .chunk_audio import reconcile, validate_words


class ChunkInput(Strict):
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    core_start: float = Field(ge=0)
    core_end: float = Field(gt=0)
    boundary: bool = False
    sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    bytes: int = Field(gt=44, le=64*1024**2)


class Prepared(Strict):
    duration: float = Field(gt=0, le=(4*1024**3-1024**2)/32000)
    artifacts: list[ArtifactInput] = Field(max_length=257)
    chunks: list[ChunkInput] = Field(min_length=1, max_length=4096)


class ChunkResult(Strict):
    segments: list[Segment] = Field(max_length=4000)


def install(app, store, worker_auth, receive, embeddings):
    def owner(request, row):
        worker = getattr(request.state, 'worker_id', None)
        if worker is not None and row['worker'] != worker:
            raise HTTPException(403, 'Task belongs to another worker')

    def parent(db, jid, token, request):
        row = lease_row(db, jid, token)
        if row is None:
            raise HTTPException(409, 'Preparation lease expired or superseded')
        owner(request, row)
        return row

    def child(db, cid, token, request, completed=False):
        row = db.execute('SELECT c.*,r.language FROM chunks c JOIN jobs j ON j.id=c.job_id JOIN recordings r ON r.id=j.recording_id WHERE c.id=?', (cid,)).fetchone()
        if row is None or row['lease_id'] != token:
            raise HTTPException(409, 'Chunk lease expired or superseded')
        owner(request, row)
        if completed and row['state'] == 'completed':
            return row
        if row['state'] != 'running' or row['lease_until'] <= time.time():
            raise HTTPException(409, 'Chunk lease expired or superseded')
        return row

    def aggregate(db, jid):
        row = db.execute('SELECT p.duration,t.started FROM preparations p JOIN pipeline_progress t ON t.job_id=p.job_id WHERE p.job_id=?', (jid,)).fetchone()
        if row is None:
            return
        completed = db.execute("SELECT count(*) AS count,coalesce(sum(core_end-core_start),0) AS seconds FROM chunks WHERE job_id=? AND boundary=0 AND state='completed'", (jid,)).fetchone()
        now = time.time()
        payload = dict(audio_processed_seconds=min(row['duration'],completed['seconds']),audio_total_seconds=row['duration'],
                       passages=completed['count'],elapsed_seconds=max(0,now-row['started']),eta_seconds=None)
        db.execute('INSERT INTO job_progress VALUES(?,?,?) ON CONFLICT(job_id) DO UPDATE SET payload=excluded.payload,updated=excluded.updated',
                   (jid,json.dumps(payload),now))

    @app.post('/api/worker/tasks/claim', dependencies=[Depends(worker_auth)])
    def claim(request: Request):
        worker = getattr(request.state, 'worker_id', None)
        with store().db() as db:
            db.execute('BEGIN IMMEDIATE')
            if not store().capacity_available(db, worker):
                return None
            expired = db.execute("SELECT DISTINCT job_id FROM chunks WHERE state='running' AND lease_until<=? AND attempts>=3", (time.time(),)).fetchall()
            for row in expired:
                db.execute("UPDATE jobs SET state='failed',stage='Chunk retries exhausted' WHERE id=?", (row['job_id'],))
                db.execute("UPDATE recordings SET status='failed',error='Chunk lease retries exhausted' WHERE id=(SELECT recording_id FROM jobs WHERE id=?)", (row['job_id'],))
            row = db.execute("SELECT c.*,r.language FROM chunks c JOIN jobs j ON j.id=c.job_id JOIN recordings r ON r.id=j.recording_id WHERE j.state='prepared' AND (c.state='queued' OR (c.state='running' AND c.lease_until<=? AND c.attempts<3)) ORDER BY j.priority*300-j.created DESC,c.ordinal LIMIT 1", (time.time(),)).fetchone()
            if row:
                token = identity()
                db.execute("UPDATE chunks SET state='running',worker=?,lease_id=?,lease_until=?,attempts=attempts+1 WHERE id=?", (worker, token, time.time()+120, row['id']))
                return {**dict(row), 'kind': 'chunk', 'lease_id': token, 'lease_seconds': 120, 'path': None, 'result': None}
        # Store.claim repeats the capacity check transactionally.
        job = store().claim(worker)
        if job:
            return {**job, 'kind': 'prepare', 'chunk_seconds': getattr(store(), 'chunk_seconds', 180), 'overlap_seconds': getattr(store(), 'overlap_seconds', 2)}
        return None

    @app.put('/api/worker/jobs/{jid}/chunks/{index}', dependencies=[Depends(worker_auth)])
    async def upload(jid: str, index: int, request: Request, x_lease_id: str = Header()):
        if not 0 <= index < 4096:
            raise HTTPException(422, 'Invalid chunk index')
        with store().db() as db:
            parent(db, jid, x_lease_id, request)
        directory = store().root / 'results' / jid / x_lease_id
        directory.mkdir(parents=True, exist_ok=True)
        temporary = directory / (identity()+'.part')
        try:
            await receive(request, temporary, 64*1024**2)
            with store().db() as db:
                db.execute('BEGIN IMMEDIATE')
                parent(db, jid, x_lease_id, request)
                temporary.replace(directory / f'chunk-{index:04d}.wav')
        finally:
            temporary.unlink(missing_ok=True)
        return {'index': index}

    @app.post('/api/worker/jobs/{jid}/prepare', dependencies=[Depends(worker_auth)])
    def prepare(jid: str, body: Prepared, request: Request, x_lease_id: str = Header()):
        digest = hashlib.sha256(body.model_dump_json().encode()).hexdigest()
        with store().db() as db:
            db.execute('BEGIN IMMEDIATE')
            previous = db.execute('SELECT * FROM preparations WHERE job_id=?', (jid,)).fetchone()
            job = db.execute('SELECT * FROM jobs WHERE id=?', (jid,)).fetchone()
            if previous and previous['lease_id'] == x_lease_id and previous['result_sha'] == digest:
                owner(request, job)
                return {'status': 'prepared'}
            parent(db, jid, x_lease_id, request)
            directory = store().root / 'results' / jid / x_lease_id
        try:
            Completion(duration=body.duration, segments=[], artifacts=body.artifacts)
        except ValueError as exc:
            raise HTTPException(422, 'Invalid preparation artifacts') from exc
        files, records = {}, []
        def stamp(path):
            value = path.stat()
            return value.st_ino, value.st_size, value.st_mtime_ns
        for artifact in body.artifacts:
            path = directory / artifact.name
            if not path.is_file():
                raise HTTPException(409, 'Artifact upload incomplete')
            files[path] = stamp(path)
            with path.open('rb') as artifact_stream:
                signature = artifact_stream.read(12)
            if artifact.kind == 'audio' and not (signature[:4] == b'RIFF' and signature[8:12] == b'WAVE') or artifact.kind == 'frame' and signature[:3] != b'\xff\xd8\xff':
                raise HTTPException(422, 'Invalid artifact')
        try:
            with wave.open(str(directory/'audio.wav')) as audio:
                if (audio.getnchannels(),audio.getsampwidth(),audio.getframerate()) != (1,2,16000) or abs(audio.getnframes()/16000-body.duration)>.001:
                    raise ValueError('Retained audio format or duration mismatch')
                audio.setpos(audio.getnframes()-1)
                if len(audio.readframes(1)) != 2:
                    raise ValueError('Retained audio is truncated')
        except (wave.Error, EOFError, ValueError) as exc:
            raise HTTPException(422, 'Invalid retained audio format or duration') from exc
        cores = [c for c in body.chunks if not c.boundary]
        if not cores or abs(cores[0].core_start) > .001 or abs(cores[-1].core_end-body.duration) > .001 or any(abs(a.core_end-b.core_start)>.001 for a,b in zip(cores,cores[1:])):
            raise HTTPException(422, 'Chunk cores must cover the complete recording')
        overlap = getattr(store(), 'overlap_seconds', 2)
        if any(c.core_end-c.core_start > getattr(store(), 'chunk_seconds', 180)+5.001 for c in cores):
            raise HTTPException(422, 'Chunk core exceeds configured duration')
        if any(c.core_end-c.core_start < getattr(store(), 'chunk_seconds', 180)-5.001 for c in cores[:-1]):
            raise HTTPException(422, 'Chunk cuts exceed configured pause window')
        cuts = [c.core_end for c in cores[:-1]]
        boundary = [c for c in body.chunks if c.boundary]
        if len(boundary) != len(cuts) or any(abs(c.core_start-max(0,cut-overlap))>.001 or abs(c.core_end-min(body.duration,cut+overlap))>.001 for c,cut in zip(boundary,cuts)):
            raise HTTPException(422, 'Every cut requires a context retry')
        for index, chunk in enumerate(body.chunks):
            if not 0 <= chunk.start <= chunk.core_start < chunk.core_end <= chunk.end <= body.duration or chunk.end-chunk.start > 1900:
                raise HTTPException(422, 'Invalid chunk bounds')
            context = max(12, overlap*3) if chunk.boundary else overlap
            if abs(chunk.start-max(0,chunk.core_start-(context-overlap if chunk.boundary else context)))>.001 or abs(chunk.end-min(body.duration,chunk.core_end+(context-overlap if chunk.boundary else context)))>.001:
                raise HTTPException(422, 'Chunk context does not match configured overlap')
            path = directory / f'chunk-{index:04d}.wav'
            if not path.is_file() or path.stat().st_size != chunk.bytes:
                raise HTTPException(409, 'Chunk upload incomplete')
            files[path] = stamp(path)
            hasher = hashlib.sha256()
            with path.open('rb') as stream:
                for block in iter(lambda: stream.read(1024**2), b''):
                    hasher.update(block)
            if hasher.hexdigest() != chunk.sha256:
                raise HTTPException(422, 'Chunk integrity mismatch')
            try:
                with wave.open(str(path)) as wav:
                    valid = (wav.getnchannels(),wav.getsampwidth(),wav.getframerate()) == (1,2,16000) and abs(wav.getnframes()/16000-(chunk.end-chunk.start)) < .001
            except (wave.Error, EOFError):
                valid = False
            if not valid:
                raise HTTPException(422, 'Chunk WAV format or duration mismatch')
            with wave.open(str(directory/'audio.wav')) as original, wave.open(str(path)) as cropped:
                original.setpos(round(chunk.start*16000))
                remaining = cropped.getnframes()
                while remaining:
                    count = min(remaining,16000)
                    original_data, chunk_data = original.readframes(count), cropped.readframes(count)
                    if len(original_data) != count*2 or original_data != chunk_data:
                        raise HTTPException(422, 'Chunk does not match retained source audio')
                    remaining -= count
            records.append((identity(),jid,index,int(chunk.boundary),chunk.start,chunk.end,chunk.core_start,chunk.core_end,str(path),chunk.bytes,chunk.sha256))
        # Hashing/copy verification must not block unrelated worker heartbeats.
        # Uploads replace files under this same transaction fence, so inode/size/
        # mtime seals close the validation-to-publication race.
        with store().db() as db:
            db.execute('BEGIN IMMEDIATE')
            previous = db.execute('SELECT * FROM preparations WHERE job_id=?', (jid,)).fetchone()
            if previous and previous['lease_id'] == x_lease_id and previous['result_sha'] == digest:
                owner(request, db.execute('SELECT * FROM jobs WHERE id=?', (jid,)).fetchone())
                return {'status':'prepared'}
            job = parent(db, jid, x_lease_id, request)
            if any(not path.is_file() or stamp(path) != expected for path,expected in files.items()):
                raise HTTPException(409, 'Prepared uploads changed during validation')
            db.executemany('INSERT INTO chunks(id,job_id,ordinal,boundary,start,end,core_start,core_end,path,bytes,sha256) VALUES(?,?,?,?,?,?,?,?,?,?,?)', records)
            db.execute('INSERT INTO preparations VALUES(?,?,?,?,?)', (jid,x_lease_id,body.duration,json.dumps([a.model_dump() for a in body.artifacts]),digest))
            db.execute("UPDATE jobs SET state='prepared',progress=.1,stage='Waiting for chunk workers',lease_until=NULL WHERE id=?", (jid,))
            db.execute('UPDATE recordings SET duration=? WHERE id=?', (body.duration,job['recording_id']))
            db.execute('INSERT INTO pipeline_progress VALUES(?,?)', (jid,time.time()))
            aggregate(db,jid)
        return {'status': 'prepared'}

    @app.get('/api/worker/chunks/{cid}/audio', dependencies=[Depends(worker_auth)])
    def audio(cid: str, request: Request, x_lease_id: str = Header()):
        with store().db() as db:
            row = child(db,cid,x_lease_id,request)
        return FileResponse(row['path'], media_type='audio/wav')

    @app.post('/api/worker/chunks/{cid}/heartbeat', dependencies=[Depends(worker_auth)])
    def heartbeat(cid: str, body: Heartbeat, request: Request, x_lease_id: str = Header()):
        with store().db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = child(db,cid,x_lease_id,request)
            db.execute('UPDATE chunks SET lease_until=? WHERE id=?', (time.time()+120,cid))
            aggregate(db,row['job_id'])
        return {'lease_seconds': 120}

    @app.post('/api/worker/chunks/{cid}/fail', dependencies=[Depends(worker_auth)])
    def fail(cid: str, body: Failure, request: Request, x_lease_id: str = Header()):
        with store().db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = child(db,cid,x_lease_id,request)
            db.execute("UPDATE chunks SET state=?,error=?,lease_until=NULL WHERE id=?", ('queued' if row['attempts']<3 else 'failed',body.message,cid))
            if row['attempts']>=3:
                db.execute("UPDATE jobs SET state='failed',stage='Chunk failed' WHERE id=?", (row['job_id'],))
                db.execute("UPDATE recordings SET status='failed',error=? WHERE id=(SELECT recording_id FROM jobs WHERE id=?)", (body.message,row['job_id']))
        return {'status': 'failed'}

    @app.post('/api/worker/chunks/{cid}/complete', dependencies=[Depends(worker_auth)])
    def complete(cid: str, body: ChunkResult, request: Request, background_tasks: BackgroundTasks, x_lease_id: str = Header()):
        payload = body.model_dump_json()
        digest = hashlib.sha256(payload.encode()).hexdigest()
        published = None
        with store().db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = child(db,cid,x_lease_id,request,completed=True)
            if row['state'] == 'completed':
                if row['result_sha'] != digest:
                    raise HTTPException(409, 'Completed chunk payload changed')
                return {'status': 'completed'}
            if any(s.end > row['end']-row['start']+.01 for s in body.segments) or any(b.start<a.start for a,b in zip(body.segments,body.segments[1:])):
                raise HTTPException(422, 'Chunk timestamps must be local and ordered')
            if any(w.end > row['end']-row['start']+.01 for s in body.segments for w in s.words) or any(b.start<a.start for s in body.segments for a,b in zip(s.words,s.words[1:])):
                raise HTTPException(422, 'Chunk word timestamps must be local and ordered')
            if any(s.text.strip() and not s.words for s in body.segments):
                raise HTTPException(422, 'Word timestamps required for safe reconciliation')
            try:
                validate_words([s.model_dump() for s in body.segments],0,row['end']-row['start'])
            except ValueError as exc:
                raise HTTPException(422,str(exc)) from exc
            db.execute("UPDATE chunks SET state='completed',result=?,result_sha=?,lease_until=NULL WHERE id=?", (payload,digest,cid))
            counts = db.execute("SELECT count(*) AS total,sum(state='completed') AS done FROM chunks WHERE job_id=?", (row['job_id'],)).fetchone()
            completed,total = counts['done'],counts['total']
            aggregate(db,row['job_id'])
            db.execute("UPDATE jobs SET progress=?,stage=? WHERE id=?", (.1+.85*completed/total,f'Chunks {completed}/{total}',row['job_id']))
            if completed == total:
                rows = db.execute('SELECT * FROM chunks WHERE job_id=? ORDER BY ordinal', (row['job_id'],)).fetchall()
                values = []
                for item in rows:
                    segments = json.loads(item['result'])['segments']
                    for segment in segments:
                        segment['start'] += item['start']; segment['end'] += item['start']
                        for word in segment['words']:
                            word['start'] += item['start']; word['end'] += item['start']
                    values.append({**dict(item),'segments':segments})
                try:
                    segments = reconcile(values)
                except ValueError as exc:
                    db.execute("UPDATE jobs SET state='failed',stage='Boundary review required' WHERE id=?", (row['job_id'],))
                    db.execute("UPDATE recordings SET status='failed',error=? WHERE id=(SELECT recording_id FROM jobs WHERE id=?)", (str(exc),row['job_id']))
                    return {'status': 'boundary_review_required'}
                preparation = db.execute('SELECT * FROM preparations WHERE job_id=?', (row['job_id'],)).fetchone()
                job = db.execute('SELECT * FROM jobs WHERE id=?', (row['job_id'],)).fetchone()
                published = job['recording_id']
                artifacts = json.loads(preparation['artifacts'])
                final = Completion(duration=preparation['duration'],segments=segments,artifacts=artifacts)
                for segment in final.segments:
                    sid = identity()
                    db.execute('INSERT INTO segments VALUES(?,?,?,?,?,?)', (sid,published,segment.start,segment.end,segment.text,json.dumps([w.model_dump() for w in segment.words])))
                    db.execute('INSERT INTO segment_search VALUES(?,?,?)', (segment.text,sid,published))
                directory = store().root/'results'/row['job_id']/preparation['lease_id']
                for artifact in artifacts:
                    db.execute('INSERT INTO artifacts VALUES(?,?,?,?,?)', (published,artifact['name'],artifact['time'],artifact['kind'],str(directory/artifact['name'])))
                db.execute("UPDATE jobs SET state='completed',progress=1,stage='Ready',result_sha=? WHERE id=?", (hashlib.sha256(final.model_dump_json().encode()).hexdigest(),row['job_id']))
                db.execute("UPDATE recordings SET status='ready',duration=? WHERE id=?", (preparation['duration'],published))
        if published and embeddings.configured:
            background_tasks.add_task(embeddings.refresh,store(),published)
        return {'status': 'completed'}
