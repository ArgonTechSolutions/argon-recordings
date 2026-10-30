"""Boot bind-race recovery; synthetic Docker state, no system changes."""
import copy
import json
import os
import threading
import time
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from scripts import boot_recovery as boot


@pytest.fixture
def manifest():
    return dict(version=1,identity='a06fd327a1985e6d',api='argon-recordings',
                transport='argon-recordings-transport',address='100.70.97.127',port=8788,uid=1000)


def containers(value):
    api=dict(Name='/'+value['api'],Config={'Labels':{boot.LABEL:value['identity'],boot.ROLE:'api'}},State={'Running':True})
    relay=dict(Id='immutable-transport-id',Name='/'+value['transport'],Config={'Labels':{boot.LABEL:value['identity'],boot.ROLE:'transport'}},
               HostConfig={'PortBindings':{'8443/tcp':[{'HostIp':value['address'],'HostPort':str(value['port'])}]},'RestartPolicy':{'Name':'unless-stopped'}},
               State={'Running':False,'Error':'failed to set up container networking: failed to bind host port 100.70.97.127:8788/tcp: cannot assign requested address'})
    return api,relay


def test_only_known_bind_failure_is_recovered(manifest):
    api,relay=containers(manifest)
    assert boot.recovery_required(manifest,api,relay)
    relay['State']['Error']=''
    assert not boot.recovery_required(manifest,api,relay)  # Manual stop.
    relay['State']['Error']='failed to bind host port: address already in use'
    assert not boot.recovery_required(manifest,api,relay)
    relay=containers(manifest)[1]
    api['State']['Running']=False
    assert not boot.recovery_required(manifest,api,relay)
    api['State']['Running']=True
    relay['State']['Running']=True
    assert not boot.recovery_required(manifest,api,relay)


@pytest.mark.parametrize('change',['identity','role','name','address','port','restart'])
def test_wrong_container_or_public_binding_is_never_started(manifest,change):
    api,relay=containers(manifest)
    if change=='identity':relay['Config']['Labels'][boot.LABEL]='different'
    elif change=='role':relay['Config']['Labels'][boot.ROLE]='worker'
    elif change=='name':relay['Name']='/other-project'
    elif change=='address':relay['HostConfig']['PortBindings']['8443/tcp'][0]['HostIp']='0.0.0.0'
    elif change=='port':relay['HostConfig']['PortBindings']['8443/tcp'][0]['HostPort']='443'
    else:relay['HostConfig']['RestartPolicy']['Name']='always'
    with pytest.raises(ValueError):boot.recovery_required(manifest,api,relay)


def test_delayed_address_retries_then_starts_only_existing_transport(tmp_path,manifest,monkeypatch):
    path=tmp_path/'manifest.json';path.write_text(json.dumps(manifest));path.chmod(0o644)
    # Validation containers run as root; no root filesystem is written here.
    assert path.stat().st_uid==0
    api,relay=containers(manifest)
    lock=tmp_path/'lifecycle.lock';lock.touch();lock.chmod(0o600)
    os.chown(lock,manifest['uid'],0)
    monkeypatch.setattr(boot,'lock_path',lambda _:lock)
    monkeypatch.setattr(boot,'inspect',lambda name:api if name==manifest['api'] else relay)
    seen=[]
    def absent(args):
        seen.append(args)
        return SimpleNamespace(stdout='[]')
    monkeypatch.setattr(boot,'command',absent)
    with pytest.raises(RuntimeError,match='Waiting for'):
        boot.recover(path)
    assert not any('start' in args for args in seen)
    def available(args):
        seen.append(args)
        if args[0]=='/usr/sbin/ip':
            return SimpleNamespace(stdout=json.dumps([{'addr_info':[{'local':manifest['address']}]}]))
        assert args==[*boot.DOCKER,'start',relay['Id']]
        relay['State']['Running']=True
        return SimpleNamespace(stdout='')
    monkeypatch.setattr(boot,'command',available)
    boot.recover(path)
    assert relay['State']['Running']
    assert sum('start' in args for args in seen)==1
    boot.recover(path)
    assert sum('start' in args for args in seen)==1
    assert not any('run' in args or 'rm' in args for args in seen)


def test_manifest_and_root_execution_paths_are_restricted(tmp_path,manifest):
    for address in ['0.0.0.0','192.168.1.12','127.0.0.1','::1']:
        with pytest.raises(ValueError):boot.validate_manifest(dict(manifest,address=address))
    for name in ['../other','name with spaces','x;touch /tmp/no']:
        with pytest.raises(ValueError):boot.validate_manifest(dict(manifest,api=name))
    text=boot.unit_text(manifest,Path('/usr/local/lib/argon-recordings/boot/fixed'))
    assert 'Restart=on-failure' in text and 'RestartSec=5s' in text
    assert 'After=docker.service tailscaled.service network-online.target' in text
    assert '/home/' not in text and 'recordings.py' not in text
    assert 'ExecStart=/usr/bin/python3 -I -S ' in text
    directory=tmp_path/'writable';directory.mkdir(mode=0o777);directory.chmod(0o777)
    with pytest.raises(ValueError,match='Unsafe'):
        boot.trusted_directory(directory,[])


@pytest.mark.parametrize('previous',[False,True])
def test_publication_failure_restores_previous_unit_enablement(tmp_path,monkeypatch,previous):
    calls=[]
    failed=False
    def command(args):
        nonlocal failed
        calls.append(args)
        if 'daemon-reload' in args and not failed:
            failed=True
            raise RuntimeError('Injected reload failure')
        return SimpleNamespace(stdout='')
    monkeypatch.setattr(boot,'command',command)
    target=tmp_path/'test.service';backup=tmp_path/'backup'
    with pytest.raises(RuntimeError,match='Injected'):
        boot.publish_unit([],tmp_path,'test.service',target,backup,previous,previous,previous)
    assert ['/usr/bin/systemctl','stop','test.service'] in calls
    assert ['/usr/bin/systemctl','disable','test.service'] in calls
    if previous:
        assert any(str(backup/'previous.service') in call for call in calls)
        assert ['/usr/bin/systemctl','enable','test.service'] in calls
        assert ['/usr/bin/systemctl','start','--no-block','test.service'] in calls
    else:
        assert ['/usr/bin/rm','-f','--',str(target)] in calls
        assert not any('enable' in call for call in calls)
    assert sum('daemon-reload' in call for call in calls)==2


def test_operator_stop_wins_while_recovery_waits_for_shared_lock(tmp_path,manifest,monkeypatch):
    path=tmp_path/'manifest.json';path.write_text(json.dumps(manifest));path.chmod(0o644)
    lock=tmp_path/'lifecycle.lock';lock.touch();lock.chmod(0o600);os.chown(lock,manifest['uid'],0)
    monkeypatch.setattr(boot,'lock_path',lambda _:lock)
    api,relay=containers(manifest)
    monkeypatch.setattr(boot,'inspect',lambda name:api if name==manifest['api'] else relay)
    calls=[]
    monkeypatch.setattr(boot,'command',lambda args:calls.append(args))
    finished=threading.Event()
    errors=[]
    def recover():
        try:boot.recover(path)
        except Exception as exc:errors.append(exc)
        finally:finished.set()
    with boot.lifecycle_lock(manifest,required=True):
        thread=threading.Thread(target=recover)
        thread.start()
        assert not finished.wait(.1)
        api['State']['Running']=False  # Launcher stops it under the same lock.
    thread.join(timeout=2)
    assert finished.is_set() and not errors
    assert not calls



def test_rollback_restores_files_even_when_systemd_cleanup_fails(tmp_path,monkeypatch):
    calls=[]
    def command(args):
        calls.append(args)
        if '/usr/bin/systemctl' in args:
            raise RuntimeError('Injected DBus unavailable')
        return SimpleNamespace(stdout='')
    monkeypatch.setattr(boot,'command',command)
    target=tmp_path/'test.service';backup=tmp_path/'backup'
    with pytest.raises(RuntimeError,match='rollback incomplete'):
        boot.publish_unit([],tmp_path,'test.service',target,backup,True,True,True)
    assert any(str(backup/'previous.service') in call for call in calls)
    assert sum(call[0]=='/usr/bin/mv' and str(target) in call for call in calls)==2
    assert ['/usr/bin/systemctl','enable','test.service'] in calls
    assert ['/usr/bin/systemctl','start','--no-block','test.service'] in calls


def test_setup_installs_isolated_root_snapshot_not_user_checkout(tmp_path,manifest,monkeypatch):
    from contextlib import nullcontext
    api,relay=containers(manifest)
    cfg=dict(mode='coordinator',identity=manifest['identity'],name=manifest['api'],transport=dict(bind_address=manifest['address'],port=manifest['port']))
    monkeypatch.setattr(boot.os,'geteuid',lambda:manifest['uid'])
    monkeypatch.setattr(boot.os,'access',lambda *args:True)
    monkeypatch.setattr(boot,'trusted_directory',lambda *args:None)
    monkeypatch.setattr(boot,'lifecycle_lock',lambda *args,**kw:nullcontext())
    monkeypatch.setattr(boot.subprocess,'run',lambda *args,**kw:SimpleNamespace(stdout='',returncode=1))
    seen=[]
    texts=[]
    def command(args):
        seen.append(args)
        if 'inspect' in args:
            return SimpleNamespace(stdout=json.dumps([api if args[-1]==manifest['api'] else relay]))
        if 'verify' in args:
            texts.append(Path(args[-1]).read_text())
        if 'LoadState' in args:return SimpleNamespace(stdout='loaded')
        return SimpleNamespace(stdout='')
    monkeypatch.setattr(boot,'command',command)
    installer=SimpleNamespace(docker=lambda *args,**kwargs:SimpleNamespace(stdout='[]'))
    boot.setup(installer,cfg)
    assert len(texts)==1
    assert 'ExecStart=/usr/bin/python3 -I -S /usr/local/lib/argon-recordings/boot/' in texts[0]
    bootstrap=next(call for call in seen if '-c' in call)
    assert bootstrap[:6]==['sudo','-n','/usr/bin/python3','-I','-S','-c']
    assert any('O_EXCL' in arg and 'O_NOFOLLOW' in arg for arg in bootstrap)
    assert any('enable' in call for call in seen)
    assert not any('/etc/caddy' in arg or '/models/' in arg for call in seen for arg in call)
