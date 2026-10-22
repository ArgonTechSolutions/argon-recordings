"""Second-factor bypass, signature/origin, single-use recovery and migration checks."""
import time

import pytest
from fastapi.testclient import TestClient
from argon_recordings.accounts import Accounts, cookie, digest
from argon_recordings.api import create_app, Settings
from argon_recordings.passkeys import enrollment_ticket
from argon_recordings.store import Store
from passkey_fixture import Authenticator, enroll, headers
from test_accounts import PASSWORD, team  # noqa: F401


def password_login(client):
    h = headers(client)
    result = client.post('/api/auth/login', json={'email': 'owner@example.com', 'password': PASSWORD, 'audience': 'admin'}, headers=h)
    assert result.status_code == 200
    return headers(client)


def test_password_only_admin_session_cannot_read_or_write_admin_api(team):
    app, client, manager, uid = team
    h = password_login(client)
    assert client.get('/api/admin/users', headers=h).status_code == 403
    assert client.patch('/api/admin/settings', json={'registration': 'closed'}, headers=h).status_code == 403
    assert client.get('/api/auth/session?audience=admin').json()['mfa_verified'] is False
    assert client.post('/api/auth/mfa/register/options', json={'code': 'invented'}, headers=h).status_code == 403
    assert client.post('/api/auth/mfa/recovery-codes', headers=h).status_code == 401
    assert client.post('/api/auth/mfa/authenticate/options', headers=h).status_code == 409


def test_registration_signature_uv_origin_rotation_and_replay(team):
    app, client, manager, uid = team
    h = password_login(client)
    code = enrollment_ticket(manager, 'owner@example.com')
    key = Authenticator()
    old = client.cookies.get(cookie('admin'))
    def options():
        result = client.post('/api/auth/mfa/register/options', json={'code': code}, headers=headers(client))
        assert result.status_code == 200
        return result.json()
    invalid = key.registration(options(), origin='https://attacker.example')
    assert client.post('/api/auth/mfa/register/verify', json={'credential': invalid}, headers=h).status_code == 400
    invalid = key.registration(options(), uv=False)
    assert client.post('/api/auth/mfa/register/verify', json={'credential': invalid}, headers=h).status_code == 400
    good = key.registration(options())
    result = client.post('/api/auth/mfa/register/verify', json={'credential': good}, headers=h)
    assert result.status_code == 200, result.text
    assert len(result.json()['recovery_codes']) == 8
    assert manager.session(old, 'admin') is None
    assert client.get('/api/admin/users').status_code == 200
    assert client.post('/api/auth/mfa/register/verify', json={'credential': good}, headers=headers(client)).status_code == 400
    with manager.store.db() as db:
        assert not db.execute('SELECT 1 FROM mfa_enrollments WHERE user_id=?', (uid,)).fetchone()
        stored = {r[0] for r in db.execute('SELECT digest FROM mfa_recovery')}
        assert not stored.intersection(result.json()['recovery_codes'])


def test_authenticate_uses_real_signature_and_one_time_challenge(team):
    app, client, manager, uid = team
    password_login(client)
    key, _ = enroll(client, manager)
    h = password_login(client)
    opts = client.post('/api/auth/mfa/authenticate/options', headers=h).json()
    good = key.authentication(opts)
    bad = {**good, 'response': {**good['response'], 'signature': 'AAAA'}}
    assert client.post('/api/auth/mfa/authenticate/verify', json={'credential': bad}, headers=h).status_code == 400
    assert client.post('/api/auth/mfa/authenticate/verify', json={'credential': good}, headers=h).status_code == 400
    opts = client.post('/api/auth/mfa/authenticate/options', headers=h).json()
    good = key.authentication(opts)
    assert client.post('/api/auth/mfa/authenticate/verify', json={'credential': good}, headers=h).status_code == 200
    assert client.get('/api/admin/users').status_code == 200
    assert client.delete('/api/auth/mfa/keys/' + good['id'], headers=headers(client)).status_code == 409


def test_recovery_single_use_and_operator_revocation(team):
    app, client, manager, uid = team
    password_login(client)
    key, codes = enroll(client, manager)
    h = password_login(client)
    assert client.post('/api/auth/mfa/recovery', json={'code': codes[0]}, headers=h).status_code == 200
    assert client.get('/api/admin/users').status_code == 200
    assert client.post('/api/auth/mfa/recovery-codes', headers=headers(client)).status_code == 403
    h = password_login(client)
    assert client.post('/api/auth/mfa/recovery', json={'code': codes[0]}, headers=h).status_code == 400
    assert client.post('/api/auth/mfa/recovery', json={'code': codes[1]}, headers=h).status_code == 200
    enrollment_ticket(manager, 'owner@example.com', reset=True)
    assert client.get('/api/admin/users').status_code == 401
    with manager.store.db() as db:
        assert not db.execute('SELECT 1 FROM passkeys').fetchone()
        assert not db.execute('SELECT 1 FROM mfa_recovery').fetchone()
    h = password_login(client)
    assert client.post('/api/auth/mfa/recovery', json={'code': codes[2]}, headers=h).status_code == 400


def test_challenge_expiry_browser_binding_and_csrf(team):
    app, client, manager, uid = team
    h = password_login(client)
    code = enrollment_ticket(manager, 'owner@example.com')
    opts = client.post('/api/auth/mfa/register/options', json={'code': code}, headers=h).json()
    credential = Authenticator().registration(opts)
    with manager.store.db() as db:
        db.execute('UPDATE mfa_challenges SET expires=?', (time.time() - 1,))
    assert client.post('/api/auth/mfa/register/verify', json={'credential': credential}, headers=h).status_code == 400
    assert client.post('/api/auth/mfa/register/options', json={'code': code}, headers={'X-Session-Audience': 'admin'}).status_code == 403
    opts = client.post('/api/auth/mfa/register/options', json={'code': code}, headers=h).json()
    other = TestClient(app, base_url='http://localhost:8787')
    oh = password_login(other)
    assert other.post('/api/auth/mfa/register/verify', json={'credential': Authenticator().registration(opts)}, headers=oh).status_code == 400
    assert client.get('/api/admin/users', headers=h).status_code == 403


def test_old_account_schema_upgrades_without_authorizing_old_admin(tmp_path):
    store = Store(tmp_path)
    manager = Accounts(store, 'accounts')
    uid = manager.bootstrap('owner@example.com', 'Owner', PASSWORD)
    app_token, admin_token = manager.new_session(uid, 'app'), manager.new_session(uid, 'admin')
    with store.db() as db:
        db.execute('ALTER TABLE sessions DROP COLUMN mfa_method')
        db.execute('ALTER TABLE sessions DROP COLUMN mfa_at')
        db.execute("UPDATE account_settings SET value='1' WHERE key='schema'")
    upgraded = Accounts(store, 'accounts')
    assert upgraded.session(app_token)['id'] == uid
    assert upgraded.session(admin_token, 'admin')['mfa_at'] is None
    with store.db() as db:
        assert db.execute("SELECT value FROM account_settings WHERE key='schema'").fetchone()[0] == '2'


def add_key(client):
    key = Authenticator()
    opts = client.post('/api/auth/mfa/register/options', json={}, headers=headers(client))
    assert opts.status_code == 200, opts.text
    result = client.post('/api/auth/mfa/register/verify', json={'credential': key.registration(opts.json())}, headers=headers(client))
    assert result.status_code == 200, result.text
    return key, result.json()


def test_recovery_replacement_rotates_codes_but_cannot_remove_keys(team):
    app, client, manager, uid = team
    password_login(client)
    first, codes = enroll(client, manager)
    second, _ = add_key(client)
    h = password_login(client)
    assert client.post('/api/auth/mfa/recovery', json={'code': codes[0]}, headers=h).status_code == 200
    from webauthn.helpers import bytes_to_base64url
    assert client.delete('/api/auth/mfa/keys/' + bytes_to_base64url(first.id), headers=headers(client)).status_code == 403
    third, result = add_key(client)
    assert len(result['recovery_codes']) == 8
    h = password_login(client)
    assert client.post('/api/auth/mfa/recovery', json={'code': codes[1]}, headers=h).status_code == 400
    assert client.post('/api/auth/mfa/recovery', json={'code': result['recovery_codes'][0]}, headers=h).status_code == 200


@pytest.mark.parametrize('operation', ['codes', 'delete', 'register'])
def test_revoked_session_cannot_race_security_changes(team, monkeypatch, operation):
    app, client, manager, uid = team
    password_login(client)
    first, codes = enroll(client, manager)
    second, _ = add_key(client)
    from webauthn.helpers import bytes_to_base64url
    path = {'codes': '/api/auth/mfa/recovery-codes', 'delete': '/api/auth/mfa/keys/' + bytes_to_base64url(first.id),
            'register': '/api/auth/mfa/register/verify'}[operation]
    body = None
    if operation == 'register':
        opts = client.post('/api/auth/mfa/register/options', json={}, headers=headers(client)).json()
        body = {'credential': Authenticator().registration(opts)}
    h = headers(client)
    authorize = manager.authorize
    def revoke_after_authorize(request):
        authorize(request)
        if request.url.path == path:
            with manager.store.db() as db:
                db.execute('DELETE FROM sessions WHERE user_id=?', (uid,))
    monkeypatch.setattr(manager, 'authorize', revoke_after_authorize)
    result = client.request('DELETE' if operation == 'delete' else 'POST', path, json=body, headers=h)
    assert result.status_code == 401, result.text
    with manager.store.db() as db:
        assert db.execute('SELECT count(*) FROM passkeys').fetchone()[0] == 2
        assert db.execute('SELECT count(*) FROM mfa_recovery').fetchone()[0] == 8


@pytest.mark.parametrize('host', ['localhost/api/worker', 'localhost?ignored=', 'localhost#ignored',
                                  'localhost:8787@evil.example', 'localhost:invalid', 'localhost\\api\\worker'])
def test_malformed_host_cannot_bypass_admin_path_authorization(team, host):
    app, client, manager, uid = team
    result = client.get('/api/admin/users', headers={'Host': host})
    assert result.status_code == 403
    assert result.json()['detail'] == 'Host is not allowed'


def test_duplicate_host_is_rejected_before_authentication(team):
    app, client, manager, uid = team
    result = client.get('/api/admin/users', headers=[('Host', 'localhost'), ('Host', 'localhost/api/worker')])
    assert result.status_code == 403
