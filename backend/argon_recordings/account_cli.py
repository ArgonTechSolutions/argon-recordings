"""Private interactive account provisioning; passwords never enter command arguments."""
import argparse
import getpass
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

from .accounts import Accounts, email
from .store import Store


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['setup-admin', 'setup-oidc', 'recover-admin', 'setup-admin-mfa', 'recover-admin-mfa'])
    parser.add_argument('--adopt-existing', action='store_true')
    args = parser.parse_args()
    if os.environ.get('RECORDINGS_AUTH_MODE') != 'accounts':
        parser.exit(1, 'Enable auth.mode=accounts and restart before account setup.\n')
    manager = Accounts(Store(Path(os.environ.get('RECORDINGS_DATA', '/data')), recover_uploads=False), 'accounts', os.environ.get('RECORDINGS_PUBLIC_URL', ''))
    try:
        if args.command == 'setup-oidc':
            if not manager.public_url:
                raise ValueError('Configure auth.public_url as the final HTTPS origin first')
            issuer = input('OIDC issuer (HTTPS): ').strip()
            parsed = urlsplit(issuer)
            if parsed.scheme != 'https' or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError('Use an HTTPS issuer without credentials, query or fragment')
            config = dict(issuer=issuer, client_id=input('Client ID: ').strip(), client_secret=getpass.getpass('Client secret: '))
            if not config['client_id'] or not config['client_secret']:
                raise ValueError('Client ID and secret are required')
            folder = manager.store.root / 'auth'
            folder.mkdir(mode=0o700, exist_ok=True)
            target = folder / 'oidc.json'
            # This operator command is the only writer; atomic replacement preserves a valid old config on failure.
            temporary = folder / 'oidc.json.new'
            with open(temporary, 'w', opener=lambda path, flags: os.open(path, flags | os.O_NOFOLLOW, 0o600)) as out:
                os.fchmod(out.fileno(), 0o600)
                json.dump(config, out)
                out.flush()
                os.fsync(out.fileno())
            temporary.chmod(0o600)
            temporary.replace(target)
            print('OIDC configuration saved privately. Redirect URI:', manager.public_url + '/api/auth/oidc/callback')
            return
        address = email(input('Administrator email: '))
        if args.command in {'setup-admin-mfa', 'recover-admin-mfa'}:
            from .passkeys import enrollment_ticket
            reset = args.command == 'recover-admin-mfa'
            if reset and input('Remove administrator passkeys and recovery codes? Type RESET: ') != 'RESET':
                raise ValueError('Recovery cancelled')
            ticket = enrollment_ticket(manager, address, reset=reset)
            print('One-time passkey enrollment code (expires in 10 minutes):', ticket)
            print('Enter this privately at /admin/. Do not share it in chat or logs.')
            return
        name = input('Display name: ').strip() if args.command == 'setup-admin' else None
        password = getpass.getpass('Password (15–128 characters): ')
        if password != getpass.getpass('Confirm password: '):
            raise ValueError('Passwords do not match')
        if args.command == 'setup-admin':
            if not name or len(name) > 100:
                raise ValueError('Display name must contain 1–100 characters')
            manager.bootstrap(address, name, password, adopt=args.adopt_existing)
        else:
            hashed = manager.hash_password(password)
            with manager.store.db() as db:
                db.execute('BEGIN IMMEDIATE')
                row = db.execute('SELECT id FROM users WHERE email=? AND admin=1', (address,)).fetchone()
                if not row:
                    raise ValueError('Administrator not found')
                db.execute('UPDATE users SET password=?,disabled=0 WHERE id=?', (hashed, row[0]))
                db.execute('DELETE FROM sessions WHERE user_id=?', (row[0],))
                manager.audit(db, row[0], 'operator-recovery')
        print('Administrator ready. Passwords and session tokens were not printed or saved as plaintext.')
    except Exception as exc:
        parser.exit(1, f'Account setup stopped: {exc}\n')


if __name__ == '__main__':
    main()
