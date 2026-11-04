"""Admin key isolation, MFA/CSRF and paid-call limits, with synthetic credentials."""
import asyncio
import json
import stat

import httpx
import pytest
from fastapi import HTTPException

from argon_recordings.ai_settings import AIControls,read_key
from argon_recordings.report_generation import Generation
from argon_recordings.report_reading import WRITER_MODEL
from test_accounts import team,login,session,PASSWORD
from test_workflow import client
from test_reports import draft
from test_report_generation import isolated,queue,transport


def config(engine,**values):
    with engine.store.db() as db:db.execute('INSERT OR REPLACE INTO ai_settings VALUES(1,?)',(AIControls(**values).model_dump_json(),))


def test_ai_admin_key_requires_admin_mfa_and_csrf_and_never_echoes_secret(team):
    app,owner,manager,uid=team
    assert owner.get('/api/admin/ai').status_code==401
    login(owner)
    assert owner.put('/api/admin/ai/key',json={'api_key':'synthetic-private-key-12345'},headers=session(owner)).status_code==401
    response=owner.post('/api/auth/login',json={'email':'owner@example.com','password':PASSWORD,'audience':'admin'},headers=session(owner,'admin'))
    assert response.status_code==200
    assert owner.get('/api/admin/ai').status_code==403
    headers=login(owner,audience='admin')
    secret='synthetic-private-key-123456789'
    assert owner.put('/api/admin/ai/key',json={'api_key':secret}).status_code==403
    response=owner.put('/api/admin/ai/key',json={'api_key':secret},headers=headers)
    assert response.status_code==200 and response.json()['key_source']=='administration'
    assert secret not in response.text
    assert app.state.generation.key==secret
    path=manager.store.root/'ai-private'/'openai.key'
    assert stat.S_IMODE(path.stat().st_mode)==0o600
    assert stat.S_IMODE(path.parent.stat().st_mode)==0o700
    assert read_key(manager.store.root)==secret
    assert secret not in owner.get('/api/admin/audit').text
    assert secret not in manager.store.path.read_bytes().decode(errors='ignore')
    invalid=owner.put('/api/admin/ai/key',json={'api_key':secret+'\ninvalid'},headers=headers)
    assert invalid.status_code==422 and secret not in invalid.text
    invalid=owner.put('/api/admin/ai/key',json={'api_key':'é'*4096},headers=headers)
    assert invalid.status_code==422 and 'é' not in invalid.text
    assert app.state.generation.key==secret
    reopened=Generation(app,app.state.generation.settings,None,{})
    assert reopened.key==secret
    response=owner.delete('/api/admin/ai/key',headers=headers)
    assert response.status_code==200 and response.json()['key_source']=='deployment'
    assert not path.exists() and app.state.generation.key=='synthetic-test-only'


def test_admin_limits_validate_and_persist(team):
    app,owner,manager,uid=team;headers=login(owner,audience='admin')
    assert owner.put('/api/admin/ai',json={'report_soft_usd':.2,'report_hard_usd':.1},headers=headers).status_code==422
    assert owner.put('/api/admin/ai',json={'monthly_hard_usd':0},headers=headers).status_code==422
    body={'enabled':True,'report_soft_usd':.05,'report_hard_usd':.2,'monthly_soft_usd':5,'monthly_hard_usd':10}
    response=owner.put('/api/admin/ai',json=body,headers=headers)
    assert response.status_code==200 and response.json()['controls']==body
    assert owner.get('/api/admin/ai').json()['controls']==body


def test_local_mode_cannot_configure_admin_key_and_symlink_is_rejected(client,tmp_path):
    assert client.put('/api/admin/ai/key',json={'api_key':'synthetic-private-key-12345'}).status_code==404
    directory=client.app.state.store.root/'ai-private'; target=tmp_path/'foreign';target.write_text('synthetic-foreign-key')
    (directory/'openai.key').symlink_to(target)
    with pytest.raises(OSError):read_key(client.app.state.store.root)
    assert target.read_text()=='synthetic-foreign-key'


def test_hard_limit_blocks_before_any_paid_request(client,tmp_path,monkeypatch):
    rid,body,_=draft(client,tmp_path);g=client.app.state.generation
    config(g,report_hard_usd=.000001)
    jid=queue(client,rid);calls=[]
    original=httpx.AsyncClient
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kwargs:original(**kwargs,transport=httpx.MockTransport(lambda request:calls.append(request))))
    with pytest.raises(HTTPException) as error:client.portal.call(g.run,jid)
    assert error.value.status_code==429 and not calls
    assert client.get(f'/api/recordings/{rid}/report-generation').json()['jobs'][0]['estimated_cost_usd']==0
    assert not client.get(f'/api/recordings/{rid}/reports').json()['reports']


def test_soft_limit_finishes_without_visual_research_and_warns(client,tmp_path,monkeypatch):
    rid,body,_=draft(client,tmp_path);g=client.app.state.generation
    config(g,report_soft_usd=.000001)
    jid=queue(client,rid,visuals=True)
    sent=transport(monkeypatch,client,body,images=False)
    client.portal.call(g.run,jid)
    assert sent[-1]['model']==WRITER_MODEL and sent[-1]['tools']==[]
    assert sent[-1]['max_output_tokens']==12000 and 'soft cost limit' in sent[-1]['instructions']
    job=client.get(f'/api/recordings/{rid}/report-generation').json()['jobs'][0]
    assert job['state']=='completed' and job['warning']


def test_monthly_hard_limit_reserves_inflight_calls_atomically(client,tmp_path):
    rid,_,_=draft(client,tmp_path);other,_,_=draft(client,tmp_path);g=client.app.state.generation
    first,second=queue(client,rid),queue(client,other)
    config(g,monthly_hard_usd=.007,report_soft_usd=.001)
    async def run():
        started=asyncio.Event();release=asyncio.Event();calls=[]
        async def respond(request):
            calls.append(True);started.set();await release.wait()
            return httpx.Response(200,json={'status':'completed','output':[],'usage':{'input_tokens':10,'output_tokens':10}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            pending=asyncio.create_task(g.complete(http,first,WRITER_MODEL,'Read source',[{'role':'user','content':'hello'}],{},1000))
            await started.wait()
            with g.store.db() as db:assert not g.soft_reached(db,first)
            try:
                with pytest.raises(HTTPException) as error:
                    await g.complete(http,second,WRITER_MODEL,'Read source',[{'role':'user','content':'hello'}],{},1000)
                assert error.value.status_code==429 and len(calls)==1
            finally:release.set();await pending
    client.portal.call(run)


def test_month_limit_uses_each_call_month_when_job_crosses_month_end(client,tmp_path,monkeypatch):
    rid,_,_=draft(client,tmp_path);g=client.app.state.generation
    monkeypatch.setattr(g,'period',lambda:'2026-10')
    jid=queue(client,rid)
    async def paid():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request:httpx.Response(200,json={'status':'completed','output':[],'usage':{'input_tokens':100,'output_tokens':100}}))) as http:
            return await g.complete(http,jid,WRITER_MODEL,'Read',[{'role':'user','content':'source'}],{},1000)
    client.portal.call(paid)
    with g.store.db() as db:old=g.month_cost(db)
    monkeypatch.setattr(g,'period',lambda:'2026-11')
    client.portal.call(paid)
    with g.store.db() as db:
        assert g.month_cost(db)==pytest.approx(old)
        assert db.execute('SELECT cost FROM report_generation WHERE id=?',(jid,)).fetchone()[0]==pytest.approx(old*2)
        assert {r[0] for r in db.execute('SELECT period FROM ai_call_costs')}=={'2026-10','2026-11'}
    config(g,monthly_hard_usd=old+.00001)
    with pytest.raises(HTTPException):client.portal.call(paid)
