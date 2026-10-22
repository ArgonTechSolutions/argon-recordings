"""Administrator MFA: preferred WebAuthn passkeys and optional authenticator apps."""
import json
import secrets
import sqlite3
import time
from urllib.parse import urlsplit

from fastapi import HTTPException, Request
from pydantic import Field
from webauthn import (generate_authentication_options, generate_registration_options,
                      options_to_json, verify_authentication_response, verify_registration_response)
from webauthn.helpers import bytes_to_base64url, base64url_to_bytes
from webauthn.helpers.structs import (AuthenticatorSelectionCriteria, PublicKeyCredentialDescriptor,
                                     ResidentKeyRequirement, UserVerificationRequirement)

from .accounts import cookie, digest
from .contracts import Strict
from . import totp

SCHEMA = '''
CREATE TABLE IF NOT EXISTS passkeys(id TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES users(id),
 rp TEXT NOT NULL,name TEXT NOT NULL,public_key BLOB NOT NULL,sign_count INTEGER NOT NULL,created REAL NOT NULL);
CREATE INDEX IF NOT EXISTS passkeys_user ON passkeys(user_id,rp);
CREATE TABLE IF NOT EXISTS mfa_challenges(session TEXT PRIMARY KEY,user_id TEXT NOT NULL,kind TEXT NOT NULL,
 challenge BLOB NOT NULL,origin TEXT NOT NULL,rp TEXT NOT NULL,ticket TEXT,expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS mfa_enrollments(user_id TEXT PRIMARY KEY,digest TEXT NOT NULL,rp TEXT NOT NULL,expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS mfa_recovery(digest TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES users(id));
''' + totp.SCHEMA


class Enrollment(Strict):
    code: str = Field(default='', max_length=200)


class OTP(Strict):
    code: str = Field(pattern=r'^[0-9]{6}$')


class Credential(Strict):
    credential: dict
    name: str = Field(default='Passkey', min_length=1, max_length=80)


class Recovery(Strict):
    code: str = Field(min_length=20, max_length=100)


def enrollment_ticket(manager, address, reset=False):
    """Host-terminal authorization, never exposed by a browser account/password reset."""
    rp = urlsplit(manager.public_url).hostname if manager.public_url else 'localhost'
    token = secrets.token_urlsafe(24)
    with manager.store.db() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT id FROM users WHERE email=? AND admin=1 AND disabled=0', (address,)).fetchone()
        if not row:
            raise ValueError('Active administrator not found')
        uid = row[0]
        if reset:
            db.execute('DELETE FROM totp_factors WHERE user_id=?', (uid,))
            db.execute('DELETE FROM totp_pending WHERE user_id=?', (uid,))
            db.execute('DELETE FROM passkeys WHERE user_id=?', (uid,))
            db.execute('DELETE FROM mfa_recovery WHERE user_id=?', (uid,))
            db.execute('DELETE FROM sessions WHERE user_id=?', (uid,))
        elif db.execute('SELECT 1 FROM passkeys WHERE user_id=? AND rp=?', (uid, rp)).fetchone() or (manager.allow_totp and db.execute('SELECT 1 FROM totp_factors WHERE user_id=? AND rp=?', (uid, rp)).fetchone()):
            raise ValueError('An MFA factor is already enrolled; sign in with it or use recover-admin-mfa')
        db.execute('DELETE FROM mfa_challenges WHERE user_id=?', (uid,))
        db.execute('INSERT OR REPLACE INTO mfa_enrollments VALUES(?,?,?,?)', (uid, digest(token), rp, time.time() + 600))
        manager.audit(db, uid, 'operator-mfa-recovery' if reset else 'operator-mfa-enrollment')
    return token


def install(app, get_accounts):
    def manager_allowed_totp():
        return get_accounts().allow_totp

    def context(request, fresh=False, passkey=False):
        manager = get_accounts()
        if not manager.enabled:
            raise HTTPException(404, 'Accounts are not enabled')
        user = request.state.account
        if not user['admin'] or request.headers.get('x-session-audience') != 'admin':
            raise HTTPException(403, 'An administrator session is required')
        if time.time() - user['created'] > 900 and not user['mfa_at']:
            raise HTTPException(401, 'Sign in again before verifying your second factor')
        if fresh and (not user['mfa_at'] or time.time() - user['mfa_at'] > 900):
            raise HTTPException(401, 'Sign in and verify your second factor again')
        if passkey and user['mfa_method'] != 'passkey':
            raise HTTPException(403, 'Verify a passkey before replacing recovery codes')
        # Never derive the public RP from forwarded/client-controlled headers.
        origin = manager.public_url or str(request.base_url).rstrip('/')
        rp = urlsplit(origin).hostname
        if not manager.public_url and rp != 'localhost':
            raise HTTPException(409, 'Use http://localhost through your SSH tunnel to enroll or verify administrator MFA')
        return manager, user, digest(request.cookies.get(cookie('admin'), '')), origin, rp

    def keys(db, uid, rp):
        return db.execute('SELECT * FROM passkeys WHERE user_id=? AND rp=? ORDER BY created', (uid, rp)).fetchall()

    def challenge(db, sid, user, kind, origin, rp, ticket=None):
        value = secrets.token_bytes(32)
        db.execute('DELETE FROM mfa_challenges WHERE expires<?', (time.time(),))
        db.execute('INSERT OR REPLACE INTO mfa_challenges VALUES(?,?,?,?,?,?,?,?)',
                   (sid, user['id'], kind, value, origin, rp, ticket, time.time() + 120))
        return value

    def consume(manager, sid, user, kind, origin, rp):
        # Committed before verification; failed signatures cannot reuse a challenge.
        with manager.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM mfa_challenges WHERE session=?', (sid,)).fetchone()
            db.execute('DELETE FROM mfa_challenges WHERE session=?', (sid,))
        if not row or row['expires'] <= time.time() or (row['user_id'], row['kind'], row['origin'], row['rp']) != (user['id'], kind, origin, rp):
            raise HTTPException(400, 'Passkey request expired; try again')
        return row

    def active_session(db, sid, uid, fresh=False, passkey=False):
        row = db.execute("SELECT s.* FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.digest=? "
                         "AND s.user_id=? AND s.audience='admin' AND s.expires>? AND u.admin=1 AND u.disabled=0",
                         (sid, uid, time.time())).fetchone()
        if not row or (row['mfa_method'] == 'totp' and not manager_allowed_totp()):
            raise HTTPException(401, 'Sign in again')
        if fresh and (not row['mfa_at'] or time.time() - row['mfa_at'] > 900):
            raise HTTPException(401, 'Sign in and verify your second factor again')
        if passkey and row['mfa_method'] != 'passkey':
            raise HTTPException(403, 'Verify a passkey before changing security credentials')
        return row

    def elevate(manager, db, sid, uid, method):
        # Rotate the authenticated cookie; the original password-only token is invalid.
        now, token = time.time(), secrets.token_urlsafe(32)
        active_session(db, sid, uid)
        db.execute('DELETE FROM sessions WHERE digest=?', (sid,))
        db.execute('INSERT INTO sessions VALUES(?,?,?,?,?,?,?)', (digest(token), uid, 'admin', now, now + 3600, now, method))
        manager.audit(db, uid, 'mfa-verified', method)
        return token

    def response(manager, token, **extra):
        from fastapi.responses import JSONResponse
        result = JSONResponse({'ok': True, **extra})
        result.set_cookie(cookie('admin'), token, httponly=True, secure=manager.secure, samesite='lax', max_age=3600, path='/')
        return result

    def recovery_codes(manager, db, uid):
        codes = [secrets.token_hex(16) for _ in range(8)]
        db.execute('DELETE FROM mfa_recovery WHERE user_id=?', (uid,))
        db.executemany('INSERT INTO mfa_recovery VALUES(?,?)', [(digest(code), uid) for code in codes])
        manager.audit(db, uid, 'mfa-recovery-codes-replaced')
        return codes

    def credential_session(db, sid, uid):
        row = active_session(db, sid, uid, fresh=True)
        if row['mfa_method'] not in ({'passkey', 'totp'} if manager_allowed_totp() else {'passkey'}):
            raise HTTPException(403, 'Verify a passkey or authenticator code before changing credentials')
        return row

    def enrolled_totp(manager, uid, rp):
        with manager.store.db() as db:
            return db.execute('SELECT 1 FROM totp_factors WHERE user_id=? AND rp=?', (uid, rp)).fetchone() is not None

    @app.get('/api/auth/mfa')
    def status(request: Request):
        manager, user, sid, origin, rp = context(request)
        with manager.store.db() as db:
            rows = keys(db, user['id'], rp)
            remaining = db.execute('SELECT count(*) FROM mfa_recovery WHERE user_id=?', (user['id'],)).fetchone()[0]
        return {'verified': bool(user['mfa_at']), 'method': user['mfa_method'], 'rp': rp,
                'keys': [{'id': row['id'], 'name': row['name']} for row in rows], 'recovery_remaining': remaining, 'totp_allowed': manager.allow_totp,
                'totp_enrolled': bool(manager.allow_totp and enrolled_totp(manager, user['id'], rp))}

    @app.post('/api/auth/mfa/register/options')
    def registration_options(body: Enrollment, request: Request):
        manager, user, sid, origin, rp = context(request)
        manager.limit('mfa:' + user['id'], maximum=30)
        with manager.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            rows = keys(db, user['id'], rp)
            if len(rows) >= 8:
                raise HTTPException(409, 'Keep at most eight passkeys per hostname')
            ticket = None
            if rows or (manager.allow_totp and db.execute('SELECT 1 FROM totp_factors WHERE user_id=? AND rp=?', (user['id'], rp)).fetchone()):
                active_session(db, sid, user['id'], fresh=True)
            else:
                entry = db.execute('SELECT * FROM mfa_enrollments WHERE user_id=? AND digest=? AND rp=? AND expires>?',
                                   (user['id'], digest(body.code), rp, time.time())).fetchone()
                if not entry:
                    raise HTTPException(403, 'Request a one-time enrollment code from the host operator')
                ticket = entry['digest']
            value = challenge(db, sid, user, 'register', origin, rp, ticket)
        options = generate_registration_options(
            rp_id=rp, rp_name='Argon Recordings', user_id=user['id'].encode(), user_name=user['email'],
            challenge=value, timeout=120000,
            authenticator_selection=AuthenticatorSelectionCriteria(resident_key=ResidentKeyRequirement.PREFERRED,
                                                                   user_verification=UserVerificationRequirement.REQUIRED),
            exclude_credentials=[PublicKeyCredentialDescriptor(id=base64url_to_bytes(row['id'])) for row in rows])
        return json.loads(options_to_json(options))

    @app.post('/api/auth/mfa/register/verify')
    def registration_verify(body: Credential, request: Request):
        manager, user, sid, origin, rp = context(request)
        manager.limit('mfa:' + user['id'], maximum=30)
        flow = consume(manager, sid, user, 'register', origin, rp)
        try:
            result = verify_registration_response(credential=body.credential, expected_challenge=flow['challenge'],
                                                  expected_rp_id=rp, expected_origin=origin, require_user_verification=True)
        except Exception as exc:
            raise HTTPException(400, 'Passkey could not be verified; try again') from exc
        try:
            with manager.store.db() as db:
                db.execute('BEGIN IMMEDIATE')
                if flow['ticket']:
                    if db.execute('DELETE FROM mfa_enrollments WHERE user_id=? AND digest=? AND rp=? AND expires>?',
                                  (user['id'], flow['ticket'], rp, time.time())).rowcount != 1:
                        raise HTTPException(400, 'Enrollment code expired or was already used')
                else:
                    active_session(db, sid, user['id'], fresh=True)
                if len(keys(db, user['id'], rp)) >= 8:
                    raise HTTPException(409, 'Passkey limit reached')
                db.execute('INSERT INTO passkeys VALUES(?,?,?,?,?,?,?)', (bytes_to_base64url(result.credential_id), user['id'], rp,
                           body.name.strip() or 'Passkey', result.credential_public_key, result.sign_count, time.time()))
                token = elevate(manager, db, sid, user['id'], 'passkey')
                # A recovery code intentionally authorizes replacing a lost passkey.
                # Rotate remaining codes and revoke other admin sessions on replacement.
                db.execute("DELETE FROM sessions WHERE user_id=? AND audience='admin' AND digest!=?", (user['id'], digest(token)))
                codes = recovery_codes(manager, db, user['id']) if flow['ticket'] or user['mfa_method'] == 'recovery' else None
                manager.audit(db, user['id'], 'passkey-enrolled', rp)
        except sqlite3.IntegrityError as exc:
            raise HTTPException(409, 'That passkey is already registered') from exc
        return response(manager, token, recovery_codes=codes)

    @app.post('/api/auth/mfa/authenticate/options')
    def authentication_options(request: Request):
        manager, user, sid, origin, rp = context(request)
        manager.limit('mfa:' + user['id'], maximum=30)
        with manager.store.db() as db:
            rows = keys(db, user['id'], rp)
            if not rows:
                raise HTTPException(409, 'Enroll an administrator passkey first')
            value = challenge(db, sid, user, 'authenticate', origin, rp)
        return json.loads(options_to_json(generate_authentication_options(rp_id=rp, challenge=value, timeout=120000,
            allow_credentials=[PublicKeyCredentialDescriptor(id=base64url_to_bytes(row['id'])) for row in rows],
            user_verification=UserVerificationRequirement.REQUIRED)))

    @app.post('/api/auth/mfa/authenticate/verify')
    def authentication_verify(body: Credential, request: Request):
        manager, user, sid, origin, rp = context(request)
        manager.limit('mfa:' + user['id'], maximum=30)
        flow = consume(manager, sid, user, 'authenticate', origin, rp)
        with manager.store.db() as db:
            row = db.execute('SELECT * FROM passkeys WHERE id=? AND user_id=? AND rp=?',
                             (body.credential.get('id') if isinstance(body.credential.get('id'), str) else '', user['id'], rp)).fetchone()
        if not row:
            raise HTTPException(400, 'Passkey could not be verified')
        try:
            result = verify_authentication_response(credential=body.credential, expected_challenge=flow['challenge'],
                expected_rp_id=rp, expected_origin=origin, credential_public_key=row['public_key'],
                credential_current_sign_count=row['sign_count'], require_user_verification=True)
        except Exception as exc:
            raise HTTPException(400, 'Passkey could not be verified; try again') from exc
        with manager.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('UPDATE passkeys SET sign_count=? WHERE id=? AND user_id=? AND sign_count=?',
                          (result.new_sign_count, row['id'], user['id'], row['sign_count'])).rowcount != 1:
                raise HTTPException(409, 'Passkey changed; try again')
            token = elevate(manager, db, sid, user['id'], 'passkey')
        return response(manager, token)

    @app.post('/api/auth/mfa/recovery')
    def recover(body: Recovery, request: Request):
        manager, user, sid, origin, rp = context(request)
        manager.limit('mfa-recovery:' + user['id'], maximum=8, seconds=900)
        with manager.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            # Recovery only restores access at a hostname with an enabled enrolled factor.
            has_factor = bool(keys(db, user['id'], rp) or (manager.allow_totp and db.execute('SELECT 1 FROM totp_factors WHERE user_id=? AND rp=?', (user['id'], rp)).fetchone()))
            if not has_factor or db.execute('DELETE FROM mfa_recovery WHERE digest=? AND user_id=?',
                (digest(body.code.strip()), user['id'])).rowcount != 1:
                raise HTTPException(400, 'Recovery code is invalid or already used')
            db.execute('DELETE FROM mfa_challenges WHERE session=?', (sid,))
            token = elevate(manager, db, sid, user['id'], 'recovery')
        return response(manager, token)

    @app.post('/api/auth/mfa/recovery-codes')
    def replace_codes(request: Request):
        manager, user, sid, origin, rp = context(request, fresh=True)
        with manager.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            credential_session(db, sid, user['id'])
            codes = recovery_codes(manager, db, user['id'])
        return {'codes': codes}

    @app.delete('/api/auth/mfa/keys/{key_id}')
    def remove_key(key_id: str, request: Request):
        manager, user, sid, origin, rp = context(request, fresh=True, passkey=True)
        with manager.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            active_session(db, sid, user['id'], fresh=True, passkey=True)
            if not db.execute('SELECT 1 FROM passkeys WHERE id=? AND user_id=? AND rp=?', (key_id, user['id'], rp)).fetchone():
                raise HTTPException(404, 'Passkey not found')
            if len(keys(db, user['id'], rp)) <= 1:
                raise HTTPException(409, 'Add another passkey before removing your last one')
            db.execute('DELETE FROM passkeys WHERE id=?', (key_id,))
            db.execute("DELETE FROM sessions WHERE user_id=? AND audience='admin' AND digest!=?", (user['id'], sid))
            db.execute('DELETE FROM mfa_challenges WHERE user_id=?', (user['id'],))
            manager.audit(db, user['id'], 'passkey-removed', rp)
        return {'ok': True}


    def totp_context(request):
        result = context(request)
        if not result[0].allow_totp:
            raise HTTPException(404, 'Authenticator apps are not enabled for this installation')
        return result

    @app.post('/api/auth/mfa/totp/setup')
    def totp_setup(body: Enrollment, request: Request):
        import pyotp
        import segno
        manager, user, sid, origin, rp = totp_context(request)
        manager.limit('totp-setup:' + user['id'], maximum=8)
        with manager.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            active_session(db, sid, user['id'])
            if db.execute('SELECT 1 FROM totp_factors WHERE user_id=? AND rp=?', (user['id'], rp)).fetchone():
                raise HTTPException(409, 'An authenticator app is already enrolled')
            ticket = None
            if keys(db, user['id'], rp):
                active_session(db, sid, user['id'], fresh=True)
            else:
                entry = db.execute('SELECT digest FROM mfa_enrollments WHERE user_id=? AND digest=? AND rp=? AND expires>?',
                                   (user['id'], digest(body.code), rp, time.time())).fetchone()
                if not entry:
                    raise HTTPException(403, 'Request a one-time enrollment code from the host operator')
                ticket = entry[0]
            db.execute('DELETE FROM totp_pending WHERE expires<?', (time.time(),))
            secret = pyotp.random_base32()
            encrypted = totp.encrypt(manager, db, user['id'], rp, secret)
            db.execute('INSERT OR REPLACE INTO totp_pending VALUES(?,?,?,?,?,?,?)',
                       (sid, user['id'], rp, origin, encrypted, ticket, time.time() + 600))
        uri = pyotp.TOTP(secret).provisioning_uri(name=user['email'] + ' (' + rp + ')', issuer_name='Argon Recordings')
        # Generate locally: never send an otpauth URI to an external QR service.
        return {'secret': secret, 'qr': segno.make(uri, micro=False).png_data_uri(scale=4), 'expires_in': 600}

    @app.post('/api/auth/mfa/totp/confirm')
    def totp_confirm(body: OTP, request: Request):
        manager, user, sid, origin, rp = totp_context(request)
        manager.limit('totp-verify:' + user['id'], maximum=8)
        with manager.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            live = active_session(db, sid, user['id'])
            entry = db.execute('SELECT * FROM totp_pending WHERE session=? AND user_id=? AND rp=? AND origin=? AND expires>?',
                               (sid, user['id'], rp, origin, time.time())).fetchone()
            if not entry:
                raise HTTPException(400, 'Authenticator setup expired; start again')
            if entry['ticket']:
                if db.execute('DELETE FROM mfa_enrollments WHERE user_id=? AND digest=? AND rp=? AND expires>?',
                              (user['id'], entry['ticket'], rp, time.time())).rowcount != 1:
                    raise HTTPException(400, 'Enrollment code expired or was already used')
            else:
                active_session(db, sid, user['id'], fresh=True)
            secret = totp.decrypt(manager, db, user['id'], rp, entry['secret'])
            counter = totp.matching_counter(secret, body.code)
            if db.execute('SELECT 1 FROM totp_factors WHERE user_id=? AND rp=?', (user['id'], rp)).fetchone():
                raise HTTPException(409, 'An authenticator app is already enrolled')
            db.execute('INSERT INTO totp_factors VALUES(?,?,?,?,?)', (user['id'], rp, entry['secret'], counter, time.time()))
            db.execute('DELETE FROM totp_pending WHERE user_id=?', (user['id'],))
            db.execute('DELETE FROM mfa_challenges WHERE user_id=?', (user['id'],))
            token = elevate(manager, db, sid, user['id'], 'totp')
            db.execute("DELETE FROM sessions WHERE user_id=? AND audience='admin' AND digest!=?", (user['id'], digest(token)))
            codes = recovery_codes(manager, db, user['id']) if entry['ticket'] or live['mfa_method'] == 'recovery' else None
            manager.audit(db, user['id'], 'totp-enrolled', rp)
        return response(manager, token, recovery_codes=codes)

    @app.post('/api/auth/mfa/totp/verify')
    def totp_verify(body: OTP, request: Request):
        manager, user, sid, origin, rp = totp_context(request)
        manager.limit('totp-verify:' + user['id'], maximum=8)
        with manager.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            active_session(db, sid, user['id'])
            entry = db.execute('SELECT * FROM totp_factors WHERE user_id=? AND rp=?', (user['id'], rp)).fetchone()
            if not entry:
                raise HTTPException(400, 'No authenticator app is enrolled for this hostname')
            counter = totp.matching_counter(totp.decrypt(manager, db, user['id'], rp, entry['secret']), body.code, entry['last_counter'])
            db.execute('UPDATE totp_factors SET last_counter=? WHERE user_id=? AND rp=?', (counter, user['id'], rp))
            token = elevate(manager, db, sid, user['id'], 'totp')
        return response(manager, token)

    @app.delete('/api/auth/mfa/totp')
    def totp_remove(request: Request):
        manager, user, sid, origin, rp = totp_context(request)
        with manager.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            credential_session(db, sid, user['id'])
            if not keys(db, user['id'], rp):
                raise HTTPException(409, 'Add a passkey before removing your authenticator app')
            if db.execute('DELETE FROM totp_factors WHERE user_id=? AND rp=?', (user['id'], rp)).rowcount != 1:
                raise HTTPException(404, 'Authenticator app not found')
            db.execute('DELETE FROM totp_pending WHERE user_id=?', (user['id'],))
            db.execute('DELETE FROM mfa_challenges WHERE user_id=?', (user['id'],))
            db.execute("DELETE FROM sessions WHERE user_id=? AND audience='admin'", (user['id'],))
            manager.audit(db, user['id'], 'totp-removed', rp)
        return {'ok': True}

    @app.delete('/api/auth/mfa/totp/setup')
    def totp_cancel(request: Request):
        manager, user, sid, origin, rp = totp_context(request)
        with manager.store.db() as db:
            db.execute('DELETE FROM totp_pending WHERE session=? AND user_id=?', (sid, user['id']))
        return {'ok': True}
