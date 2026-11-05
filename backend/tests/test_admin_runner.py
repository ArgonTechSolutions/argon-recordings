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
    monkeypatch.setattr(admin_runner,'check_update',lambda *args:{'state':'failed','latest_revision':None,'checked_at':0,'request_id':0})
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


def test_update_check_distinguishes_release_and_runtime(monkeypatch,tmp_path):
    from scripts import git_updates
    monkeypatch.setattr(git_updates,'checked_checkout',lambda root:root)
    monkeypatch.setattr(git_updates,'git',lambda *args,**kwargs:SimpleNamespace(stdout='b'*40+'\trefs/heads/main\n'))
    monkeypatch.setattr(admin_runner,'revision',lambda installer:'a'*40)
    installer=SimpleNamespace(ROOT=tmp_path,state=lambda c:{'source':'installed'},source_id=lambda:'installed')
    assert admin_runner.check_update(installer,{},7)['state']=='available'
    monkeypatch.setattr(admin_runner,'revision',lambda installer:'b'*40)
    result=admin_runner.check_update(installer,{},7)
    assert result['state']=='up_to_date' and result['request_id']==7
    installer.source_id=lambda:'different-runtime'
    assert admin_runner.check_update(installer,{})['state']=='failed'
    def failure(*args,**kwargs):raise subprocess.CalledProcessError(1,'git',stderr='private token must not escape')
    monkeypatch.setattr(git_updates,'git',failure)
    assert 'private token' not in json.dumps(admin_runner.check_update(installer,{}))


def test_service_paths_cannot_inject_unit_directives(tmp_path):
    from scripts.admin_runner_service import definition,unit_name
    with pytest.raises(ValueError):definition(tmp_path/'bad\nUser=root',tmp_path/'config','argon','/home/argon')
    with pytest.raises(ValueError):definition(tmp_path,tmp_path/'config','argon\nUser=root','/home/argon')
    body=definition(tmp_path/'with space%p',tmp_path/'config','argon','/home/argon')
    assert 'User=argon\n' in body and '%%p' in body and 'Restart=on-failure' in body
    assert unit_name(tmp_path)!=unit_name(tmp_path/'other')


def test_successful_persistent_update_reloads_only_after_receipt_ack(tmp_path,monkeypatch):
    installer=SimpleNamespace(ROOT=tmp_path)
    config={'home':tmp_path,'config_path':tmp_path/'recordings.toml'}
    monkeypatch.setattr(admin_runner,'revision',lambda installer:'b'*40)
    monkeypatch.setattr(admin_runner,'check_update',lambda *args:{'state':'available','checked_at':0,'request_id':0})
    calls=[]
    def bridge(*args):
        calls.append(args[-1]);return {'update_id':'a'*32 if len(calls)==1 else None}
    monkeypatch.setattr(admin_runner,'bridge',bridge)
    monkeypatch.setattr(admin_runner.subprocess,'run',lambda *args,**kwargs:SimpleNamespace(returncode=0))
    def reload(executable,args):
        assert len(calls)==2 and calls[-1]['result']=='completed'
        assert not (tmp_path/'.recordings/admin-runner/receipt.json').exists()
        assert args[-1]=='admin-runner'
        raise RuntimeError('reload verified')
    monkeypatch.setattr(admin_runner.os,'execv',reload)
    with pytest.raises(RuntimeError,match='reload verified'):admin_runner.run(installer,config)
