"""Optional TOTP: enrollment authority, encryption, replay and revocation."""
import stat
import time

import pyotp
import pytest
from fastapi.testclient import TestClient
from argon_recordings.accounts import Accounts, cookie
from argon_recordings.passkeys import enrollment_ticket
from argon_recordings.store import Store
from passkey_fixture import enroll, headers
from test_accounts import team, PASSWORD  # noqa: F401
from test_passkeys import password_login, add_key


def begin(client, manager):
    manager.allow_totp = True
    code = enrollment_ticket(manager, 'owner@example.com')
    response = client.post('/api/auth/mfa/totp/setup', json={'code': code}, headers=headers(client))
    assert response.status_code == 200, response.text
    return response.json()


def confirm(client, setup, at=None):
    at = time.time() if at is None else at
    return client.post('/api/auth/mfa/totp/confirm', json={'code': pyotp.TOTP(setup['secret']).at(at)}, headers=headers(client))


def test_opt_in_and_terminal_authority(team):
    app, client, manager, uid = team
    password_login(client)
    assert client.get('/api/auth/mfa', headers=headers(client)).json()['totp_allowed'] is False
    assert client.post('/api/auth/mfa/totp/setup', json={}, headers=headers(client)).status_code == 404
    manager.allow_totp = True
    assert client.post('/api/auth/mfa/totp/setup', json={}, headers=headers(client)).status_code == 403
    assert client.get('/api/admin/users').status_code == 403
    setup = begin(client, manager)
    assert setup['qr'].startswith('data:image/png;base64,')
    assert client.get('/api/admin/users').status_code == 403
    result = confirm(client, setup)
    assert result.status_code == 200, result.text
    assert len(result.json()['recovery_codes']) == 8
    assert client.get('/api/admin/users').status_code == 200


def test_encryption_backup_restore_and_immediate_replay(team, monkeypatch):
    app, client, manager, uid = team
    password_login(client)
    setup = begin(client, manager)
    old = client.cookies.get(cookie('admin'))
    now = time.time()
    assert confirm(client, setup, now).status_code == 200
    assert manager.session(old, 'admin') is None
    key = manager.store.root / 'auth/totp.key'
    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    with manager.store.db() as db:
        encrypted, previous = db.execute('SELECT secret,last_counter FROM totp_factors').fetchone()
        assert setup['secret'].encode() not in encrypted
        assert not db.execute('SELECT 1 FROM totp_pending').fetchone()
    h = password_login(client)
    code = pyotp.TOTP(setup['secret']).at(now)
    assert client.post('/api/auth/mfa/totp/verify', json={'code': code}, headers=h).status_code == 400
    future = (previous + 1) * 30 + 1
    monkeypatch.setattr('argon_recordings.totp.time.time', lambda: future)
    result = client.post('/api/auth/mfa/totp/verify', json={'code': pyotp.TOTP(setup['secret']).at(future)}, headers=h)
    assert result.status_code == 200, result.text
    restored = Accounts(Store(manager.store.root), 'accounts', allow_totp=True)
    assert restored.session(client.cookies.get(cookie('admin')), 'admin')['mfa_method'] == 'totp'
    disabled_token = client.cookies.get(cookie('admin'))
    disabled = Accounts(Store(manager.store.root), 'accounts', allow_totp=False)
    assert disabled.session(disabled_token, 'admin') is None
    assert Accounts(Store(manager.store.root), 'accounts', allow_totp=True).session(disabled_token, 'admin') is None
    manager.allow_totp = False
    assert client.get('/api/admin/users').status_code == 401
    manager.allow_totp = True
    h = password_login(client)
    original = key.read_bytes()
    key.unlink()
    result = client.post('/api/auth/mfa/totp/verify', json={'code': '123456'}, headers=h)
    assert result.status_code == 503
    assert not key.exists()
    key.write_bytes(original); key.chmod(0o600)
    key.chmod(0o644)
    assert client.post('/api/auth/mfa/totp/verify', json={'code': '123456'}, headers=h).status_code == 503


def test_setup_binding_expiry_cancel_and_rate_limits(team):
    app, client, manager, uid = team
    password_login(client)
    setup = begin(client, manager)
    other = TestClient(app, base_url='http://localhost:8787')
    password_login(other)
    assert confirm(other, setup).status_code == 400
    assert client.delete('/api/auth/mfa/totp/setup', headers=headers(client)).status_code == 200
    assert confirm(client, setup).status_code == 400
    setup = begin(client, manager)
    with manager.store.db() as db:
        db.execute('UPDATE totp_pending SET expires=?', (time.time() - 1,))
    assert confirm(client, setup).status_code == 400
    for _ in range(5):
        client.post('/api/auth/mfa/totp/verify', json={'code': '000000'}, headers=headers(client))
    assert client.post('/api/auth/mfa/totp/verify', json={'code': '000000'}, headers=headers(client)).status_code == 429


def test_totp_only_recovery_and_last_factor_protection(team):
    app, client, manager, uid = team
    password_login(client)
    setup = begin(client, manager)
    codes = confirm(client, setup).json()['recovery_codes']
    assert client.delete('/api/auth/mfa/totp', headers=headers(client)).status_code == 409
    assert client.post('/api/auth/mfa/recovery-codes', headers=headers(client)).status_code == 200
    # Get current recovery codes because replacement invalidated the first batch.
    codes = client.post('/api/auth/mfa/recovery-codes', headers=headers(client)).json()['codes']
    h = password_login(client)
    assert client.post('/api/auth/mfa/recovery', json={'code': codes[0]}, headers=h).status_code == 200
    assert client.post('/api/auth/mfa/recovery-codes', headers=headers(client)).status_code == 403
    key, result = add_key(client)
    assert result['recovery_codes']
    assert client.delete('/api/auth/mfa/totp', headers=headers(client)).status_code == 200
    assert client.get('/api/admin/users').status_code == 401


def test_passkey_verified_session_can_enroll_totp_without_host_ticket(team):
    app, client, manager, uid = team
    password_login(client)
    enroll(client, manager)
    manager.allow_totp = True
    result = client.post('/api/auth/mfa/totp/setup', json={}, headers=headers(client))
    assert result.status_code == 200, result.text
    assert confirm(client, result.json()).status_code == 200
    # A new setup cannot replace an already-enrolled secret.
    assert client.post('/api/auth/mfa/totp/setup', json={}, headers=headers(client)).status_code == 409
    token = client.cookies.get(cookie('admin'))
    enrollment_ticket(manager, 'owner@example.com', reset=True)
    assert manager.session(token, 'admin') is None
    with manager.store.db() as db:
        assert not db.execute('SELECT 1 FROM totp_factors').fetchone()
        assert not db.execute('SELECT 1 FROM totp_pending').fetchone()


@pytest.mark.parametrize('operation', ['setup', 'confirm', 'verify', 'remove'])
def test_revocation_cannot_race_totp_writes(team, monkeypatch, operation):
    app, client, manager, uid = team
    password_login(client)
    enroll(client, manager)
    manager.allow_totp = True
    setup = client.post('/api/auth/mfa/totp/setup', json={}, headers=headers(client)).json()
    if operation in {'verify', 'remove'}:
        assert confirm(client, setup).status_code == 200
    path = '/api/auth/mfa/totp' if operation == 'remove' else '/api/auth/mfa/totp/' + operation
    h = headers(client)
    original = manager.authorize
    def revoke(request):
        original(request)
        if request.scope['path'] == path:
            with manager.store.db() as db:
                db.execute('DELETE FROM sessions WHERE user_id=?', (uid,))
    monkeypatch.setattr(manager, 'authorize', revoke)
    body = {} if operation == 'setup' else {'code': pyotp.TOTP(setup['secret']).now()}
    result = client.request('DELETE' if operation == 'remove' else 'POST', path, json=body, headers=h)
    assert result.status_code == 401, result.text


def test_stale_mfa_requires_reverification_without_revoking_live_session(team):
    app, client, manager, uid = team
    password_login(client)
    key, _ = enroll(client, manager)
    manager.allow_totp = True
    old = client.cookies.get(cookie('admin'))
    with manager.store.db() as db:
        db.execute("UPDATE sessions SET mfa_at=? WHERE user_id=?", (time.time() - 901, uid))
    result = client.post('/api/auth/mfa/totp/setup', json={}, headers=headers(client))
    assert result.status_code == 403
    assert result.json()['detail']['code'] == 'reauthentication_required'
    assert manager.session(old, 'admin')
    with manager.store.db() as db:
        assert not db.execute('SELECT 1 FROM totp_pending').fetchone()
    options = client.post('/api/auth/mfa/authenticate/options', headers=headers(client)).json()
    result = client.post('/api/auth/mfa/authenticate/verify', json={'credential': key.authentication(options)}, headers=headers(client))
    assert result.status_code == 200
    assert manager.session(old, 'admin') is None
    assert client.post('/api/auth/mfa/totp/setup', json={}, headers=headers(client)).status_code == 200
