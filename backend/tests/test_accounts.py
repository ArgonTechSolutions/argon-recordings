"""Account lifecycle and authorization exercised against the actual SQLite/API."""
import time

from fastapi.testclient import TestClient
import pytest

from argon_recordings.api import Settings, create_app
from argon_recordings.accounts import Accounts, cookie, digest
from argon_recordings.store import Store

PASSWORD = 'correct horse battery staple'


@pytest.fixture
def team(tmp_path):
    app = create_app(Settings(tmp_path, auth_mode='accounts', worker_token='worker-only', reserve=0))
    with TestClient(app, base_url='http://localhost:8787') as owner:
        manager = app.state.accounts
        uid = manager.bootstrap('owner@example.com', 'Owner', PASSWORD)
        yield app, owner, manager, uid


def session(client, audience='app'):
    response = client.get('/api/auth/session', params={'audience': audience})
    assert response.status_code == 200
    return {'X-CSRF-Token': response.json()['csrf'], 'X-Session-Audience': audience}


def login(client, address='owner@example.com', audience='app'):
    response = client.post('/api/auth/login', json={'email': address, 'password': PASSWORD, 'audience': audience}, headers=session(client, audience))
    assert response.status_code == 200, response.text
    if audience == 'admin':
        from passkey_fixture import enroll, headers as mfa_headers
        manager = client.app.state.accounts
        enroll(client, manager, address)
        return mfa_headers(client)
    return session(client, audience)


def invited(app, manager, owner_id, address):
    token = manager.invite(owner_id, address)
    client = TestClient(app, base_url='http://localhost:8787')
    response = client.post('/api/auth/accept-invitation', json={'token': token, 'name': address.split('@')[0], 'password': PASSWORD}, headers=session(client))
    assert response.status_code == 200, response.text
    return client, session(client)


def test_invitation_only_replay_csrf_and_hashed_sessions(team):
    app, owner, manager, uid = team
    assert owner.get('/api/projects').status_code == 401
    assert owner.post('/api/auth/login', json={'email':'owner@example.com','password':PASSWORD}).status_code == 403
    headers = login(owner, audience='admin')
    token = owner.post('/api/admin/invitations', json={'email':'member@example.com'}, headers=headers).json()['token']
    guest = TestClient(app)
    body = {'token':token,'name':'Member','password':PASSWORD}
    assert guest.post('/api/auth/accept-invitation', json=body, headers=session(guest)).status_code == 200
    assert guest.post('/api/auth/accept-invitation', json=body, headers=session(guest)).status_code == 400
    raw = guest.cookies.get(cookie('app'))
    with manager.store.db() as db:
        assert db.execute('SELECT 1 FROM sessions WHERE digest=?', (digest(raw),)).fetchone()
        assert not db.execute('SELECT 1 FROM sessions WHERE digest=?', (raw,)).fetchone()
        assert db.execute("SELECT password FROM users WHERE email='member@example.com'").fetchone()[0].startswith('$argon2id$')
    # Administrative sessions never authorize ordinary project routes.
    assert owner.get('/api/projects', headers={'X-Session-Audience':'admin'}).status_code == 403
    assert guest.get('/api/admin/users').status_code == 401
    response = owner.patch('/api/admin/settings', json={'registration':'closed'}, headers=headers)
    assert response.status_code == 200
    assert owner.post('/api/admin/invitations', json={'email':'closed@example.com'}, headers=headers).status_code == 409


def test_disjoint_project_access_and_role_changes(team):
    app, owner, manager, uid = team
    headers = login(owner)
    pid = owner.post('/api/projects', json={'name':'Private'}, headers=headers).json()['id']
    rid = owner.post(f'/api/projects/{pid}/recordings', json={'title':'Secret','filename':'secret.wav'}, headers=headers).json()['id']
    guest, guest_headers = invited(app, manager, uid, 'guest@example.com')
    try:
        assert guest.get('/api/projects').json() == []
        paths = [f'/api/projects/{pid}/recordings', f'/api/recordings/{rid}', f'/api/recordings/{rid}/transcript',
                 f'/api/recordings/{rid}/media/audio.wav', f'/api/recordings/{rid}/corrections', f'/api/projects/{pid}/events',
                 f'/api/search?project_id={pid}&q=secret&mode=keyword']
        for path in paths:
            assert guest.get(path).status_code == 404, path
        assert guest.put(f'/api/recordings/{rid}/source', content=b'forbidden', headers=guest_headers).status_code == 404
        assert owner.put(f'/api/projects/{pid}/members', json={'email':'guest@example.com','role':'viewer'}, headers=headers).status_code == 200
        assert guest.get('/api/projects').json()[0]['role'] == 'viewer'
        assert guest.post(f'/api/projects/{pid}/recordings', json={'title':'Blocked','filename':'x.wav'}, headers=guest_headers).status_code == 403
        assert guest.post(f'/api/recordings/{rid}/corrections', json={}, headers=guest_headers).status_code == 403
        assert guest.get(f'/api/projects/{pid}/members').status_code == 403
        assert owner.put(f'/api/projects/{pid}/members', json={'email':'guest@example.com','role':'editor'}, headers=headers).status_code == 200
        assert guest.put(f'/api/recordings/{rid}/source', content=b'authorized', headers=guest_headers).status_code == 200
        assert owner.request('DELETE', f'/api/projects/{pid}/members', json={'email':'owner@example.com'}, headers=headers).status_code == 409
    finally:
        guest.close()


def test_admin_boundaries_suspension_reset_and_last_admin(team):
    app, owner, manager, uid = team
    guest, guest_headers = invited(app, manager, uid, 'guest@example.com')
    try:
        admin = login(owner, audience='admin')
        users = owner.get('/api/admin/users', headers=admin).json()
        gid = next(user['id'] for user in users if user['email']=='guest@example.com')
        assert all('password' not in user for user in users)
        assert owner.patch(f'/api/admin/users/{uid}', json={'admin':False,'disabled':False}, headers=admin).status_code == 409
        assert owner.patch(f'/api/admin/users/{gid}', json={'admin':False,'disabled':True}, headers=admin).status_code == 200
        assert guest.get('/api/projects').status_code == 401
        assert owner.patch(f'/api/admin/users/{gid}', json={'admin':False,'disabled':False}, headers=admin).status_code == 200
        token = owner.post(f'/api/admin/users/{gid}/reset', headers=admin).json()['token']
        assert guest.post('/api/auth/reset-password', json={'token':token,'password':PASSWORD}, headers=session(guest)).status_code == 200
        assert guest.post('/api/auth/reset-password', json={'token':token,'password':PASSWORD}, headers=session(guest)).status_code == 400
        gh = login(guest, 'guest@example.com')
        assert guest.post('/api/auth/logout-all', headers=gh).status_code == 200
        assert guest.get('/api/projects').status_code == 401
    finally:
        guest.close()


def test_legacy_adoption_is_explicit_and_never_falls_back(tmp_path):
    store = Store(tmp_path)
    with store.db() as db:
        db.execute("INSERT INTO projects VALUES('old','Existing data',0)")
    accounts = Accounts(store, 'accounts')
    with pytest.raises(ValueError, match='adopt-existing'):
        accounts.bootstrap('owner@example.com','Owner',PASSWORD)
    uid = accounts.bootstrap('owner@example.com','Owner',PASSWORD,adopt=True)
    assert accounts.role(uid,'old') == 'owner'
    with pytest.raises(ValueError, match='fallback'):
        Accounts(store,'local')
    with pytest.raises(ValueError, match='bootstrap'):
        accounts.bootstrap('another@example.com','Other',PASSWORD)


def test_expiry_rate_limit_origin_and_privilege_injection(team):
    app, owner, manager, uid = team
    headers = login(owner)
    assert owner.post('/api/projects',json={'name':'X'},headers={**headers,'Origin':'https://evil.example'}).status_code == 403
    assert owner.post('/api/projects',json={'name':'X'},headers={'X-CSRF-Token':'wrong'}).status_code == 403
    assert owner.post('/api/auth/accept-invitation',json={'token':'x'*32,'name':'Admin','password':PASSWORD,'admin':True},headers=headers).status_code == 422
    with manager.store.db() as db:
        db.execute('UPDATE sessions SET expires=?', (time.time()-1,))
    assert owner.get('/api/projects').status_code == 401
    for _ in range(8):
        response = owner.post('/api/auth/login',json={'email':'unknown@example.com','password':'wrong'},headers=session(owner))
        assert response.status_code == 401
    assert owner.post('/api/auth/login',json={'email':'unknown@example.com','password':'wrong'},headers=session(owner)).status_code == 429


def test_accounts_persist_and_unknown_schema_fails_closed(team):
    app, owner, manager, uid = team
    login(owner)
    token = owner.cookies.get(cookie('app'))
    restored = Accounts(Store(manager.store.root), 'accounts')
    assert restored.session(token)['id'] == uid
    with manager.store.db() as db:
        db.execute("UPDATE account_settings SET value='999' WHERE key='schema'")
    with pytest.raises(ValueError, match='schema version'):
        Accounts(Store(manager.store.root), 'accounts')


def test_oidc_issuer_exactness_and_secret_file_permissions(tmp_path):
    import json
    folder = tmp_path / 'auth'
    folder.mkdir()
    path = folder / 'oidc.json'
    path.write_text(json.dumps(dict(issuer='https://identity.example/',client_id='test',client_secret='synthetic-only')))
    path.chmod(0o600)
    manager = Accounts(Store(tmp_path),'accounts','https://recordings.example')
    assert manager.oidc_config()['issuer'] == 'https://identity.example/'
    path.chmod(0o644)
    with pytest.raises(ValueError, match='0600'):
        manager.oidc_config()


def test_account_request_budget_includes_unknown_length_bodies(team):
    app, owner, manager, uid = team
    headers = session(owner)
    assert owner.post('/api/auth/login', content=b'{' + b' ' * 70000, headers={**headers,'Content-Type':'application/json'}).status_code == 413
    def chunks():
        for _ in range(70):
            yield b' ' * 1024
    assert owner.post('/api/auth/login', content=chunks(), headers={**headers,'Content-Type':'application/json'}).status_code == 413


def test_instance_admin_does_not_inherit_project_content(team):
    app, owner, manager, uid = team
    owner_headers = login(owner)
    pid = owner.post('/api/projects',json={'name':'Owner content'},headers=owner_headers).json()['id']
    guest, _ = invited(app,manager,uid,'another-admin@example.com')
    try:
        admin_headers = login(owner,audience='admin')
        users = owner.get('/api/admin/users',headers=admin_headers).json()
        gid = next(user['id'] for user in users if user['email']=='another-admin@example.com')
        assert owner.patch(f'/api/admin/users/{gid}',json={'admin':True,'disabled':False},headers=admin_headers).status_code == 200
        login(guest,'another-admin@example.com')
        assert guest.get('/api/projects').json() == []
        assert guest.get(f'/api/projects/{pid}/recordings').status_code == 404
        gh = login(guest,'another-admin@example.com',audience='admin')
        assert guest.get('/api/admin/users',headers=gh).status_code == 200
    finally:
        guest.close()
