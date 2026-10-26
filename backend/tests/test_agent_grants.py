"""Disposable account grants; no production credentials are provisioned."""
import json
import time

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from test_accounts import team, login, invited
from argon_recordings.agent_grants import authenticate,authorize_project,authorize_recording
from argon_recordings.accounts import digest


def issue(owner,headers,pid,**options):
    response=owner.post('/api/auth/agent-grants',headers=headers,
        json={'label':'Synthetic test agent','project_ids':[pid],**options})
    assert response.status_code==200,response.text
    return response.json()


def request_for(token):
    return Request({'type':'http','path':'/api/agent/projects','method':'GET',
        'headers':[(b'authorization',('Bearer '+token).encode())],
        'client':('synthetic-test',0),'server':('testserver',80),'scheme':'http','query_string':b''})


def test_explicit_grants_are_hashed_scoped_revocable_and_not_browser_sessions(team):
    app,owner,manager,uid=team;headers=login(owner)
    pid=owner.post('/api/projects',headers=headers,json={'name':'Scoped'}).json()['id']
    other=owner.post('/api/projects',headers=headers,json={'name':'Other'}).json()['id']
    granted=issue(owner,headers,pid);token=granted['token'];gid=granted['grant']['id']
    assert token.startswith('rec_agent_') and 'token' not in json.dumps(owner.get('/api/auth/agent-grants').json())
    with manager.store.db() as db:
        row=db.execute('SELECT * FROM agent_grants WHERE id=?',(gid,)).fetchone()
        assert row['digest']==digest(token) and token not in json.dumps(dict(row))
        assert token not in json.dumps([tuple(r) for r in db.execute('SELECT * FROM account_audit')])
    principal=authenticate(manager,request_for(token))
    assert authorize_project(manager,principal,pid)=='owner'
    with pytest.raises(HTTPException) as error: authorize_project(manager,principal,other)
    assert error.value.status_code==404
    with pytest.raises(HTTPException) as error: authorize_project(manager,principal,pid,derive=True)
    assert error.value.status_code==403
    # A bearer token cannot replace an app cookie or CSRF proof.
    owner.cookies.clear()
    assert owner.get('/api/projects',headers={'Authorization':'Bearer '+token}).status_code==401
    assert owner.post('/api/auth/agent-grants',headers={'Authorization':'Bearer '+token},json={}).status_code==401
    headers=login(owner)
    assert owner.delete('/api/auth/agent-grants/'+gid,headers=headers).status_code==200
    assert owner.delete('/api/auth/agent-grants/'+gid,headers=headers).status_code==200
    with pytest.raises(HTTPException) as error: authenticate(manager,request_for(token))
    assert error.value.status_code==401
    with pytest.raises(HTTPException): authorize_project(manager,principal,pid)


def test_current_membership_and_disabled_users_gate_every_operation(team):
    app,owner,manager,uid=team;headers=login(owner)
    pid=owner.post('/api/projects',headers=headers,json={'name':'Shared'}).json()['id']
    rid=owner.post(f'/api/projects/{pid}/recordings',headers=headers,json={'title':'Demo','filename':'demo.mp4'}).json()['id']
    guest,gh=invited(app,manager,uid,'agent-guest@example.com')
    try:
        assert guest.post('/api/auth/agent-grants',headers=gh,json={'label':'No access','project_ids':[pid]}).status_code==404
        owner.put(f'/api/projects/{pid}/members',headers=headers,json={'email':'agent-guest@example.com','role':'editor'})
        granted=issue(guest,gh,pid,derive_media=True)
        principal=authenticate(manager,request_for(granted['token']))
        assert authorize_recording(manager,principal,rid,derive=True)['id']==rid
        owner.put(f'/api/projects/{pid}/members',headers=headers,json={'email':'agent-guest@example.com','role':'viewer'})
        assert authorize_project(manager,principal,pid)=='viewer'
        with pytest.raises(HTTPException) as error: authorize_recording(manager,principal,rid,derive=True)
        assert error.value.status_code==403
        assert guest.post('/api/auth/agent-grants',headers=gh,json={'label':'Denied derivation','project_ids':[pid],'derive_media':True}).status_code==403
        assert owner.delete('/api/auth/agent-grants/'+granted['grant']['id'],headers=headers).status_code==404
        with manager.store.db() as db: db.execute('UPDATE users SET disabled=1 WHERE id=?',(principal.user_id,))
        with pytest.raises(HTTPException): authenticate(manager,request_for(granted['token']))
        with pytest.raises(HTTPException): authorize_project(manager,principal,pid)
        with manager.store.db() as db:
            db.execute('UPDATE users SET disabled=0 WHERE id=?',(principal.user_id,))
            db.execute('DELETE FROM memberships WHERE user_id=? AND project_id=?',(principal.user_id,pid))
        with pytest.raises(HTTPException) as error: authorize_project(manager,principal,pid)
        assert error.value.status_code==404
    finally: guest.close()


def test_grant_expiry_csrf_and_admin_content_boundaries(team):
    app,owner,manager,uid=team;headers=login(owner)
    pid=owner.post('/api/projects',headers=headers,json={'name':'Private'}).json()['id']
    assert owner.post('/api/auth/agent-grants',json={'label':'CSRF','project_ids':[pid]}).status_code==403
    assert owner.post('/api/auth/agent-grants',headers=headers,json={'label':'Long','project_ids':[pid],'expires_in_days':31}).status_code==422
    assert owner.post('/api/auth/agent-grants',headers=headers,json={'label':'Escalate','project_ids':[pid],'admin':True}).status_code==422
    granted=issue(owner,headers,pid)
    with manager.store.db() as db: db.execute('UPDATE agent_grants SET expires=? WHERE id=?',(time.time()-1,granted['grant']['id']))
    with pytest.raises(HTTPException): authenticate(manager,request_for(granted['token']))
    admin=login(owner,audience='admin')
    assert owner.post('/api/auth/agent-grants',headers=admin,json={'label':'Wrong audience','project_ids':[pid]}).status_code==403
    guest,gh=invited(app,manager,uid,'private-owner@example.com')
    try:
        their_pid=guest.post('/api/projects',headers=gh,json={'name':'No admin bypass'}).json()['id']
        # Admin status carries no membership or content authority.
        assert owner.post('/api/auth/agent-grants',headers=headers,json={'label':'Admin bypass','project_ids':[their_pid]}).status_code==404
    finally: guest.close()
