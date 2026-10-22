"""Interactive setup does not put secrets in arguments/logs or reset live uploads."""
import json
import stat
from argon_recordings import account_cli
from argon_recordings.accounts import Accounts
from argon_recordings.store import Store
from test_accounts import PASSWORD


def test_private_setup_oidc_and_recovery(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv('RECORDINGS_DATA',str(tmp_path))
    monkeypatch.setenv('RECORDINGS_AUTH_MODE','accounts')
    values = iter(['owner@example.com','Owner'])
    monkeypatch.setattr('builtins.input',lambda _:next(values))
    monkeypatch.setattr('getpass.getpass',lambda _:PASSWORD)
    monkeypatch.setattr('sys.argv',['account_cli','setup-admin'])
    account_cli.main()
    assert PASSWORD not in capsys.readouterr().out
    manager = Accounts(Store(tmp_path),'accounts')
    with manager.store.db() as db:
        uid = db.execute('SELECT id FROM users').fetchone()[0]
        db.execute("INSERT INTO projects VALUES('p','Ongoing project',0)")
        db.execute("INSERT INTO recordings(id,project_id,title,filename,language,status,created) VALUES('r','p','Uploading','x.wav','en','uploading',0)")
    token = manager.new_session(uid,'app')
    monkeypatch.setenv('RECORDINGS_PUBLIC_URL','https://recordings.example')
    values = iter(['https://issuer.example/','test-client'])
    monkeypatch.setattr('builtins.input',lambda _:next(values))
    monkeypatch.setattr('getpass.getpass',lambda _:'synthetic-provider-secret')
    monkeypatch.setattr('sys.argv',['account_cli','setup-oidc'])
    account_cli.main()
    assert 'synthetic-provider-secret' not in capsys.readouterr().out
    path = tmp_path / 'auth/oidc.json'
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text())['issuer'] == 'https://issuer.example/'
    with manager.store.db() as db:
        assert db.execute("SELECT status FROM recordings WHERE id='r'").fetchone()[0] == 'uploading'
    monkeypatch.setattr('builtins.input',lambda _:'owner@example.com')
    monkeypatch.setattr('getpass.getpass',lambda _:'another valid password for recovery')
    monkeypatch.setattr('sys.argv',['account_cli','recover-admin'])
    account_cli.main()
    assert manager.session(token) is None
    assert 'another valid password' not in capsys.readouterr().out


def test_terminal_mfa_enrollment_and_explicit_recovery(tmp_path, monkeypatch, capsys):
    from argon_recordings.accounts import digest
    monkeypatch.setenv('RECORDINGS_DATA', str(tmp_path))
    monkeypatch.setenv('RECORDINGS_AUTH_MODE', 'accounts')
    manager = Accounts(Store(tmp_path), 'accounts')
    uid = manager.bootstrap('owner@example.com', 'Owner', PASSWORD)
    monkeypatch.setattr('builtins.input', lambda _: 'owner@example.com')
    monkeypatch.setattr('sys.argv', ['account_cli', 'setup-admin-mfa'])
    account_cli.main()
    output = capsys.readouterr().out
    code = output.split('minutes): ', 1)[1].splitlines()[0]
    with manager.store.db() as db:
        assert db.execute('SELECT digest FROM mfa_enrollments WHERE user_id=?', (uid,)).fetchone()[0] == digest(code)
        db.execute("INSERT INTO passkeys VALUES('synthetic',?,'localhost','Lost key',X'00',0,0)", (uid,))
    token = manager.new_session(uid, 'app')
    monkeypatch.setattr('sys.argv', ['account_cli', 'recover-admin-mfa'])
    values = iter(['owner@example.com', 'cancel'])
    monkeypatch.setattr('builtins.input', lambda _: next(values))
    import pytest
    with pytest.raises(SystemExit):
        account_cli.main()
    assert manager.session(token)
    values = iter(['owner@example.com', 'RESET'])
    monkeypatch.setattr('builtins.input', lambda _: next(values))
    account_cli.main()
    assert manager.session(token) is None
    with manager.store.db() as db:
        assert not db.execute('SELECT 1 FROM passkeys').fetchone()
        assert db.execute('SELECT 1 FROM users WHERE id=?', (uid,)).fetchone()
