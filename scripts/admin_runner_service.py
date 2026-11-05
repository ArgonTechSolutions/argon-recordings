"""Explicit owner-invoked systemd setup for the existing privileged updater."""
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import subprocess
import sys
import tempfile


def unit_name(root):
    return 'argon-recordings-updater-'+hashlib.sha256(str(Path(root).resolve()).encode()).hexdigest()[:12]+'.service'


def quote(value):
    value=str(value)
    if any(ord(c)<32 for c in value):raise ValueError('Control characters in service path')
    return json.dumps(value).replace('%','%%').replace('$','$$')


def definition(root,config,username,home):
    if not re.fullmatch(r'[a-z_][a-z0-9_-]*[$]?',username):raise ValueError('Unsupported account name')
    return f'''# Managed by Argon Recordings admin-runner setup
[Unit]
Description=Argon Recordings update runner
Wants=network-online.target
After=network-online.target docker.service
[Service]
Type=simple
User={username}
WorkingDirectory={quote(root)}
Environment="PATH=/usr/bin:/bin"
Environment={quote('HOME='+str(home))}
ExecStart={quote(sys.executable)} {quote(Path(root)/'recordings.py')} --config {quote(config)} admin-runner
Restart=on-failure
RestartSec=15
UMask=0077
TimeoutStopSec=300
[Install]
WantedBy=multi-user.target
'''


def setup(installer,config):
    if os.geteuid()==0:raise ValueError('Run as the nonroot installation owner, with sudo authenticated')
    account=pwd.getpwuid(os.geteuid())
    root=Path(installer.ROOT).resolve()
    from scripts.git_updates import checked_checkout
    checked_checkout(root)
    if not Path('/run/systemd/system').is_dir():raise ValueError('A systemd host is required')
    # Do not interrupt a claimed update or an already-running foreground runner.
    import fcntl
    folder=config['home']/'.recordings'/'admin-runner'
    folder.mkdir(parents=True,exist_ok=True,mode=0o700)
    if (folder/'receipt.json').exists():raise ValueError('Inspect retained update receipt before service setup')
    with (folder/'runner.lock').open('a') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise ValueError('Stop the idle foreground runner with Ctrl+C before setup') from None
        name=unit_name(config['config_path'])
        marker=f'# Argon Recordings managed updater {name} uid={os.geteuid()}\n'
        unit=marker+definition(root,config['config_path'],account.pw_name,account.pw_dir)
        print('Installing',name,'as',account.pw_name,flush=True)
        def sudo(*args,check=True):
            return subprocess.run(['sudo','-n',*args],check=check,capture_output=True,text=True)
        def existing(path):
            if sudo('test','-L',path,check=False).returncode==0:raise ValueError('Refusing service artifact symlink')
            found=sudo('test','-e',path,check=False)
            if found.returncode==1:return None
            found.check_returncode()
            uid,mode=sudo('stat','-c','%u %a',path).stdout.split()
            if uid!='0' or int(mode,8)&0o022:raise ValueError('Unsafe service artifact ownership')
            value=sudo('cat',path).stdout
            if not value.startswith(marker):raise ValueError('Existing service artifact belongs to another setup')
            return value
        installer.docker(config,'info',capture_output=True,text=True)
        with tempfile.TemporaryDirectory(prefix='argon-updater-service-') as temp:
            path=Path(temp)/name;path.write_text(unit)
            subprocess.run(['systemd-analyze','verify',str(path)],check=True)
            artifacts=[(path,'/etc/systemd/system/'+name,'0644')]
            if config['sudo']:
                if not Path('/usr/bin/docker').is_file():raise ValueError('Expected /usr/bin/docker')
                rules=Path(temp)/'sudoers'
                rules.write_text(marker+'# Root-equivalent Docker access for the explicitly enabled updater\n'+account.pw_name+' ALL=(root) NOPASSWD: /usr/bin/docker\n')
                sudo('/usr/sbin/visudo','-cf',str(rules))
                artifacts.insert(0,(rules,'/etc/sudoers.d/'+name[:-8],'0440'))
            previous={destination:existing(destination) for _,destination,_ in artifacts}
            was_enabled=sudo('systemctl','is-enabled',name,check=False).returncode==0
            written=[]
            try:
                for staged,destination,mode in artifacts:
                    sudo('install','-o','root','-g','root','-m',mode,str(staged),destination)
                    written.append((staged,destination,mode))
                sudo('systemctl','daemon-reload')
                # Release the shared foreground lock before starting the daemon.
                fcntl.flock(lock,fcntl.LOCK_UN)
                sudo('systemctl','enable','--now',name)
                time_wait=__import__('time');time_wait.sleep(2)
                sudo('systemctl','is-active',name)
            except BaseException:
                sudo('systemctl','stop',name,check=False)
                if not was_enabled:sudo('systemctl','disable',name,check=False)
                for staged,destination,mode in reversed(written):
                    # Never remove or overwrite a file changed by somebody else.
                    if sudo('cmp','-s',str(staged),destination,check=False).returncode!=0:continue
                    old=previous[destination]
                    if old is None:sudo('rm','--',destination)
                    else:
                        restore=Path(temp)/'restore';restore.write_text(old)
                        sudo('install','-o','root','-g','root','-m',mode,str(restore),destination)
                sudo('systemctl','daemon-reload',check=False)
                raise
    print('Updater service enabled at boot. Terminal may now be closed. Unit:',name)
