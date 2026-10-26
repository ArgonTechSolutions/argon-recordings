"""Explicit, revocable project grants. Agent credentials never authorize browser routes."""
import json
import re
import secrets
import time
from dataclasses import dataclass

from fastapi import HTTPException, Request
from pydantic import Field

from .accounts import digest
from .contracts import Strict
from .store import identity

SCHEMA = '''
CREATE TABLE IF NOT EXISTS agent_grants(
 id TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES users(id),digest TEXT NOT NULL UNIQUE,
 label TEXT NOT NULL,projects TEXT NOT NULL,derive INTEGER NOT NULL DEFAULT 0,
 created REAL NOT NULL,expires REAL NOT NULL,revoked REAL);
CREATE INDEX IF NOT EXISTS agent_grants_user ON agent_grants(user_id);
'''


class GrantInput(Strict):
    label: str = Field(min_length=1,max_length=80,pattern=r'\S')
    project_ids: list[str] = Field(min_length=1,max_length=20)
    derive_media: bool = False
    expires_in_days: int = Field(default=7,ge=1,le=30)


class GrantView(Strict):
    id: str
    label: str
    project_ids: list[str]
    derive_media: bool
    created: float
    expires: float
    revoked: float | None
    expired: bool


class IssuedGrant(Strict):
    grant: GrantView
    token: str


@dataclass(frozen=True)
class Principal:
    grant_id: str
    user_id: str
    projects: tuple[str,...]
    derive: bool


def grant_view(row):
    return GrantView(id=row['id'],label=row['label'],project_ids=json.loads(row['projects']),
        derive_media=bool(row['derive']),created=row['created'],expires=row['expires'],revoked=row['revoked'],
        expired=row['expires']<=time.time())


def authenticate(manager,request):
    """Only the dedicated agent namespace accepts opaque bearer grants."""
    if not manager.enabled: raise HTTPException(404,'Agent access requires account mode')
    headers = [v.decode('latin-1') for k,v in request.scope['headers'] if k.lower()==b'authorization']
    if len(headers)!=1 or not re.fullmatch(r'Bearer rec_agent_[A-Za-z0-9_-]{43}',headers[0]):
        raise HTTPException(401,'A valid agent grant is required')
    # Bounded per-IP authorization attempts; no credential material in limits/audit.
    manager.limit('agent-ip:'+(request.client.host if request.client else ''),maximum=240)
    with manager.store.db() as db:
        row=db.execute('SELECT g.*,u.disabled FROM agent_grants g JOIN users u ON u.id=g.user_id WHERE g.digest=?',
                       (digest(headers[0][7:]),)).fetchone()
        if not row or row['disabled'] or row['revoked'] is not None or row['expires']<=time.time():
            raise HTTPException(401,'A valid agent grant is required')
        principal=Principal(row['id'],row['user_id'],tuple(json.loads(row['projects'])),bool(row['derive']))
    request.state.agent=principal
    # Queue admission uses the same per-user identity as the browser.
    request.state.account={'id':principal.user_id}
    return principal


def authorize_project(manager,principal,pid,derive=False):
    """Recheck grant, user and current membership on every operation."""
    with manager.store.db() as db:
        row=db.execute('SELECT g.projects,g.derive,g.expires,g.revoked,u.disabled,m.role FROM agent_grants g '
            'JOIN users u ON u.id=g.user_id LEFT JOIN memberships m ON m.user_id=u.id AND m.project_id=? '
            'WHERE g.id=? AND g.user_id=?',(pid,principal.grant_id,principal.user_id)).fetchone()
        if not row or row['disabled'] or row['revoked'] is not None or row['expires']<=time.time():
            raise HTTPException(401,'Agent grant expired or revoked')
        if pid not in json.loads(row['projects']) or not row['role']:
            raise HTTPException(404,'Project not found')
        if derive and (not row['derive'] or row['role'] not in {'owner','editor'}):
            raise HTTPException(403,'This grant cannot request derived media')
        return row['role']


def authorize_recording(manager,principal,rid,derive=False):
    row=manager.store.recording(rid)
    if not row: raise HTTPException(404,'Recording not found')
    authorize_project(manager,principal,row['project_id'],derive)
    return row


def install(app,get_accounts):
    def account(request):
        manager=get_accounts()
        if not manager.enabled: raise HTTPException(404,'Agent access requires account mode')
        if request.headers.get('x-session-audience','app') != 'app':
            raise HTTPException(403,'Use an app session to manage agent access')
        # These routes additionally require an ordinary authenticated app session;
        # an agent grant and admin audience cannot mint or revoke credentials.
        user=getattr(request.state,'account',None)
        if not user: raise HTTPException(401,'Sign in to manage agent access')
        return manager,user

    @app.get('/api/auth/agent-grants',response_model=list[GrantView])
    def list_grants(request: Request):
        manager,user=account(request)
        with manager.store.db() as db:
            return [grant_view(r) for r in db.execute('SELECT * FROM agent_grants WHERE user_id=? ORDER BY created DESC',(user['id'],))]

    @app.post('/api/auth/agent-grants',response_model=IssuedGrant)
    def issue(body: GrantInput,request: Request):
        manager,user=account(request)
        projects=sorted(set(body.project_ids))
        if any(not re.fullmatch(r'[a-f0-9]{32}',pid) for pid in projects):
            raise HTTPException(422,'Choose valid projects')
        with manager.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            for pid in projects:
                membership=db.execute('SELECT role FROM memberships WHERE project_id=? AND user_id=?',(pid,user['id'])).fetchone()
                if not membership: raise HTTPException(404,'Project not found')
                if body.derive_media and membership['role'] not in {'editor','owner'}:
                    raise HTTPException(403,'Your project role cannot request derived media')
            if db.execute('SELECT count(*) FROM agent_grants WHERE user_id=?',(user['id'],)).fetchone()[0]>=100:
                raise HTTPException(409,'Agent grant history limit reached')
            if db.execute('SELECT count(*) FROM agent_grants WHERE user_id=? AND revoked IS NULL AND expires>?',(user['id'],time.time())).fetchone()[0]>=20:
                raise HTTPException(429,'Revoke an existing grant before creating another')
            token='rec_agent_'+secrets.token_urlsafe(32)
            gid=identity();now=time.time()
            db.execute('INSERT INTO agent_grants VALUES(?,?,?,?,?,?,?,?,NULL)',
                (gid,user['id'],digest(token),body.label.strip(),json.dumps(projects),int(body.derive_media),now,now+body.expires_in_days*86400))
            manager.audit(db,user['id'],'agent-grant-created',gid)
            view=grant_view(db.execute('SELECT * FROM agent_grants WHERE id=?',(gid,)).fetchone())
        return IssuedGrant(grant=view,token=token)

    @app.delete('/api/auth/agent-grants/{gid}')
    def revoke(gid: str,request: Request):
        manager,user=account(request)
        with manager.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT * FROM agent_grants WHERE id=? AND user_id=?',(gid,user['id'])).fetchone()
            if not row: raise HTTPException(404,'Agent grant not found')
            if row['revoked'] is None:
                db.execute('UPDATE agent_grants SET revoked=? WHERE id=?',(time.time(),gid))
                manager.audit(db,user['id'],'agent-grant-revoked',gid)
        return {'ok':True}
