"""Per-account caps, attribution, privacy and atomic admission with synthetic calls."""
import asyncio
import httpx
import pytest
from fastapi import HTTPException
from test_accounts import team, login, invited
from test_ai_settings import config
from test_workflow import client
from test_reports import draft
from test_report_generation import isolated, queue
from argon_recordings.report_reading import WRITER_MODEL


def test_default_override_unlimited_inheritance_and_self_only(team):
    app,owner,manager,uid=team
    headers=login(owner,audience='admin')
    guest,guest_headers=invited(app,manager,uid,'guest@example.com')
    try:
        gid=guest.get('/api/auth/session').json()['user']['id']
        base='/api/admin/ai/budgets'
        assert guest.get(base).status_code==401
        assert owner.put(base+'/default',json={'cap_usd':5}).status_code==403
        assert owner.put(base+'/default',json={'cap_usd':-1},headers=headers).status_code==422
        assert owner.put(base+'/default',json={'cap_usd':5},headers=headers).status_code==200
        usage=guest.get('/api/account/usage').json()
        assert usage['user_id']==gid and usage['cap_usd']==5 and usage['cap_source']=='default'
        assert 'users' not in usage and usage['usage_usd']==0
        for cap in [10,0,None]:
            response=owner.put(base+'/users/'+gid,json={'cap_usd':cap},headers=headers)
            assert response.status_code==200
            usage=guest.get('/api/account/usage').json()
            assert usage['cap_usd']==cap and usage['cap_source']=='user'
        owner.put(base+'/default',json={'cap_usd':2},headers=headers)
        assert guest.get('/api/account/usage').json()['cap_usd'] is None
        assert owner.delete(base+'/users/'+gid,headers=headers).status_code==200
        assert guest.get('/api/account/usage').json()['cap_usd']==2
        assert owner.put(base+'/users/missing',json={'cap_usd':1},headers=headers).status_code==404
    finally:guest.close()


def test_user_caps_reserve_pending_calls_and_isolate_other_users(client,tmp_path,monkeypatch):
    rid,_,_=draft(client,tmp_path);other,_,_=draft(client,tmp_path);g=client.app.state.generation
    first,second=queue(client,rid),queue(client,other)
    # Local fixture uses synthetic account rows solely for attribution.
    from argon_recordings.accounts import Accounts
    manager=Accounts(g.store,'accounts','',False)
    uid=manager.bootstrap('one@example.com','One','correct horse battery staple',adopt=True)
    with g.store.db() as db:
        db.execute('UPDATE report_generation SET requester=?',(uid,))
        db.execute('INSERT INTO ai_user_defaults VALUES(1,.007)')
    config(g)
    async def run():
        started=asyncio.Event();release=asyncio.Event();calls=[]
        async def respond(request):
            calls.append(True);started.set();await release.wait()
            return httpx.Response(200,json={'status':'completed','output':[],'usage':{'input_tokens':10,'output_tokens':10}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            pending=asyncio.create_task(g.complete(http,first,WRITER_MODEL,'Read',[{'role':'user','content':'hello'}],{},1000))
            await started.wait()
            try:
                with pytest.raises(HTTPException) as error:await g.complete(http,second,WRITER_MODEL,'Read',[{'role':'user','content':'hello'}],{},1000)
                assert error.value.status_code==429 and 'monthly AI cap' in error.value.detail and len(calls)==1
                with g.store.db() as db:
                    assert g.month_cost(db,requester=uid)>0
                    assert g.month_cost(db,requester='other')==0
            finally:release.set();await pending
    client.portal.call(run)
    with g.store.db() as db:
        actual=g.month_cost(db,requester=uid)
        assert 0<actual<.007
        db.execute('INSERT INTO ai_user_caps VALUES(?,0)',(uid,))
        assert g.user_limit_exceeded(db,uid,.000001)
        db.execute('UPDATE ai_user_caps SET cap=NULL WHERE user_id=?',(uid,))
        assert not g.user_limit_exceeded(db,uid,100)
    monkeypatch.setattr(g,'period',lambda:'2099-01')
    with g.store.db() as db:assert g.month_cost(db,requester=uid)==0
