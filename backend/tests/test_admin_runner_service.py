"""Service setup failure must not leave a new root-equivalent grant behind."""
import subprocess
from pathlib import Path
from types import SimpleNamespace
import pytest
from scripts import admin_runner_service as service


@pytest.mark.parametrize('foreign',[False,True])
def test_failed_start_rolls_back_only_created_service_and_sudoers(tmp_path,monkeypatch,foreign):
    from scripts import git_updates
    import time
    monkeypatch.setattr(service.os,'geteuid',lambda:1000)
    monkeypatch.setattr(service.pwd,'getpwuid',lambda uid:SimpleNamespace(pw_name='argon',pw_dir='/home/argon'))
    monkeypatch.setattr(git_updates,'checked_checkout',lambda root:root)
    real_dir=Path.is_dir;real_file=Path.is_file
    monkeypatch.setattr(Path,'is_dir',lambda path:True if str(path)=='/run/systemd/system' else real_dir(path))
    monkeypatch.setattr(Path,'is_file',lambda path:True if str(path)=='/usr/bin/docker' else real_file(path))
    monkeypatch.setattr(time,'sleep',lambda seconds:None)
    destination='/etc/systemd/system/'+service.unit_name(tmp_path/'recordings.toml')
    files={destination:'Foreign service content'} if foreign else {}
    original=dict(files);calls=[]
    def run(command,**kwargs):
        calls.append(command)
        args=command[2:] if command[:2]==['sudo','-n'] else command
        code=0
        if args[:2]==['test','-L']:code=1
        elif args[:2]==['test','-e']:code=0 if args[-1] in files else 1
        elif args[:1]==['stat']:return subprocess.CompletedProcess(command,0,stdout='0 644',stderr='')
        elif args[:1]==['cat']:return subprocess.CompletedProcess(command,0,stdout=files[args[-1]],stderr='')
        elif args[:1]==['install']:files[args[-1]]=Path(args[-2]).read_text()
        elif args[:1]==['cmp']:code=0 if files.get(args[-1])==Path(args[-2]).read_text() else 1
        elif args[:1]==['rm']:files.pop(args[-1],None)
        elif args[:2]==['systemctl','is-enabled']:code=1
        elif args[:2]==['systemctl','is-active']:raise subprocess.CalledProcessError(3,command)
        return subprocess.CompletedProcess(command,code,stdout='',stderr='')
    monkeypatch.setattr(service.subprocess,'run',run)
    installer=SimpleNamespace(ROOT=tmp_path,docker=lambda *args,**kwargs:None)
    with pytest.raises(ValueError if foreign else subprocess.CalledProcessError):service.setup(installer,{'home':tmp_path,'config_path':tmp_path/'recordings.toml','sudo':True})
    assert files==original
    if foreign:
        assert not any(c[2:3]==['install'] for c in calls)
        return
    assert any(c[2:4]==['systemctl','stop'] for c in calls)
    assert any(c[2:4]==['systemctl','disable'] for c in calls)
