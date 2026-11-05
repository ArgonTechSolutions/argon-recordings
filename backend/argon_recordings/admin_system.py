"""Admin monitoring and durable, fixed-action update requests.

Only an explicitly started host runner executes updates. Containers never receive
host execution rights or a Docker socket. Worker credentials address only self.
"""
import time
import sqlite3
from typing import Literal
from fastapi import Depends, HTTPException, Request
from pydantic import Field
from .contracts import Strict
from .store import identity
from .operational_logs import record
from .host_runner_auth import HostProof, verify as verify_host_proof

SCHEMA = '''
CREATE TABLE IF NOT EXISTS management_hosts(target TEXT PRIMARY KEY,seen REAL NOT NULL,revision TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS management_updates(id TEXT PRIMARY KEY,target TEXT NOT NULL,state TEXT NOT NULL,
 created REAL NOT NULL,updated REAL NOT NULL,error TEXT,requester TEXT NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS management_active ON management_updates((1)) WHERE state IN ('queued','running');
CREATE TABLE IF NOT EXISTS management_nonces(target TEXT NOT NULL,nonce TEXT NOT NULL,created REAL NOT NULL,PRIMARY KEY(target,nonce));
'''

class HostMessage(Strict):
    revision: str = Field(pattern=r'^(?:[a-f0-9]{40,64}|unknown)$')
    result_id: str | None = Field(default=None,pattern=r'^[a-f0-9]{32}$')
    result: Literal['completed','failed'] | None = None
    proof: HostProof | None = None

class UpdateRequest(Strict):
    target: str = Field(pattern=r'^(?:app|worker-[1-8])$')
    confirm: Literal['update-latest-main']

class UpdateView(Strict):
    id: str
    target: str
    state: str
    created: float
    updated: float
    error: str | None

class WorkerView(Strict):
    id: str
    online: bool
    last_contact_seconds: float | None
    diarization_available: bool
    active_tasks: int

class HostView(Strict):
    target: str
    online: bool
    source_revision: str

class SystemState(Strict):
    workers: list[WorkerView]
    hosts: list[HostView]
    updates: list[UpdateView]


def exchange(db,target,body,*,proof_nonce=None):
    now=time.time()
    db.execute('BEGIN IMMEDIATE')
    if proof_nonce:
        db.execute('DELETE FROM management_nonces WHERE created<?',(now-240,))
        try:db.execute('INSERT INTO management_nonces VALUES(?,?,?)',(target,proof_nonce,now))
        except sqlite3.IntegrityError:raise HTTPException(409,'Host runner proof was already used') from None
    db.execute('INSERT OR REPLACE INTO management_hosts VALUES(?,?,?)',(target,now,body.revision))
    if body.result_id:
        if body.result is None:raise ValueError('Missing update result')
        changed=db.execute("UPDATE management_updates SET state=?,updated=?,error=? WHERE id=? AND target=? AND state='running'",
            (body.result,now,'Host update failed or was interrupted; inspect the host runner log before retrying.' if body.result=='failed' else None,body.result_id,target))
        if changed.rowcount:record(db,'app' if target=='app' else target,'updates','error' if body.result=='failed' else 'info','update-'+body.result,'Host update '+body.result+'.')
    row=db.execute("SELECT id FROM management_updates WHERE target=? AND state='queued' ORDER BY created LIMIT 1",(target,)).fetchone()
    if row:
        db.execute("UPDATE management_updates SET state='running',updated=? WHERE id=?",(now,row['id']))
        record(db,'app' if target=='app' else target,'updates','info','update-started','Host runner claimed the update; services may restart.')
    return {'update_id':row['id'] if row else None}


def install(app,settings,worker_auth):
    def admin(request):
        if not app.state.accounts.enabled:raise HTTPException(404,'Administration requires accounts mode')
        if not getattr(request.state,'account',{}).get('admin'):raise HTTPException(403,'Administrator access required')

    @app.get('/api/admin/system',response_model=SystemState)
    def status(request: Request):
        admin(request);workers=[];now=time.monotonic()
        configured=set(settings.worker_tokens) or ({'legacy'} if settings.worker_token else set())
        with app.state.store.db() as db:
            for wid in sorted(configured|set(app.state.worker_seen)):
                seen=app.state.worker_seen.get(wid);age=max(0,now-seen) if seen is not None else None
                active=0
                for table in ('jobs','chunks','corrections','diarization_jobs','visual_jobs','analysis_jobs'):
                    active+=db.execute(f"SELECT count(*) FROM {table} WHERE worker=? AND state='running' AND lease_until>?",(wid,time.time())).fetchone()[0]
                workers.append(WorkerView(id=wid,online=age is not None and age<130,last_contact_seconds=age,
                    diarization_available=bool(app.state.worker_capabilities.get(wid)),active_tasks=active))
            hosts=[HostView(target=r['target'],online=time.time()-r['seen']<90,source_revision=r['revision']) for r in db.execute('SELECT * FROM management_hosts ORDER BY target')]
            updates=[UpdateView(**dict(r)) for r in db.execute('SELECT id,target,state,created,updated,error FROM management_updates ORDER BY created DESC LIMIT 20')]
        return SystemState(workers=workers,hosts=hosts,updates=updates)

    @app.post('/api/admin/system/updates',response_model=UpdateView,status_code=202)
    def queue(body: UpdateRequest,request: Request):
        admin(request)
        with app.state.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            host=db.execute('SELECT seen FROM management_hosts WHERE target=?',(body.target,)).fetchone()
            if not host or time.time()-host[0]>=90:raise HTTPException(409,'The host update runner is offline or has not been enabled')
            if db.execute("SELECT 1 FROM management_updates WHERE state IN ('queued','running')").fetchone():raise HTTPException(409,'An update is already queued or running')
            now=time.time();jid=identity()
            db.execute('INSERT INTO management_updates VALUES(?,?,?,?,?,NULL,?)',(jid,body.target,'queued',now,now,request.state.account['id']))
            app.state.accounts.audit(db,request.state.account['id'],'system-update-requested',body.target)
            record(db,'app' if body.target=='app' else body.target,'updates','info','update-queued','Administrator requested a host update.')
            return UpdateView(id=jid,target=body.target,state='queued',created=now,updated=now,error=None)

    @app.delete('/api/admin/system/updates/{jid}',response_model=UpdateView)
    def cancel(jid: str,request: Request):
        admin(request)
        with app.state.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT * FROM management_updates WHERE id=?',(jid,)).fetchone()
            if not row:raise HTTPException(404,'Update not found')
            if row['state']!='queued':raise HTTPException(409,'Only a queued update can be cancelled; inspect the host for a running update')
            now=time.time();db.execute("UPDATE management_updates SET state='cancelled',updated=? WHERE id=?",(now,jid))
            app.state.accounts.audit(db,request.state.account['id'],'system-update-cancelled',row['target'])
            record(db,'app' if row['target']=='app' else row['target'],'updates','info','update-cancelled','Administrator cancelled a queued host update.')
            return UpdateView(**{k:(now if k=='updated' else 'cancelled' if k=='state' else row[k]) for k in UpdateView.model_fields})

    @app.post('/api/worker/management',dependencies=[Depends(worker_auth)])
    def worker_exchange(body: HostMessage,request: Request):
        wid=request.state.worker_id
        if wid is None:raise HTTPException(403,'Host management requires an isolated worker identity')
        nonce=verify_host_proof(wid,body.model_dump(exclude={'proof'}),body.proof)
        with app.state.store.db() as db:return exchange(db,wid,body,proof_nonce=nonce)
