"""Categorized operational summaries exclude raw input and are bounded/paged."""
from test_accounts import team,login
from test_workflow import client
from test_report_generation import isolated
from argon_recordings.operational_logs import record


def test_admin_logs_filter_pagination_and_repeated_summary_grouping(team):
    app,owner,manager,uid=team
    assert owner.get('/api/admin/logs').status_code==401
    login(owner)
    assert owner.get('/api/admin/logs').status_code==401
    login(owner,audience='admin')
    with manager.store.db() as db:
        record(db,'worker-1','errors','error','task-failed','Worker reported a task failure.')
        record(db,'worker-1','errors','error','task-failed','Worker reported a task failure.')
        record(db,'app','usage','info','summary','Report generation completed; estimated cost $0.123.')
    page=owner.get('/api/admin/logs?category=errors&source=worker-1').json()
    assert len(page['entries'])==1 and page['entries'][0]['occurrences']==2
    assert page['entries'][0]['source']=='worker-1'
    first=owner.get('/api/admin/logs?limit=1').json()
    assert first['next_before'] is not None
    second=owner.get('/api/admin/logs?limit=1&before='+str(first['next_before'])).json()
    assert second['entries'][0]['id']<first['entries'][0]['id']
    assert owner.get('/api/admin/logs?source=arbitrary').status_code==422
    assert owner.get('/api/admin/logs?limit=101').status_code==422


def test_worker_log_schema_cannot_inject_raw_messages_or_choose_app_source(team):
    app,owner,manager,uid=team;login(owner,audience='admin')
    headers={'Authorization':'Bearer worker-only'}
    assert owner.post('/api/worker/logs',json={'kind':'error','error_type':'RuntimeError'},headers=headers).status_code==200
    assert owner.post('/api/worker/logs',json={'kind':'error','error_type':'synthetic secret value'},headers=headers).status_code==422
    assert owner.post('/api/worker/logs',json={'kind':'error','message':'private transcript','source':'app'},headers=headers).status_code==422
    assert owner.post('/api/worker/logs',json={'kind':'summary'}).status_code==401
    rows=owner.get('/api/admin/logs?category=errors').json()['entries']
    assert rows[0]['source']=='legacy' and 'RuntimeError' in rows[0]['summary']
    assert 'private transcript' not in str(rows) and 'synthetic secret value' not in str(rows)


def test_log_retention_does_not_grow_without_bound(team):
    app,owner,manager,uid=team
    with manager.store.db() as db:
        db.executemany('INSERT INTO operational_logs(created,source,category,level,event,summary) VALUES(?,?,?,?,?,?)',[(0,'app','system','info',str(i),'fixture') for i in range(10005)])
        record(db,'app','system','info','latest','Latest summary')
        assert db.execute('SELECT count(*) FROM operational_logs').fetchone()[0]==10000
        assert db.execute('SELECT summary FROM operational_logs ORDER BY id DESC LIMIT 1').fetchone()[0]=='Latest summary'


def test_log_storage_failure_preserves_original_service_error(client,monkeypatch):
    import sqlite3
    from test_workflow import project,upload
    from argon_recordings import operational_logs
    rid=upload(client,project(client))
    client.app.state.generation.key=''
    client.app.state.generation._managed_key=''
    def unavailable(*args,**kwargs):raise sqlite3.OperationalError('Synthetic storage failure')
    monkeypatch.setattr(operational_logs,'record',unavailable)
    response=client.post(f'/api/recordings/{rid}/report-generation',json={'kind':'summary'})
    assert response.status_code==503 and 'no API key' in response.json()['detail']


def test_generation_failure_logs_safe_usage_summary(client,tmp_path,monkeypatch):
    import asyncio
    from fastapi import HTTPException
    from test_reports import draft
    from test_report_generation import queue
    rid,_,_=draft(client,tmp_path);g=client.app.state.generation
    jid=queue(client,rid)
    with g.store.db() as db:db.execute("UPDATE report_generation SET state='queued' WHERE id=?",(jid,))
    async def fail(jid):
        with g.store.db() as db:db.execute('UPDATE report_generation SET cost=.125,input_tokens=100,output_tokens=10 WHERE id=?',(jid,))
        raise HTTPException(502,'synthetic private transcript detail')
    monkeypatch.setattr(g,'run',fail)
    async def exercise():
        task=asyncio.create_task(g.loop())
        try:
            async def logged():
                while True:
                    with g.store.db() as db:
                        if db.execute("SELECT 1 FROM operational_logs WHERE event='report-generation-failed'").fetchone():return
                    await asyncio.sleep(.01)
            await asyncio.wait_for(logged(),3)
        finally:
            task.cancel()
            try:await task
            except asyncio.CancelledError:pass
    client.portal.call(exercise)
    with g.store.db() as db:
        rows=[dict(r) for r in db.execute("SELECT * FROM operational_logs WHERE event LIKE 'report-generation-%'")]
    assert {r['category'] for r in rows}=={'usage','errors'}
    assert '$0.125000' in str(rows) and 'input tokens 100' in str(rows)
    assert 'synthetic private transcript detail' not in str(rows)
