"""Small TOTP helpers. Secrets are encrypted in SQLite; key stays in private /data/auth."""
import os
import stat
import time

from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException
import pyotp

SCHEMA = '''
CREATE TABLE IF NOT EXISTS totp_factors(user_id TEXT NOT NULL REFERENCES users(id),rp TEXT NOT NULL,
 secret BLOB NOT NULL,last_counter INTEGER NOT NULL,created REAL NOT NULL,PRIMARY KEY(user_id,rp));
CREATE TABLE IF NOT EXISTS totp_pending(session TEXT PRIMARY KEY,user_id TEXT NOT NULL,rp TEXT NOT NULL,
 origin TEXT NOT NULL,secret BLOB NOT NULL,ticket TEXT,expires REAL NOT NULL);
'''


def cipher(manager, db, create=False):
    """Call under the database write lock, including key creation and factor reads."""
    folder = manager.store.root / 'auth'
    path = folder / 'totp.key'
    if create and not path.exists():
        if db.execute('SELECT 1 FROM totp_factors UNION SELECT 1 FROM totp_pending').fetchone():
            raise HTTPException(503, 'Restore the authenticator encryption key from backup')
        folder.mkdir(mode=0o700, exist_ok=True)
        # All callers hold BEGIN IMMEDIATE: nobody can observe a partial new key.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'wb') as out:
            out.write(Fernet.generate_key())
            out.flush()
            os.fsync(out.fileno())
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, 'rb') as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
                raise ValueError('unsafe key permissions')
            return Fernet(source.read(128))
    except (OSError, ValueError) as exc:
        raise HTTPException(503, 'Restore the private authenticator encryption key and its 0600 permissions') from exc


def encrypt(manager, db, uid, rp, secret):
    # Bind ciphertext to the stable account/RP, not just to a global encryption key.
    return cipher(manager, db, create=True).encrypt((uid + '\n' + rp + '\n' + secret).encode())


def decrypt(manager, db, uid, rp, encrypted):
    try:
        plain = cipher(manager, db).decrypt(encrypted).decode()
        account, hostname, secret = plain.split('\n')
        if (account, hostname) != (uid, rp):
            raise ValueError('factor identity mismatch')
        return secret
    except (InvalidToken, UnicodeError, ValueError) as exc:
        raise HTTPException(503, 'Authenticator secret could not be opened; restore the matching key and database') from exc


def matching_counter(secret, code, previous=-1):
    # Verify a bounded +/-30-second clock window using the maintained library.
    current = int(time.time()) // 30
    otp = pyotp.TOTP(secret)
    matches = [counter for counter in range(max(0, current - 1), current + 2)
               if counter > previous and otp.verify(code, for_time=counter * 30)]
    if not matches:
        raise HTTPException(400, 'Code is incorrect, expired or already used. Try the next code.')
    return max(matches)
