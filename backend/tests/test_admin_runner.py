"""Host update completion survives container loss; claimed work is never replayed."""
import json
import subprocess
from types import SimpleNamespace
import pytest
from scripts import admin_runner


def test_bridge_checks_ownership_and_uses_fixed_role():
    calls=[]
    def docker(config,*args,**kwargs):
        calls.append((args,kwargs));return SimpleNamespace(stdout='{"update_id":null}')
    installer=SimpleNamespace(docker=docker,inspect=lambda config:{'State':{'Running':True}})
    config={'mode':'worker','name':'example'}
    assert admin_runner.bridge(installer,config,{'revision':'a'*40})=={'update_id':None}
    assert calls[0][0]==('exec','-i','example','python','-m','argon_recordings.management_bridge','remote-worker')
    assert json.loads(calls[0][1]['input'])['revision']=='a'*40
    installer.inspect=lambda config:None
    with pytest.raises(ValueError):admin_runner.bridge(installer,config,{})
    assert len(calls)==1


def test_result_receipt_survives_disconnection_without_replaying(tmp_path,monkeypatch):
    installer=SimpleNamespace(ROOT=tmp_path)
    config={'home':tmp_path,'config_path':tmp_path/'recordings.toml'}
    jid='a'*32;calls=[];updates=[]
    monkeypatch.setattr(admin_runner,'revision',lambda installer:'b'*40)
    def bridge(installer,config,body):
        calls.append(body)
        if len(calls)==1:return {'update_id':jid}
        if len(calls)==2:raise OSError('App restarting')
        return {'update_id':None}
    monkeypatch.setattr(admin_runner,'bridge',bridge)
    monkeypatch.setattr(admin_runner.subprocess,'run',lambda command,**kwargs:updates.append(command) or SimpleNamespace(returncode=0))
    with pytest.raises(OSError):admin_runner.run(installer,config,once=True)
    receipt=tmp_path/'.recordings/admin-runner/receipt.json'
    assert json.loads(receipt.read_text())=={'id':jid,'result':'completed'}
    admin_runner.run(installer,config,once=True)
    assert not receipt.exists() and len(updates)==1
    assert updates[0][-1]=='update' and updates[0][-3]=='--config'
    assert calls[-1]['result_id']==jid and calls[-1]['result']=='completed'


def test_interrupted_update_receipt_reports_failure_and_does_not_execute(tmp_path,monkeypatch):
    folder=tmp_path/'.recordings/admin-runner';folder.mkdir(parents=True)
    (folder/'receipt.json').write_text(json.dumps({'id':'a'*32,'result':'failed'}))
    calls=[]
    monkeypatch.setattr(admin_runner,'revision',lambda installer:'b'*40)
    monkeypatch.setattr(admin_runner,'bridge',lambda installer,config,body:calls.append(body) or {'update_id':None})
    monkeypatch.setattr(admin_runner.subprocess,'run',lambda *args,**kwargs:pytest.fail('Must never replay a claimed update'))
    admin_runner.run(SimpleNamespace(ROOT=tmp_path),{'home':tmp_path,'config_path':tmp_path/'recordings.toml'},once=True)
    assert calls[0]['result']=='failed' and not (folder/'receipt.json').exists()
