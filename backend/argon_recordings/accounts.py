"""Invitation-only identities, opaque sessions and project authorization."""
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import threading
import time
from urllib.parse import urlsplit

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, InvalidHashError
from fastapi import HTTPException

SCHEMA = '''
CREATE TABLE IF NOT EXISTS account_settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY,email TEXT UNIQUE NOT NULL,name TEXT NOT NULL,password TEXT,
 admin INTEGER NOT NULL DEFAULT 0,disabled INTEGER NOT NULL DEFAULT 0,created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS memberships(project_id TEXT NOT NULL REFERENCES projects(id),user_id TEXT NOT NULL REFERENCES users(id),
 role TEXT NOT NULL CHECK(role IN ('owner','editor','viewer')),PRIMARY KEY(project_id,user_id));
CREATE TABLE IF NOT EXISTS sessions(digest TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES users(id),audience TEXT NOT NULL,
 created REAL NOT NULL,expires REAL NOT NULL);
CREATE INDEX IF NOT EXISTS sessions_user ON sessions(user_id);
CREATE TABLE IF NOT EXISTS invitations(digest TEXT PRIMARY KEY,email TEXT NOT NULL,expires REAL NOT NULL,used REAL,created_by TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS password_resets(digest TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES users(id),expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS external_identities(issuer TEXT NOT NULL,subject TEXT NOT NULL,user_id TEXT NOT NULL REFERENCES users(id),PRIMARY KEY(issuer,subject));
CREATE TABLE IF NOT EXISTS oidc_flows(digest TEXT PRIMARY KEY,browser TEXT NOT NULL,nonce TEXT NOT NULL,verifier TEXT NOT NULL,
 audience TEXT NOT NULL,expires REAL NOT NULL,invite TEXT,link_user TEXT);
CREATE TABLE IF NOT EXISTS account_audit(id INTEGER PRIMARY KEY,actor TEXT,action TEXT NOT NULL,target TEXT,created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS login_limits(key TEXT PRIMARY KEY,count INTEGER NOT NULL,expires REAL NOT NULL);
'''


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def email(value):
    value = value.strip().lower()
    if len(value) > 254 or not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', value):
        raise HTTPException(422, 'Enter a valid email address')
    return value


def cookie(audience):
    return 'recordings_admin_session' if audience == 'admin' else 'recordings_session'


def csrf(token):
    return digest('recordings-csrf:' + token)


class Accounts:
    def __init__(self, store, mode='local', public_url='', allow_totp=False):
        if mode not in {'local', 'accounts'}:
            raise ValueError('auth.mode must be local or accounts')
        self.store, self.enabled = store, mode == 'accounts'
        self.public_url = public_url.rstrip('/')
        if self.public_url:
            url = urlsplit(self.public_url)
            if url.scheme != 'https' or not url.netloc or url.username or url.password or url.path or url.query or url.fragment:
                raise ValueError('auth.public_url must be an HTTPS origin without a path')
            if not self.enabled:
                raise ValueError('Public origin requires account mode')
        self.allow_totp = allow_totp
        self.secure = bool(self.public_url)
        self.hashes = PasswordHasher(time_cost=2, memory_cost=19456, parallelism=1)
        self.password_slot = threading.BoundedSemaphore(1)
        with store.db() as db:
            db.executescript(SCHEMA)
            version = db.execute("SELECT value FROM account_settings WHERE key='schema'").fetchone()
            if version and version[0] not in {'1', '2', '3'}:
                raise ValueError('Unsupported account schema version')
            # Additive migration preserves projects, models and app sessions. Old admin sessions
            # receive no second-factor proof and cannot authorize administrative routes.
            columns = {row[1] for row in db.execute('PRAGMA table_info(sessions)')}
            if 'mfa_at' not in columns:
                db.execute('ALTER TABLE sessions ADD COLUMN mfa_at REAL')
            if 'mfa_method' not in columns:
                db.execute('ALTER TABLE sessions ADD COLUMN mfa_method TEXT')
            from .passkeys import SCHEMA as PASSKEY_SCHEMA
            db.executescript(PASSKEY_SCHEMA)
            db.execute("INSERT OR REPLACE INTO account_settings VALUES('schema','3')")
            active = db.execute("SELECT value FROM account_settings WHERE key='enabled'").fetchone()
            if active and active[0] == 'true' and not self.enabled:
                raise ValueError('Accounts have been activated; anonymous fallback is disabled')
            db.execute("INSERT OR IGNORE INTO account_settings VALUES('registration','invite')")
            if not self.allow_totp:
                db.execute("DELETE FROM sessions WHERE mfa_method='totp'")
        self.dummy = self.hashes.hash(secrets.token_urlsafe(32)) if self.enabled else ''

    def audit(self, db, actor, action, target=None):
        db.execute('INSERT INTO account_audit(actor,action,target,created) VALUES(?,?,?,?)', (actor, action, target, time.time()))

    def policy(self):
        with self.store.db() as db:
            return db.execute("SELECT value FROM account_settings WHERE key='registration'").fetchone()[0]

    def hash_password(self, value):
        if not 15 <= len(value) <= 128:
            raise HTTPException(422, 'Use a password between 15 and 128 characters')
        if not self.password_slot.acquire(blocking=False):
            raise HTTPException(429, 'Password service busy; try again shortly')
        try:
            return self.hashes.hash(value)
        finally:
            self.password_slot.release()

    def verify(self, hashed, value):
        if not self.password_slot.acquire(blocking=False):
            raise HTTPException(429, 'Password service busy; try again shortly')
        try:
            try:
                return self.hashes.verify(hashed or self.dummy, value) and bool(hashed)
            except (VerificationError, InvalidHashError):
                return False
        finally:
            self.password_slot.release()

    def limit(self, key, maximum=8, seconds=300):
        key = digest(key)
        now = time.time()
        with self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM login_limits WHERE expires<?', (now,))
            row = db.execute('SELECT count FROM login_limits WHERE key=?', (key,)).fetchone()
            if row and row[0] >= maximum:
                raise HTTPException(429, 'Too many attempts; try again later')
            db.execute('INSERT INTO login_limits VALUES(?,1,?) ON CONFLICT(key) DO UPDATE SET count=count+1', (key, now + seconds))

    def session(self, token, audience='app'):
        if not token:
            return None
        with self.store.db() as db:
            row = db.execute('SELECT u.id,u.email,u.name,u.admin,s.created,s.expires,s.mfa_at,s.mfa_method FROM sessions s JOIN users u ON u.id=s.user_id '
                             'WHERE s.digest=? AND s.audience=? AND s.expires>? AND u.disabled=0', (digest(token), audience, time.time())).fetchone()
            if row and row['mfa_method'] == 'totp' and not self.allow_totp:
                db.execute('DELETE FROM sessions WHERE digest=?', (digest(token),))
                return None
            return dict(row) if row else None

    def new_session(self, uid, audience):
        token = secrets.token_urlsafe(32)
        now = time.time()
        with self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            user = db.execute('SELECT * FROM users WHERE id=? AND disabled=0', (uid,)).fetchone()
            if not user or (audience == 'admin' and not user['admin']):
                raise HTTPException(403, 'Administrator access required')
            db.execute('DELETE FROM sessions WHERE expires<?', (now,))
            # Bound retained sessions per user/audience.
            db.execute('DELETE FROM sessions WHERE digest IN (SELECT digest FROM sessions WHERE user_id=? AND audience=? ORDER BY created DESC LIMIT -1 OFFSET 9)', (uid, audience))
            db.execute('INSERT INTO sessions(digest,user_id,audience,created,expires) VALUES(?,?,?,?,?)', (digest(token), uid, audience, now, now + (3600 if audience == 'admin' else 43200)))
            self.audit(db, uid, 'login', audience)
        return token

    def role(self, uid, pid):
        with self.store.db() as db:
            row = db.execute('SELECT m.role FROM memberships m JOIN users u ON u.id=m.user_id WHERE m.user_id=? AND m.project_id=? AND u.disabled=0', (uid, pid)).fetchone()
            return row[0] if row else None

    def allowed(self, token, pid):
        user = self.session(token)
        return bool(user and self.role(user['id'], pid))

    def authorize(self, request):
        path = request.scope['path']
        audience = 'admin' if path.startswith('/api/admin/') else request.headers.get('x-session-audience', 'app')
        if audience not in {'app', 'admin'}:
            raise HTTPException(400, 'Unknown session audience')
        token = request.cookies.get(cookie(audience), '')
        user = self.session(token, audience)
        if not user:
            raise HTTPException(401, 'Sign in to continue')
        if path.startswith('/api/admin/'):
            if not user['admin']:
                raise HTTPException(403, 'Administrator access required')
            if not user['mfa_at']:
                raise HTTPException(403, 'Verify your administrator passkey first')
            if time.time() - user['mfa_at'] > 900 and request.method not in {'GET', 'HEAD'}:
                with self.store.db() as db:
                    db.execute('DELETE FROM sessions WHERE digest=?', (digest(token),))
                raise HTTPException(401, 'Sign in again before changing instance settings')
        elif audience != 'app' and not path.startswith('/api/auth/'):
            raise HTTPException(403, 'An app session is required')
        if request.method not in {'GET', 'HEAD', 'OPTIONS'}:
            if not hmac.compare_digest(request.headers.get('x-csrf-token', ''), csrf(token)):
                raise HTTPException(403, 'Refresh the page before trying again')
        pid = None
        match = re.match(r'^/api/projects/([^/]+)(?:/|$)', path)
        if match:
            pid = match[1]
        match = re.match(r'^/api/recordings/([^/]+)(?:/|$)', path)
        if match:
            row = self.store.recording(match[1])
            if not row:
                raise HTTPException(404, 'Recording not found')
            pid = row['project_id']
        if path == '/api/search':
            pid = request.query_params.get('project_id')
            if not pid:
                raise HTTPException(422, 'A project is required')
        if pid:
            role = self.role(user['id'], pid)
            if not role:
                raise HTTPException(404, 'Project not found')
            required = 'owner' if '/members' in path else ('editor' if request.method not in {'GET', 'HEAD'} and not path.endswith('/correction-candidates') else 'viewer')
            if {'viewer': 0, 'editor': 1, 'owner': 2}[role] < {'viewer': 0, 'editor': 1, 'owner': 2}[required]:
                raise HTTPException(403, 'Your project role does not allow this action')
        request.state.account = user

    def bootstrap(self, address, name, password, adopt=False):
        address, hashed = email(address), self.hash_password(password)
        with self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT 1 FROM users').fetchone():
                raise ValueError('An account already exists; bootstrap is disabled')
            if db.execute('SELECT 1 FROM projects').fetchone() and not adopt:
                raise ValueError('Existing projects require explicit --adopt-existing confirmation')
            if db.execute("SELECT 1 FROM jobs WHERE state='running' AND lease_until>? UNION SELECT 1 FROM corrections WHERE state='running' AND lease_until>?", (time.time(), time.time())).fetchone():
                raise ValueError('Wait for active processing to finish before assigning existing projects')
            uid = secrets.token_hex(16)
            db.execute('INSERT INTO users VALUES(?,?,?,?,1,0,?)', (uid, address, name, hashed, time.time()))
            db.execute("INSERT INTO memberships SELECT id,?,'owner' FROM projects", (uid,))
            db.execute("INSERT OR REPLACE INTO account_settings VALUES('enabled','true')")
            self.audit(db, uid, 'bootstrap', 'existing-projects' if adopt else None)
        return uid

    def invite(self, actor, address):
        address = email(address)
        if self.policy() != 'invite':
            raise HTTPException(409, 'Registration is closed')
        token = secrets.token_urlsafe(32)
        with self.store.db() as db:
            if db.execute('SELECT 1 FROM users WHERE email=?', (address,)).fetchone():
                raise HTTPException(409, 'That account already exists')
            db.execute('DELETE FROM invitations WHERE email=? OR expires<?', (address, time.time()))
            db.execute('INSERT INTO invitations VALUES(?,?,?,NULL,?)', (digest(token), address, time.time() + 86400, actor))
            self.audit(db, actor, 'invite', address)
        return token

    def consume_invite(self, db, value):
        row = db.execute('SELECT * FROM invitations WHERE digest=? AND expires>? AND used IS NULL', (value, time.time())).fetchone()
        if not row or db.execute("SELECT value FROM account_settings WHERE key='registration'").fetchone()[0] != 'invite':
            raise HTTPException(400, 'Invitation is invalid or expired')
        db.execute('UPDATE invitations SET used=? WHERE digest=?', (time.time(), value))
        return row['email']

    def accept(self, token, name, password):
        hashed = self.hash_password(password)
        try:
            with self.store.db() as db:
                db.execute('BEGIN IMMEDIATE')
                address = self.consume_invite(db, digest(token))
                uid = secrets.token_hex(16)
                db.execute('INSERT INTO users VALUES(?,?,?,?,0,0,?)', (uid, address, name, hashed, time.time()))
                self.audit(db, uid, 'invitation-accepted')
            return uid
        except sqlite3.IntegrityError as exc:
            raise HTTPException(409, 'Invitation cannot be accepted') from exc

    def oidc_config(self):
        path = self.store.root / 'auth' / 'oidc.json'
        if not path.exists():
            return None
        if path.stat().st_mode & 0o077:
            raise ValueError('OIDC configuration must have mode 0600')
        config = json.loads(path.read_text())
        issuer = config['issuer']
        parsed = urlsplit(issuer)
        if parsed.scheme != 'https' or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment or not self.public_url:
            raise ValueError('OIDC requires an HTTPS issuer and auth.public_url')
        return dict(config, issuer=issuer)


class AccountRequestBudget:
    """Bound small JSON requests using ASGI receive, including chunked bodies."""
    def __init__(self, app, maximum=65536):
        self.app, self.maximum = app, maximum

    async def __call__(self, scope, receive, send):
        path = scope.get('path', '')
        limited = scope['type'] == 'http' and scope['method'] not in {'GET', 'HEAD', 'OPTIONS'} and (
            path.startswith('/api/auth/') or path.startswith('/api/admin/') or '/members' in path)
        worker_json = scope['type'] == 'http' and scope['method'] == 'POST' and path.startswith('/api/worker/')
        if not limited and not worker_json:
            return await self.app(scope, receive, send)
        maximum = 8 * 1024**2 if worker_json else self.maximum
        content = bytearray()
        while True:
            message = await receive()
            if message['type'] == 'http.disconnect':
                return
            block = message.get('body', b'')
            if len(content) + len(block) > maximum:
                from fastapi.responses import JSONResponse
                return await JSONResponse({'detail': 'Worker JSON request is too large' if worker_json else 'Account request is too large'}, status_code=413)(scope, receive, send)
            content.extend(block)
            if not message.get('more_body', False):
                break
        sent = False

        async def bounded_receive():
            nonlocal sent
            if not sent:
                sent = True
                return {'type': 'http.request', 'body': bytes(content), 'more_body': False}
            return await receive()
        await self.app(scope, bounded_receive, send)
