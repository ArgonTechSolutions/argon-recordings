"""Admin update requests cannot supply commands; host/worker claims are scoped."""
import time
from fastapi.testclient import TestClient
from argon_recordings.admin_system import HostMessage, exchange
from argon_recordings.api import Settings, create_app
from test_accounts import team,login
from test_host_runner_auth import runner_credentials
from argon_recordings.host_runner_auth import sign


def heartbeat(store,target='app',**kwargs):
    with store.db() as db:return exchange(db,target,HostMessage(revision='a'*40,check={'state':'available','latest_revision':'b'*40,'checked_at':time.time()},**kwargs))


def test_admin_update_confirmation_csrf_queue_claim_receipt(team):
    app,owner,manager,uid=team;headers=login(owner,audience='admin');base='/api/admin/system'
    assert owner.get(base).status_code==200
    body={'target':'app','confirm':'update-latest-main'}
    assert owner.post(base+'/updates',json=body).status_code==403
    assert owner.post(base+'/updates',json=body,headers=headers).status_code==409
    heartbeat(manager.store)
    assert owner.post(base+'/updates',json={**body,'command':'touch /tmp/pwned'},headers=headers).status_code==422
    assert owner.post(base+'/updates',json={**body,'confirm':'yes'},headers=headers).status_code==422
    response=owner.post(base+'/updates',json=body,headers=headers);assert response.status_code==202
    jid=response.json()['id']
    assert owner.post(base+'/updates',json=body,headers=headers).status_code==409
    assert heartbeat(manager.store,'worker-1')['update_id'] is None
    assert heartbeat(manager.store)['update_id']==jid
    assert heartbeat(manager.store)['update_id'] is None  # No automatic replay.
    assert owner.delete(base+'/updates/'+jid,headers=headers).status_code==409
    heartbeat(manager.store,'worker-1',result_id=jid,result='completed')
    assert owner.get(base).json()['updates'][0]['state']=='running'
    heartbeat(manager.store,result_id=jid,result='completed')
    assert owner.get(base).json()['updates'][0]['state']=='completed'
    jid=owner.post(base+'/updates',json=body,headers=headers).json()['id']
    assert owner.delete(base+'/updates/'+jid,headers=headers).json()['state']=='cancelled'
    assert heartbeat(manager.store)['update_id'] is None
    with manager.store.db() as db:db.execute('UPDATE management_hosts SET seen=0')
    assert owner.post(base+'/updates',json=body,headers=headers).status_code==409


def test_worker_status_and_worker_control_are_scoped(tmp_path,runner_credentials):
    def signed(target="worker-1",**values):
        value=HostMessage(revision="a"*40,check={"state":"available","latest_revision":"b"*40,"checked_at":time.time()},**values).model_dump(exclude={"proof"})
        return {**value,"proof":sign(target,value,runner_credentials).model_dump()}
    app=create_app(Settings(tmp_path,auth_mode='accounts',worker_tokens={'worker-1':'one','worker-2':'two'},reserve=0))
    with TestClient(app,base_url='http://localhost:8787') as c:
        manager=app.state.accounts;manager.bootstrap('owner@example.com','Owner','correct horse battery staple')
        headers=login(c,audience='admin')
        state=c.get('/api/admin/system').json()
        assert len(state['workers'])==2 and not any(w['online'] for w in state['workers'])
        assert c.post('/api/worker/management',json={'revision':'a'*40}).status_code==401
        assert c.post('/api/worker/management',json={'revision':'a'*40,'target':'app'},headers={'Authorization':'Bearer one'}).status_code==422
        assert c.post('/api/worker/management',json={'revision':'a'*40},headers={'Authorization':'Bearer one'}).status_code==403
        proof=signed()
        assert c.post('/api/worker/management',json=proof,headers={'Authorization':'Bearer one'}).status_code==200
        assert c.post('/api/worker/management',json=proof,headers={'Authorization':'Bearer one'}).status_code==409
        state=c.get('/api/admin/system').json()
        assert not state['workers'][0]['online'] and state['hosts'][0]['target']=='worker-1'
        assert c.post('/api/worker/ready',headers={'Authorization':'Bearer one'}).status_code==200
        assert c.get('/api/admin/system').json()['workers'][0]['online']
        jid=c.post('/api/admin/system/updates',json={'target':'worker-1','confirm':'update-latest-main'},headers=headers).json()['id']
        assert c.post('/api/worker/management',json=signed(),headers={'Authorization':'Bearer two'}).status_code==403
        assert c.post('/api/worker/management',json={'revision':'a'*40},headers={'Authorization':'Bearer one'}).status_code==403
        assert c.post('/api/worker/management',json=signed(),headers={'Authorization':'Bearer one'}).json()['update_id']==jid
        assert c.post('/api/worker/management',json=signed(result_id=jid,result='completed'),headers={'Authorization':'Bearer one'}).status_code==200
        assert c.get('/api/admin/system').json()['updates'][0]['state']=='completed'
        app.state.worker_seen['worker-1']=time.monotonic()-140
        assert not c.get('/api/admin/system').json()['workers'][0]['online']


def test_check_requested_freshness_and_unknown_blocks_update(team):
    app,owner,manager,uid=team;headers=login(owner,audience='admin');base='/api/admin/system'
    with manager.store.db() as db:exchange(db,'app',HostMessage(revision='a'*40))
    body={'target':'app','confirm':'update-latest-main'}
    assert owner.post(base+'/updates',json=body,headers=headers).status_code==409
    assert owner.post(base+'/check-updates').status_code==403
    assert owner.post(base+'/check-updates',headers=headers).status_code==202
    assert owner.get(base).json()['hosts'][0]['checking']
    for state,age in [('up_to_date',0),('failed',0),('available',601)]:
        with manager.store.db() as db:exchange(db,'app',HostMessage(revision='a'*40,check={'state':state,'checked_at':time.time()-age,'latest_revision':'b'*40,'request_id':1}))
        assert owner.post(base+'/updates',json=body,headers=headers).status_code==409
    with manager.store.db() as db:exchange(db,'app',HostMessage(revision='a'*40,check={'state':'available','checked_at':time.time(),'latest_revision':'b'*40,'request_id':1}))
    assert not owner.get(base).json()['hosts'][0]['checking']
    assert owner.post(base+'/updates',json=body,headers=headers).status_code==202
