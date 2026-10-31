"""Bounded snapshot/restore/retention smoke with synthetic private data, personal Docker only."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import uuid

from dev_container import docker_command
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import recordings
from scripts import update_backups as backups
from scripts.git_updates import git
from scripts.worker_stack import prefix


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image',required=True)
    args=parser.parse_args()
    docker=docker_command()
    session='argon-recordings-backup-smoke-'+uuid.uuid4().hex[:8]
    label='com.argon.recordings.fixture='+session
    containers=[];volumes=[]
    def run(*argv,**kw):
        return subprocess.run([*docker,*map(str,argv)],check=True,timeout=120,**kw)
    def utility(cfg,*argv,**kw):
        argv=list(map(str,argv))
        if argv[0]=='run':
            name=session+'-utility-'+uuid.uuid4().hex[:8];containers.append(name)
            argv[1:1]=['--name',name,'--label',label]
        return run(*argv,**kw)
    def inspect(cfg):
        return json.loads(run('inspect',cfg['name'],capture_output=True,text=True).stdout)[0]
    installer=SimpleNamespace(LABEL=recordings.LABEL,docker=utility,inspect=inspect)
    try:
        with tempfile.TemporaryDirectory(prefix=session) as temporary:
            for mode in ('coordinator','worker'):
                home=Path(temporary)/mode;home.mkdir()
                cfg=dict(home=home,config_path=home/'recordings.toml',identity=hashlib.sha256(str(home).encode()).hexdigest()[:16],
                         name='argon-backup-'+uuid.uuid4().hex[:8],mode=mode)
                (home/'.recordings').mkdir()
                (home/'.recordings/installation.json').write_text('{"fixture":true}')
                cfg['config_path'].write_text('[deployment]\nmode="'+mode+'"\n')
                (home/'.gitignore').write_text('.recordings/\nrecordings.toml\n')
                (home/'source.txt').write_text('synthetic source')
                git(home,'init','--initial-branch=main');git(home,'add','.')
                git(home,'-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit','-m','feat: fixture')
                revision=git(home,'rev-parse','HEAD').stdout.strip()
                destination='/data' if mode=='coordinator' else '/work'
                volume=prefix(cfg)+('-data' if mode=='coordinator' else '-work')
                run('volume','create','--label',label,volume,capture_output=True);volumes.append(volume)
                name=cfg['name'] if mode=='coordinator' else cfg['name']+'-worker-1'
                role='api' if mode=='coordinator' else 'worker'
                code=f"from pathlib import Path; import os; p=Path('{destination}/private'); p.mkdir(mode=0o700); f=p/'account-fixture'; f.write_text('synthetic-private-payload'); f.chmod(0o600)"
                if mode=='worker':
                    containers.append(cfg['name'])
                    run('create','--name',cfg['name'],'--label',label,'--label',recordings.LABEL+'='+cfg['identity'],
                        '--label','com.argon.recordings.role=transport','--network','none','--entrypoint','python',args.image,'-c','pass',capture_output=True)
                containers.append(name)
                run('create','--name',name,'--label',label,'--label',recordings.LABEL+'='+cfg['identity'],
                    '--label','com.argon.recordings.role='+role,'--network','none','--read-only','--cap-drop','ALL',
                    '--user','10001:10001','--cpus','1','--memory','128m','--memory-swap','128m',
                    '-v',volume+':'+destination,'--entrypoint','python',args.image,'-c',code,capture_output=True)
                run('start','-a',name,capture_output=True)
                layout=backups.plan(installer,cfg);backups.preflight(installer,cfg,layout)
                first=backups.snapshot(installer,cfg,layout,revision,home,git)
                second=backups.snapshot(installer,cfg,layout,revision,home,git)
                backups.prune(cfg,second)
                assert not first.exists() and second.exists()
                restored=volume+'-restored';run('volume','create','--label',label,restored,capture_output=True);volumes.append(restored)
                utility(cfg,'run','--rm','--network','none','--user','0:0','-v',restored+':/restore',
                        '--entrypoint','chown',args.image,'10001:10001','/restore',capture_output=True)
                archive=second/('data.tar' if mode=='coordinator' else 'work.tar')
                with archive.open('rb') as source:
                    utility(cfg,'run','--rm','-i','--network','none','--read-only','--cap-drop','ALL','--user','10001:10001',
                            '-v',restored+':/restore','--entrypoint','tar',args.image,'-C','/restore','-xpf','-',stdin=source,capture_output=True)
                check="from pathlib import Path; p=Path('/restore/private'); f=p/'account-fixture'; assert p.stat().st_mode&0o777==0o700; assert f.stat().st_uid==10001; assert f.stat().st_mode&0o777==0o600; assert f.read_text()=='synthetic-private-payload'"
                utility(cfg,'run','--rm','--network','none','--read-only','--cap-drop','ALL','--user','10001:10001',
                        '-v',restored+':/restore:ro','--entrypoint','python',args.image,'-c',check,capture_output=True)
                print('PASS:',mode,'private UID10001 data snapshot, comparison, restore and one-backup retention',flush=True)
    finally:
        for kind,names in (('container',containers),('volume',volumes)):
            for name in reversed(names):
                found=subprocess.run([*docker,kind,'inspect',name],capture_output=True,text=True,timeout=15)
                if found.returncode:continue
                item=json.loads(found.stdout)[0]
                labels=item['Config'].get('Labels',{}) if kind=='container' else item.get('Labels',{})
                if (labels or {}).get('com.argon.recordings.fixture')!=session:raise RuntimeError('Fixture ownership mismatch')
                run(*(['rm','-f',name] if kind=='container' else ['volume','rm',name]),capture_output=True)


if __name__=='__main__':main()
