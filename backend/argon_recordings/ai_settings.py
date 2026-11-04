"""Administrator AI controls. Secret values never enter SQLite or responses."""
import os
from pathlib import Path
import stat
import tempfile
from typing import Annotated

from fastapi import HTTPException, Request
from pydantic import Field, SecretStr, field_validator, model_validator

from .contracts import Strict

Money = Annotated[float, Field(gt=0,le=1000000,allow_inf_nan=False)]
SCHEMA = '''CREATE TABLE IF NOT EXISTS ai_settings(id INTEGER PRIMARY KEY CHECK(id=1),value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ai_call_costs(
 id TEXT PRIMARY KEY,job_id TEXT NOT NULL REFERENCES report_generation(id),period TEXT NOT NULL,
 model TEXT NOT NULL,cost REAL NOT NULL,status TEXT NOT NULL,reason TEXT,created REAL NOT NULL);
CREATE INDEX IF NOT EXISTS ai_call_job ON ai_call_costs(job_id);
CREATE INDEX IF NOT EXISTS ai_call_month ON ai_call_costs(period);
'''


class AIControls(Strict):
    enabled: bool = True
    report_soft_usd: Money | None = None
    report_hard_usd: Money | None = None
    monthly_soft_usd: Money | None = None
    monthly_hard_usd: Money | None = None

    @model_validator(mode='after')
    def ordered(self):
        for prefix in ('report','monthly'):
            soft,hard=getattr(self,prefix+'_soft_usd'),getattr(self,prefix+'_hard_usd')
            if soft is not None and hard is not None and soft>hard:
                raise ValueError('A soft limit cannot exceed its hard limit')
        return self


class AIAdminState(Strict):
    controls: AIControls
    key_configured: bool
    key_source: str
    month_cost_usd: float
    reader_model: str
    writer_model: str


class APIKeyInput(Strict):
    api_key: SecretStr = Field(min_length=20,max_length=4096)

    @field_validator('api_key')
    @classmethod
    def single_value(cls,value):
        raw=value.get_secret_value()
        if not raw.isascii() or raw!=raw.strip() or any(c.isspace() for c in raw):
            raise ValueError('Enter a single ASCII API key without spaces or line breaks')
        return value


def controls(db,legacy_budget):
    row=db.execute('SELECT value FROM ai_settings WHERE id=1').fetchone()
    return AIControls.model_validate_json(row[0]) if row else AIControls(enabled=legacy_budget>0,monthly_soft_usd=legacy_budget or None)


def private_dir(root):
    path=Path(root)/'ai-private'
    path.mkdir(mode=0o700,exist_ok=True)
    info=path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_mode&0o077 or info.st_uid!=os.geteuid():
        raise RuntimeError('AI credential directory must be private and owned by the application')
    return path


def read_key(root):
    path=private_dir(root)/'openai.key'
    try: fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
    except FileNotFoundError:return ''
    with os.fdopen(fd,'r') as stream:
        info=os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode&0o077 or info.st_uid!=os.geteuid() or info.st_nlink!=1 or info.st_size>4096:
            raise RuntimeError('AI credential file must be a private regular file owned by the application')
        return stream.read().strip()


def write_key(root,value):
    directory=private_dir(root)
    fd,name=tempfile.mkstemp(prefix='.key-',dir=directory)
    try:
        with os.fdopen(fd,'w') as stream:
            os.fchmod(stream.fileno(),0o600);stream.write(value);stream.flush();os.fsync(stream.fileno())
        os.replace(name,directory/'openai.key')
        fd=os.open(directory,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(fd)
        finally:os.close(fd)
    finally:
        Path(name).unlink(missing_ok=True)


def install(app):
    def require_admin(request):
        manager=app.state.accounts
        if not manager.enabled:raise HTTPException(404,'AI administration requires accounts mode')
        # The account middleware enforces the admin audience, MFA and CSRF.
        if not getattr(request.state,'account',{}).get('admin'):raise HTTPException(403,'Administrator access required')
        return manager,app.state.generation

    def view(g):
        from .report_reading import READER_MODEL,WRITER_MODEL
        with g.store.db() as db:
            return AIAdminState(controls=controls(db,g.budget),key_configured=bool(g.key),
                key_source='administration' if g._managed_key else 'deployment' if g.key else 'none',
                month_cost_usd=g.month_cost(db),reader_model=READER_MODEL,writer_model=WRITER_MODEL)

    @app.get('/api/admin/ai',response_model=AIAdminState)
    def status(request: Request):
        _,g=require_admin(request);return view(g)

    @app.put('/api/admin/ai',response_model=AIAdminState)
    def update(body: AIControls,request: Request):
        manager,g=require_admin(request)
        with g.store.db() as db:
            db.execute('INSERT OR REPLACE INTO ai_settings VALUES(1,?)',(body.model_dump_json(),))
            manager.audit(db,request.state.account['id'],'ai-controls-updated')
        return view(g)

    @app.put('/api/admin/ai/key',response_model=AIAdminState)
    def replace(body: APIKeyInput,request: Request):
        manager,g=require_admin(request)
        value=body.api_key.get_secret_value()
        with g._key_lock:
            write_key(g.store.root,value);g._managed_key=value
        with g.store.db() as db:manager.audit(db,request.state.account['id'],'ai-key-replaced')
        return view(g)

    @app.delete('/api/admin/ai/key',response_model=AIAdminState)
    def remove(request: Request):
        manager,g=require_admin(request)
        with g._key_lock:
            (private_dir(g.store.root)/'openai.key').unlink(missing_ok=True);g._managed_key=''
        with g.store.db() as db:manager.audit(db,request.state.account['id'],'ai-key-override-removed')
        return view(g)
