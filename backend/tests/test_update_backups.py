"""Archive comparison and retention over synthetic data and real disposable Git."""
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from scripts import update_backups as backups
from scripts.git_updates import git
from scripts.worker_stack import prefix


@pytest.fixture
def fixture(tmp_path,monkeypatch):
    home=tmp_path/'app';home.mkdir()
    (home/'recordings.toml').write_text('[deployment]\nmode="coordinator"\n')
    (home/'.recordings').mkdir()
    (home/'.recordings/installation.json').write_text('{"image":"fixture"}')
    (home/'.gitignore').write_text('.recordings/\nrecordings.toml\n')
    (home/'source.txt').write_text('retained source')
    git(home,'init','--initial-branch=main')
    git(home,'add','.')
    git(home,'-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit','-m','feat: synthetic fixture')
    cfg=dict(home=home,config_path=home/'recordings.toml',identity='a'*16,name='fixture',mode='coordinator')
    volume=tmp_path/'volume';volume.mkdir()
    private=volume/'private';private.mkdir(mode=0o700)
    (private/'accounts').write_text('synthetic account and MFA bytes');(private/'accounts').chmod(0o600)
    member=dict(Id='immutable-api',Name='/fixture',Config=dict(Image='fixture',Labels={backups.__dict__.get('LABEL','com.argon.recordings.installation'):cfg['identity'],'com.argon.recordings.role':'api'}),Image='sha256:fixture',State={'Running':False},Mounts=[dict(Destination='/data',Type='volume',Name=prefix(cfg)+'-data')])
    monkeypatch.setattr(backups,'stack_items',lambda *args:[member])
    calls=[]
    corrupt=[False]
    def docker(config,*args,**kwargs):
        calls.append(args)
        assert args[0]=='run' and '--read-only' in args and '--network' in args
        assert args[args.index('--user')+1]=='10001:10001'
        assert '--cap-add' not in args
        entry=args[args.index('--entrypoint')+1]
        if entry=='python':
            return SimpleNamespace(stdout=str(sum(p.stat().st_size for p in volume.rglob('*') if p.is_file())))
        assert entry=='tar'
        position=args.index('sha256:fixture')
        command=list(args[position+1:]);command[command.index('/source')]=str(volume)
        if '-df' in command and corrupt[0]:(private/'accounts').write_text('changed after archive creation')
        return subprocess.run(['tar',*command],check=True,**kwargs)
    installer=SimpleNamespace(LABEL='com.argon.recordings.installation',docker=docker)
    revision=git(home,'rev-parse','HEAD').stdout.strip()
    return SimpleNamespace(cfg=cfg,volume=volume,member=member,installer=installer,revision=revision,calls=calls,corrupt=corrupt)


def snapshot(f):
    layout=backups.plan(f.installer,f.cfg)
    backups.preflight(f.installer,f.cfg,layout)
    return backups.snapshot(f.installer,f.cfg,layout,f.revision,f.cfg['home'],git)


def test_real_tar_comparison_source_bundle_and_single_retention(fixture):
    f=fixture
    first=snapshot(f)
    second=snapshot(f)
    untouched=second.parent/'operator-notes';untouched.mkdir();(untouched/'keep').touch()
    data=backups.verified(second,f.cfg)
    assert set(data['files'])=={'recordings.toml','installation.json','source.bundle','data.tar'}
    assert second.stat().st_mode&0o777==0o700
    assert all(path.stat().st_mode&0o077==0 for path in second.iterdir())
    backups.prune(f.cfg,second)
    assert not first.exists() and untouched.exists() and second.exists()
    assert (f.volume/'private/accounts').read_text()=='synthetic account and MFA bytes'


def test_bad_new_checksum_never_deletes_previous_backup(fixture):
    f=fixture;first=snapshot(f);second=snapshot(f)
    with (second/'data.tar').open('ab') as stream:stream.write(b'corruption')
    with pytest.raises(ValueError,match='checksum'):
        backups.prune(f.cfg,second)
    assert first.is_dir()


def test_volume_mutation_or_running_writer_prevents_verified_publication(fixture):
    f=fixture;first=snapshot(f)
    f.member['State']['Running']=True
    with pytest.raises(ValueError,match='stopped'):snapshot(f)
    f.member['State']['Running']=False;f.corrupt[0]=True
    with pytest.raises(subprocess.CalledProcessError):snapshot(f)
    assert first.is_dir()
    assert len(list(first.parent.glob('update-*')))==1


def test_wrong_installation_and_symlink_backups_are_retained(fixture,tmp_path):
    f=fixture;first=snapshot(f);second=snapshot(f)
    data=json.loads((first/'backup.json').read_text());data['identity']='b'*16
    (first/'backup.json').write_text(json.dumps(data))
    target=tmp_path/'external';target.mkdir();(target/'keep').touch()
    link=second.parent/'update-20200101T000000Z-aaaaaaaa';link.symlink_to(target,target_is_directory=True)
    backups.prune(f.cfg,second)
    assert first.exists() and link.is_symlink() and (target/'keep').exists()


def test_directory_sync_failure_keeps_prior_backup(fixture,monkeypatch):
    f=fixture;first=snapshot(f)
    original=backups.sync_path
    def fail(path,directory=False):
        if path==first.parent and directory:raise OSError('injected directory fsync failure')
        original(path,directory)
    monkeypatch.setattr(backups,'sync_path',fail)
    with pytest.raises(OSError,match='fsync'):snapshot(f)
    assert first.is_dir()


def test_restore_only_original_running_container_ids(fixture,monkeypatch):
    f=fixture;calls=[]
    f.member['State']['Running']=True
    layout=backups.plan(f.installer,f.cfg)
    def docker(cfg,*args,**kwargs):
        calls.append(args)
        if args[0]=='inspect':return SimpleNamespace(stdout=json.dumps([f.member]))
        return SimpleNamespace(stdout='')
    f.installer.docker=docker
    backups.restore_running(f.installer,f.cfg,layout)
    assert ('start','immutable-api') in calls
    calls.clear();layout['running']=[]
    backups.restore_running(f.installer,f.cfg,layout)
    assert not calls



def test_success_cleans_owned_failed_partial_but_not_unknown_folders(fixture):
    f=fixture
    first=snapshot(f)
    f.corrupt[0]=True
    with pytest.raises(subprocess.CalledProcessError):snapshot(f)
    partials=list(first.parent.glob('.update-*.partial'))
    assert len(partials)==1
    f.corrupt[0]=False
    newest=snapshot(f)
    backups.prune(f.cfg,newest)
    assert not partials[0].exists() and newest.exists()


def test_recognized_legacy_backup_is_pruned_after_verified_replacement(fixture):
    import shutil
    f=fixture;newest=snapshot(f)
    legacy=newest.parent/'remote-inference-20200101T000000Z';legacy.mkdir(mode=0o700)
    shutil.copyfile(newest/'data.tar',legacy/'data.tar')
    (legacy/'volume.txt').write_text(prefix(f.cfg)+'-data')
    (legacy/'SHA256SUMS').write_text(backups.digest(legacy/'data.tar')+'  data.tar\n')
    backups.prune(f.cfg,newest)
    assert not legacy.exists() and newest.exists()


def test_child_installer_inherits_same_lock_without_deadlock(fixture):
    import os
    f=fixture
    code="from pathlib import Path; import sys; from scripts.update_backups import update_lock; c=update_lock({'home':Path(sys.argv[1])},blocking=True); c.__enter__(); print('inherited'); c.__exit__(None,None,None)"
    with backups.update_lock(f.cfg) as fd:
        env=dict(os.environ,RECORDINGS_UPDATE_LOCK_FD=str(fd))
        result=subprocess.run([sys.executable,'-c',code,str(f.cfg['home'])],env=env,pass_fds=(fd,),capture_output=True,text=True,timeout=5,check=True)
        assert result.stdout.strip()=='inherited'
        env.pop('RECORDINGS_UPDATE_LOCK_FD')
        probe="from pathlib import Path; import sys; from scripts.update_backups import update_lock; c=update_lock({'home':Path(sys.argv[1])}); c.__enter__()"
        refused=subprocess.run([sys.executable,'-c',probe,str(f.cfg['home'])],env=env,capture_output=True,text=True,timeout=5)
        assert refused.returncode!=0 and 'already in progress' in refused.stderr


def test_manual_stop_queues_and_runs_after_update_lock_is_released(fixture):
    import os
    import time
    f=fixture
    marker=f.cfg['home']/'.recordings/manual-stop-completed'
    code="import sys; from pathlib import Path; import recordings; recordings._stop=lambda c:Path(sys.argv[2]).write_text('stopped'); recordings.stop({'home':Path(sys.argv[1]),'identity':'a'*16})"
    env=dict(os.environ);env.pop('RECORDINGS_UPDATE_LOCK_FD',None)
    process=None
    try:
        with backups.update_lock(f.cfg):
            process=subprocess.Popen([sys.executable,'-c',code,str(f.cfg['home']),str(marker)],env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
            time.sleep(.15)
            assert process.poll() is None and not marker.exists()
        output,error=process.communicate(timeout=5)
        assert process.returncode==0,error
        assert marker.read_text()=='stopped'
    finally:
        if process is not None and process.poll() is None:
            process.terminate();process.wait(timeout=5)
