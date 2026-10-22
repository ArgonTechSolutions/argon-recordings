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
