"""Account/admin API. OIDC protocol and token verification are delegated to Authlib."""
import hmac
import json
import secrets
import sqlite3
import time
from typing import Literal

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import Field

from .accounts import cookie, csrf, digest, email
from .contracts import Strict


class Login(Strict):
    email: str = Field(max_length=254)
    password: str = Field(max_length=128)
    audience: Literal['app', 'admin'] = 'app'


class Invitation(Strict):
    email: str = Field(max_length=254)


class Accept(Strict):
    token: str = Field(min_length=20, max_length=200)
    name: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=15, max_length=128)


class Reset(Strict):
    token: str = Field(min_length=20, max_length=200)
    password: str = Field(min_length=15, max_length=128)


class Password(Strict):
    current: str = Field(max_length=128)
    password: str = Field(min_length=15, max_length=128)


class UserUpdate(Strict):
    admin: bool
    disabled: bool


class Policy(Strict):
    registration: Literal['closed', 'invite']


class Member(Strict):
    email: str = Field(max_length=254)
    role: Literal['owner', 'editor', 'viewer']


class OIDCStart(Strict):
    audience: Literal['app', 'admin'] = 'app'
    invitation: str | None = Field(default=None, max_length=200)
    link: bool = False


def create_oidc_client(config):
    from authlib.integrations.starlette_client import OAuth
    return OAuth().register('oidc', client_id=config['client_id'], client_secret=config['client_secret'],
                            server_metadata_url=config['issuer'].rstrip('/') + '/.well-known/openid-configuration',
                            client_kwargs={'scope': 'openid email profile', 'code_challenge_method': 'S256',
                                           'timeout': 10, 'trust_env': False})


def verified_metadata(metadata, issuer):
    from urllib.parse import urlsplit
    if metadata.get('issuer') != issuer:
        return False
    for field in ['authorization_endpoint', 'token_endpoint', 'jwks_uri']:
        endpoint = urlsplit(metadata.get(field, ''))
        if endpoint.scheme != 'https' or not endpoint.netloc or endpoint.username or endpoint.password or endpoint.fragment:
            return False
    return True


def install(app, get_accounts):
    def auth():
        manager = get_accounts()
        if not manager.enabled:
            raise HTTPException(404, 'Accounts are not enabled')
        return manager

    def preflight(request):
        auth()
        value = request.cookies.get('recordings_login', '')
        audience = request.headers.get('x-session-audience', 'app')
        session_token = request.cookies.get(cookie(audience), '')
        valid_session = get_accounts().session(session_token, audience)
        valid = hmac.compare_digest(request.headers.get('x-csrf-token', ''), csrf(value)) if value else False
        if not valid and not (valid_session and hmac.compare_digest(request.headers.get('x-csrf-token', ''), csrf(session_token))):
            raise HTTPException(403, 'Refresh the sign-in page before trying again')

    def signed_in(manager, uid, audience):
        token = manager.new_session(uid, audience)
        response = JSONResponse({'ok': True})
        response.set_cookie(cookie(audience), token, httponly=True, secure=manager.secure, samesite='lax',
                            max_age=3600 if audience == 'admin' else 43200, path='/')
        response.headers['Cache-Control'] = 'no-store'
        return response

    from .passkeys import install as install_passkeys
    install_passkeys(app, get_accounts)

    @app.get('/api/auth/session')
    def session(request: Request, audience: Literal['app', 'admin'] = 'app'):
        manager = get_accounts()
        if not manager.enabled:
            return {'enabled': False, 'user': None, 'csrf': None, 'oidc': False}
        token = request.cookies.get(cookie(audience), '')
        user = manager.session(token, audience)
        login_token = request.cookies.get('recordings_login') or secrets.token_urlsafe(32)
        response = JSONResponse({'enabled': True, 'user': user, 'csrf': csrf(token if user else login_token),
                                 'oidc': manager.oidc_config() is not None, 'registration': manager.policy(),
                                 'mfa_verified': bool(user and user['mfa_at'])})
        response.set_cookie('recordings_login', login_token, httponly=True, secure=manager.secure, samesite='lax', max_age=3600)
        response.headers['Cache-Control'] = 'no-store'
        return response

    @app.post('/api/auth/login')
    def login(body: Login, request: Request):
        preflight(request)
        manager = auth()
        address = email(body.email)
        manager.limit('login-email:' + address)
        manager.limit('login-ip:' + (request.client.host if request.client else ''), maximum=100)
        with manager.store.db() as db:
            user = db.execute('SELECT * FROM users WHERE email=?', (address,)).fetchone()
        valid = manager.verify(user['password'] if user else None, body.password)
        if not valid or not user or user['disabled']:
            raise HTTPException(401, 'Email or password is incorrect')
        # Rotate the session and revoke the replaced cookie.
        with manager.store.db() as db:
            db.execute('DELETE FROM sessions WHERE digest=?', (digest(request.cookies.get(cookie(body.audience), '')),))
        return signed_in(manager, user['id'], body.audience)

    @app.post('/api/auth/accept-invitation')
    def accept(body: Accept, request: Request):
        preflight(request)
        manager = auth()
        manager.limit('accept:' + (request.client.host if request.client else ''), maximum=20)
        uid = manager.accept(body.token, body.name.strip(), body.password)
        return signed_in(manager, uid, 'app')

    @app.post('/api/auth/reset-password')
    def reset(body: Reset, request: Request):
        preflight(request)
        manager = auth()
        manager.limit('reset:' + (request.client.host if request.client else ''), maximum=20)
        hashed = manager.hash_password(body.password)
        with manager.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT r.user_id FROM password_resets r JOIN users u ON u.id=r.user_id WHERE r.digest=? AND r.expires>? AND u.disabled=0',
                             (digest(body.token), time.time())).fetchone()
            if not row:
                raise HTTPException(400, 'Reset link is invalid or expired')
            db.execute('UPDATE users SET password=? WHERE id=?', (hashed, row[0]))
            db.execute('DELETE FROM password_resets WHERE user_id=?', (row[0],))
            db.execute('DELETE FROM sessions WHERE user_id=?', (row[0],))
            manager.audit(db, row[0], 'password-reset')
        return {'ok': True}

    @app.post('/api/auth/logout')
    def logout(request: Request):
        manager = auth()
        audience = request.headers.get('x-session-audience', 'app')
        with manager.store.db() as db:
            db.execute('DELETE FROM sessions WHERE digest=?', (digest(request.cookies.get(cookie(audience), '')),))
        response = JSONResponse({'ok': True})
        response.delete_cookie(cookie(audience), path='/')
        return response

    @app.post('/api/auth/logout-all')
    def logout_all(request: Request):
        manager = auth()
        with manager.store.db() as db:
            db.execute('DELETE FROM sessions WHERE user_id=?', (request.state.account['id'],))
            manager.audit(db, request.state.account['id'], 'logout-all')
        return {'ok': True}

    @app.post('/api/auth/password')
    def password(body: Password, request: Request):
        manager, uid = auth(), request.state.account['id']
        with manager.store.db() as db:
            user = db.execute('SELECT password FROM users WHERE id=?', (uid,)).fetchone()
        if not manager.verify(user[0], body.current):
            raise HTTPException(401, 'Current password is incorrect')
        hashed = manager.hash_password(body.password)
        with manager.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('UPDATE users SET password=? WHERE id=? AND password=? AND disabled=0', (hashed, uid, user[0])).rowcount != 1:
                raise HTTPException(409, 'Account changed; sign in again')
            db.execute('DELETE FROM sessions WHERE user_id=?', (uid,))
            manager.audit(db, uid, 'password-change')
        return {'ok': True}

    @app.get('/api/admin/users')
    def users():
        with auth().store.db() as db:
            return [dict(row) for row in db.execute('SELECT id,email,name,admin,disabled,created FROM users ORDER BY created')]

    @app.patch('/api/admin/users/{uid}')
    def update_user(uid: str, body: UserUpdate, request: Request):
        manager = auth()
        with manager.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if not db.execute('SELECT 1 FROM users WHERE id=?', (uid,)).fetchone():
                raise HTTPException(404, 'Account not found')
            if not body.admin or body.disabled:
                if not db.execute('SELECT 1 FROM users WHERE admin=1 AND disabled=0 AND id!=?', (uid,)).fetchone():
                    raise HTTPException(409, 'Keep at least one active administrator')
            if body.disabled and db.execute("SELECT 1 FROM memberships m WHERE m.user_id=? AND m.role='owner' AND NOT EXISTS "
                "(SELECT 1 FROM memberships n JOIN users u ON u.id=n.user_id WHERE n.project_id=m.project_id AND n.user_id!=? AND n.role='owner' AND u.disabled=0)", (uid, uid)).fetchone():
                raise HTTPException(409, 'Transfer sole project ownership before suspending this account')
            db.execute('UPDATE users SET admin=?,disabled=? WHERE id=?', (body.admin, body.disabled, uid))
            db.execute('DELETE FROM sessions WHERE user_id=?', (uid,))
            manager.audit(db, request.state.account['id'], 'account-update', uid)
        return {'ok': True}

    @app.post('/api/admin/users/{uid}/reset')
    def create_reset(uid: str, request: Request):
        manager, token = auth(), secrets.token_urlsafe(32)
        with manager.store.db() as db:
            if not db.execute('SELECT 1 FROM users WHERE id=? AND disabled=0', (uid,)).fetchone():
                raise HTTPException(404, 'Active account not found')
            db.execute('DELETE FROM password_resets WHERE user_id=? OR expires<?', (uid, time.time()))
            db.execute('INSERT INTO password_resets VALUES(?,?,?)', (digest(token), uid, time.time() + 1800))
            manager.audit(db, request.state.account['id'], 'reset-issued', uid)
        return {'token': token, 'expires_in': 1800}

    @app.post('/api/admin/invitations', status_code=201)
    def invitation(body: Invitation, request: Request):
        token = auth().invite(request.state.account['id'], body.email)
        return {'token': token, 'expires_in': 86400}

    @app.get('/api/admin/invitations')
    def invitations():
        with auth().store.db() as db:
            return [dict(row) for row in db.execute('SELECT email,expires,used FROM invitations WHERE expires>? ORDER BY expires DESC', (time.time(),))]

    @app.delete('/api/admin/invitations')
    def revoke_invitation(body: Invitation, request: Request):
        manager = auth()
        with manager.store.db() as db:
            db.execute('DELETE FROM invitations WHERE email=?', (email(body.email),))
            manager.audit(db, request.state.account['id'], 'invite-revoked', email(body.email))
        return {'ok': True}

    @app.get('/api/admin/settings')
    def settings():
        manager = auth()
        config = manager.oidc_config()
        return {'registration': manager.policy(), 'oidc': {'issuer': config['issuer']} if config else None}

    @app.patch('/api/admin/settings')
    def policy(body: Policy, request: Request):
        manager = auth()
        with manager.store.db() as db:
            db.execute("UPDATE account_settings SET value=? WHERE key='registration'", (body.registration,))
            manager.audit(db, request.state.account['id'], 'registration-policy', body.registration)
        return {'ok': True}

    @app.get('/api/admin/audit')
    def audit():
        with auth().store.db() as db:
            return [dict(row) for row in db.execute('SELECT * FROM account_audit ORDER BY id DESC LIMIT 100')]

    @app.get('/api/projects/{pid}/members')
    def members(pid: str):
        with auth().store.db() as db:
            return [dict(row) for row in db.execute('SELECT u.email,u.name,u.disabled,m.role FROM memberships m JOIN users u ON u.id=m.user_id WHERE m.project_id=? ORDER BY u.email', (pid,))]

    @app.put('/api/projects/{pid}/members')
    def member(pid: str, body: Member, request: Request):
        return change_member(pid, body.email, body.role, request)

    @app.delete('/api/projects/{pid}/members')
    def remove_member(pid: str, body: Invitation, request: Request):
        return change_member(pid, body.email, None, request)

    def change_member(pid, address, role, request):
        manager = auth()
        with manager.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            # Recheck owner inside the write transaction, not just at request entry.
            if not db.execute("SELECT 1 FROM memberships WHERE project_id=? AND user_id=? AND role='owner'", (pid, request.state.account['id'])).fetchone():
                raise HTTPException(403, 'Owner access required')
            user = db.execute('SELECT id FROM users WHERE email=? AND disabled=0', (email(address),)).fetchone()
            if not user:
                raise HTTPException(404, 'Active account not found')
            uid = user[0]
            previous = db.execute('SELECT role FROM memberships WHERE project_id=? AND user_id=?', (pid, uid)).fetchone()
            if previous and previous[0] == 'owner' and role != 'owner' and not db.execute("SELECT 1 FROM memberships m JOIN users u ON u.id=m.user_id WHERE m.project_id=? AND m.user_id!=? AND m.role='owner' AND u.disabled=0", (pid, uid)).fetchone():
                raise HTTPException(409, 'Keep at least one active project owner')
            if role:
                db.execute('INSERT INTO memberships VALUES(?,?,?) ON CONFLICT(project_id,user_id) DO UPDATE SET role=excluded.role', (pid, uid, role))
            else:
                db.execute('DELETE FROM memberships WHERE project_id=? AND user_id=?', (pid, uid))
            manager.audit(db, request.state.account['id'], 'project-member', pid + ':' + uid + ':' + str(role))
        return {'ok': True}

    @app.post('/api/auth/oidc/start')
    async def oidc_start(body: OIDCStart, request: Request):
        manager = auth()
        if body.link:
            manager.authorize(request)
            if time.time() - request.state.account['created'] > 900:
                raise HTTPException(401, 'Sign in again before linking an identity')
        else:
            preflight(request)
        manager.limit('oidc:' + (request.client.host if request.client else ''), maximum=30)
        config = manager.oidc_config()
        if config is None:
            raise HTTPException(503, 'Single sign-on is not configured')
        client = create_oidc_client(config)
        metadata = await client.load_server_metadata()
        if not verified_metadata(metadata, config['issuer']):
            raise HTTPException(503, 'Identity provider metadata must match the issuer and use HTTPS')
        verifier, nonce, browser = (secrets.token_urlsafe(48) for _ in range(3))
        result = await client.create_authorization_url(manager.public_url + '/api/auth/oidc/callback', nonce=nonce, code_verifier=verifier)
        with manager.store.db() as db:
            db.execute('DELETE FROM oidc_flows WHERE expires<?', (time.time(),))
            db.execute('INSERT INTO oidc_flows VALUES(?,?,?,?,?,?,?,?)',
                       (digest(result['state']), digest(browser), nonce, verifier, body.audience, time.time() + 600,
                        digest(body.invitation) if body.invitation else None, request.state.account['id'] if body.link else None))
        response = JSONResponse({'url': result['url']})
        response.set_cookie('recordings_oidc', browser, httponly=True, secure=True, samesite='lax', max_age=600, path='/api/auth/oidc')
        return response

    @app.get('/api/auth/oidc/callback')
    async def oidc_callback(request: Request, state: str = '', code: str = ''):
        manager = auth()
        # Consume state atomically before network I/O; failed exchanges must start a new flow.
        with manager.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            flow = db.execute('SELECT * FROM oidc_flows WHERE digest=? AND expires>?', (digest(state), time.time())).fetchone()
            if not flow or not hmac.compare_digest(flow['browser'], digest(request.cookies.get('recordings_oidc', ''))):
                raise HTTPException(400, 'Sign-in expired or could not be verified')
            db.execute('DELETE FROM oidc_flows WHERE digest=?', (digest(state),))
        config = manager.oidc_config()
        if not config or not code:
            raise HTTPException(400, 'Sign-in could not be completed')
        try:
            client = create_oidc_client(config)
            metadata = await client.load_server_metadata()
            if not verified_metadata(metadata, config['issuer']):
                raise ValueError('invalid provider metadata')
            algorithms = [alg for alg in metadata.get('id_token_signing_alg_values_supported', ['RS256']) if alg in {'RS256', 'ES256', 'PS256'}]
            if not algorithms:
                raise ValueError('unsupported signing algorithm')
            metadata['id_token_signing_alg_values_supported'] = algorithms
            token = await client.fetch_access_token(redirect_uri=manager.public_url + '/api/auth/oidc/callback', code=code, code_verifier=flow['verifier'])
            claims = await client.parse_id_token(token, nonce=flow['nonce'], claims_options={'iss': {'essential': True, 'value': config['issuer']}, 'sub': {'essential': True}}, leeway=30)
            if not claims or not claims.get('sub'):
                raise ValueError('missing identity')
        except Exception as exc:
            raise HTTPException(400, 'Identity provider sign-in could not be verified') from exc
        try:
            with manager.store.db() as db:
                db.execute('BEGIN IMMEDIATE')
                existing = db.execute('SELECT user_id FROM external_identities WHERE issuer=? AND subject=?', (config['issuer'], claims['sub'])).fetchone()
                uid = existing[0] if existing else None
                if flow['link_user']:
                    current = manager.session(request.cookies.get(cookie(flow['audience']), ''), flow['audience'])
                    if not current or current['id'] != flow['link_user'] or time.time() - current['created'] > 900 or (uid and uid != current['id']):
                        raise HTTPException(403, 'Identity cannot be linked')
                    uid = current['id']
                elif not uid:
                    if not flow['invite']:
                        raise HTTPException(403, 'An invitation is required; existing accounts must link SSO while signed in')
                    address = manager.consume_invite(db, flow['invite'])
                    if claims.get('email_verified') is not True or email(claims.get('email', '')) != address:
                        raise HTTPException(403, 'Use the verified email on your invitation')
                    uid = secrets.token_hex(16)
                    db.execute('INSERT INTO users VALUES(?,?,?,NULL,0,0,?)', (uid, address, str(claims.get('name', address))[:100], time.time()))
                db.execute('INSERT OR IGNORE INTO external_identities VALUES(?,?,?)', (config['issuer'], claims['sub'], uid))
                manager.audit(db, uid, 'oidc-link' if flow['link_user'] else 'oidc-login')
        except sqlite3.IntegrityError as exc:
            raise HTTPException(409, 'That email already has an account; sign in and link SSO explicitly') from exc
        response = signed_in(manager, uid, flow['audience'])
        # Use a fixed local redirect rather than accepting arbitrary next URLs.
        redirect = RedirectResponse('/admin/' if flow['audience'] == 'admin' else '/', status_code=303)
        for value in response.raw_headers:
            if value[0].lower() == b'set-cookie':
                redirect.raw_headers.append(value)
        redirect.delete_cookie('recordings_oidc', path='/api/auth/oidc')
        return redirect
