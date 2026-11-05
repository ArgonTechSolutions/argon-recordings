"""Bounded operational summaries. Never store raw exceptions, requests or media."""
import time
from typing import Literal
from fastapi import Depends, HTTPException, Query, Request
from pydantic import Field
from .contracts import Strict

SCHEMA='''CREATE TABLE IF NOT EXISTS operational_logs(
 id INTEGER PRIMARY KEY AUTOINCREMENT,created REAL NOT NULL,source TEXT NOT NULL,
 category TEXT NOT NULL,level TEXT NOT NULL,event TEXT NOT NULL,summary TEXT NOT NULL,occurrences INTEGER NOT NULL DEFAULT 1);
'''


def record(db,source,category,level,event,summary):
    recent=db.execute('SELECT id FROM operational_logs WHERE source=? AND event=? AND summary=? AND created>=? ORDER BY id DESC LIMIT 1',(source,event,summary[:1000],time.time()-60)).fetchone()
    if recent:
        db.execute('UPDATE operational_logs SET occurrences=occurrences+1 WHERE id=?',(recent[0],))
        return
    db.execute('INSERT INTO operational_logs(created,source,category,level,event,summary) VALUES(?,?,?,?,?,?)',
        (time.time(),source,category,level,event,summary[:1000]))
    db.execute('DELETE FROM operational_logs WHERE id<=(SELECT max(id)-10000 FROM operational_logs)')


class WorkerLog(Strict):
    kind: Literal['error','summary']
    error_type: str | None = Field(default=None,pattern=r'^[A-Za-z_][A-Za-z_0-9]{0,79}$')

class LogEntry(Strict):
    id: int
    created: float
    source: str
    category: str
    level: str
    event: str
    summary: str
    occurrences: int

class LogPage(Strict):
    entries: list[LogEntry]
    next_before: int | None


def install(app,worker_auth):
    @app.get('/api/admin/logs',response_model=LogPage)
    def logs(request: Request,category: Literal['errors','usage','updates','system'] | None=None,
             source: str | None=Query(default=None,pattern=r'^(?:app|legacy|worker-[1-8])$'),
             before: int | None=Query(default=None,ge=1),limit: int=Query(default=50,ge=1,le=100)):
        if not app.state.accounts.enabled:raise HTTPException(404,'Administration requires accounts mode')
        if not getattr(request.state,'account',{}).get('admin'):raise HTTPException(403,'Administrator access required')
        where=[];values=[]
        for field,value in [('category',category),('source',source)]:
            if value is not None:where.append(field+'=?');values.append(value)
        if before is not None:where.append('id<?');values.append(before)
        clause=' WHERE '+' AND '.join(where) if where else ''
        with app.state.store.db() as db:
            rows=db.execute('SELECT * FROM operational_logs'+clause+' ORDER BY id DESC LIMIT ?',(*values,limit+1)).fetchall()
        return LogPage(entries=[LogEntry(**dict(row)) for row in rows[:limit]],next_before=rows[limit-1]['id'] if len(rows)>limit else None)

    @app.post('/api/worker/logs',dependencies=[Depends(worker_auth)])
    def worker_log(body: WorkerLog,request: Request):
        wid=request.state.worker_id or 'legacy'
        summary=('Processing cycle failed ('+(body.error_type or 'UnknownError')+'). Inspect the worker host log for details.' if body.kind=='error' else 'Processing cycle completed.')
        with app.state.store.db() as db:
            record(db,wid,'errors' if body.kind=='error' else 'usage','error' if body.kind=='error' else 'info','worker-'+body.kind,summary)
        return {'ok':True}
