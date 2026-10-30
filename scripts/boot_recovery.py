"""Opt-in rootful systemd recovery for a private listener's pre-process bind failure.

The installed helper is a root-owned stdlib-only snapshot: it never imports the
user-writable checkout, reads secrets, creates containers, or edits bindings.
"""
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
from datetime import datetime, timezone

LABEL = 'com.argon.recordings.installation'
ROLE = 'com.argon.recordings.role'
DOCKER = ['/usr/bin/docker', '--host', 'unix:///var/run/docker.sock']


def command(args):
    try:
        return subprocess.run(args, check=True, capture_output=True, text=True, timeout=30)
    except subprocess.CalledProcessError as exc:
        raise RuntimeError((exc.stderr or 'Boot recovery command failed').strip()[-1500:]) from exc


def validate_manifest(value):
    if set(value) != {'version','identity','api','transport','address','port','uid'} or value['version'] != 1:
        raise ValueError('Unsupported recovery manifest')
    if not re.fullmatch(r'[a-f0-9]{16}', value['identity']):
        raise ValueError('Invalid installation identity')
    if not re.fullmatch(r'[a-z][a-z0-9-]{0,39}', value['api']) or value['transport'] != value['api']+'-transport':
        raise ValueError('Invalid installation names')
    address = ipaddress.ip_address(value['address'])
    if address.version != 4 or address not in ipaddress.ip_network('100.64.0.0/10'):
        raise ValueError('Boot recovery requires a private Tailscale IPv4 binding')
    if type(value['port']) is not int or not 1024 <= value['port'] <= 65535:
        raise ValueError('Invalid private listener port')
    if type(value['uid']) is not int or value['uid'] <= 0:
        raise ValueError('Set up boot recovery as the nonroot installation owner using sudo')
    return value


def lock_path(identity):
    if not re.fullmatch(r'[a-f0-9]{16}',identity):
        raise ValueError('Invalid lock identity')
    return Path('/var/lib/argon-recordings/boot')/identity/'lifecycle.lock'


@contextmanager
def lifecycle_lock(value, required=False):
    path=lock_path(value['identity'])
    try:
        fd=os.open(path,os.O_RDWR|os.O_NOFOLLOW)
    except FileNotFoundError:
        if required:raise
        yield
        return
    try:
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != value.get('uid',os.geteuid()) or stat.S_IMODE(info.st_mode)!=0o600:
            raise ValueError('Unsafe lifecycle lock')
        fcntl.flock(fd,fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def validate_container(value, item, role):
    expected = value['api'] if role == 'api' else value['transport']
    labels = item['Config'].get('Labels') or {}
    if item['Name'] != '/'+expected or labels.get(LABEL) != value['identity'] or labels.get(ROLE) != role:
        raise ValueError('Container does not belong to the expected installation and role')
    if role == 'transport':
        expected_binding = {'8443/tcp':[{'HostIp':value['address'],'HostPort':str(value['port'])}]}
        if item['HostConfig'].get('PortBindings') != expected_binding:
            raise ValueError('Private listener binding differs from the installed manifest')
        if item['HostConfig'].get('RestartPolicy',{}).get('Name') != 'unless-stopped':
            raise ValueError('Unexpected transport restart policy')


def recovery_required(value, api, transport):
    validate_manifest(value)
    validate_container(value, api, 'api')
    validate_container(value, transport, 'transport')
    if not api['State']['Running'] or transport['State']['Running']:
        return False
    error = transport['State'].get('Error','')
    # Ordinary operator stops and runtime failures are left to the operator and
    # Docker restart manager. Only repair this known pre-process bind failure.
    return 'failed to bind host port' in error and 'cannot assign requested address' in error


def inspect(name):
    result = subprocess.run([*DOCKER,'inspect','--type','container',name],
                            capture_output=True,text=True,timeout=30)
    if result.returncode:
        if 'No such container' in result.stderr or 'No such object' in result.stderr:
            return None  # Deleted during an update; never recreate it here.
        raise RuntimeError('Docker inspection unavailable')
    return json.loads(result.stdout)[0]


def recover(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022 or info.st_size > 4096:
        raise ValueError('Recovery manifest must be a small root-owned, non-writable regular file')
    value = validate_manifest(json.loads(path.read_text()))
    with lifecycle_lock(value,required=True):
        api, transport = inspect(value['api']), inspect(value['transport'])
        if api is None or transport is None:
            print('Installation container absent; leaving stopped installation unchanged')
            return
        if not recovery_required(value, api, transport):
            print('No private bind recovery required')
            return
        addresses = json.loads(command(['/usr/sbin/ip','-j','-4','address','show']).stdout)
        if not any(item.get('local') == value['address'] for link in addresses for item in link.get('addr_info',[])):
            raise RuntimeError('Waiting for the configured private address')
        # Revalidate immediately before acting; never change arguments, volumes or IPs.
        api, transport = inspect(value['api']), inspect(value['transport'])
        if api is None or transport is None or not recovery_required(value, api, transport):
            return
        command([*DOCKER,'start',transport['Id']])
        if not inspect(transport['Id'])['State']['Running']:
            raise RuntimeError('Private transport did not start; retry required')
        print('Recovered owned private transport')

def unit_text(value, generation):
    return f'''[Unit]
Description=Argon Recordings private transport boot recovery ({value['identity']})
Wants=docker.service tailscaled.service network-online.target
After=docker.service tailscaled.service network-online.target
StartLimitIntervalSec=0

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/bin/python3 -I -S {generation}/helper.py --recover {generation}/manifest.json
Restart=on-failure
RestartSec=5s
TimeoutStartSec=120s
UMask=0077

[Install]
WantedBy=multi-user.target
'''


def trusted_directory(path, root):
    """Never install root-executed code below a user-writable ancestor."""
    for directory in reversed((path, *path.parents)):
        if not directory.exists():
            command([*root,'/usr/bin/install','-d','-o','root','-g','root','-m','755',str(directory)])
        info=directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError('Unsafe root helper directory: '+str(directory))


def publish_unit(root, staging, unit, target, backup, previous, was_enabled, was_active):
    candidate=target.with_suffix('.service.new')
    try:
        command([*root,'/usr/bin/install','-o','root','-g','root','-m','644',str(staging/unit),str(candidate)])
        command([*root,'/usr/bin/mv','-T',str(candidate),str(target)])
        command([*root,'/usr/bin/systemctl','daemon-reload'])
        command([*root,'/usr/bin/systemctl','enable',unit])
        command([*root,'/usr/bin/systemctl','start','--no-block',unit])
    except BaseException as original:
        failures=[]
        def attempt(args):
            try:
                command(args)
                return True
            except (OSError,subprocess.SubprocessError,RuntimeError) as exc:
                failures.append(type(exc).__name__)
                return False
        # Continue restoring files even when systemd/DBus is temporarily unavailable.
        attempt([*root,'/usr/bin/systemctl','stop',unit])
        attempt([*root,'/usr/bin/systemctl','disable',unit])
        if previous:
            if attempt([*root,'/usr/bin/install','-o','root','-g','root','-m','644',str(backup/'previous.service'),str(candidate)]):
                attempt([*root,'/usr/bin/mv','-T',str(candidate),str(target)])
        else:
            attempt([*root,'/usr/bin/rm','-f','--',str(target)])
        attempt([*root,'/usr/bin/rm','-f','--',str(candidate)])
        attempt([*root,'/usr/bin/systemctl','daemon-reload'])
        attempt([*root,'/usr/bin/systemctl','enable' if was_enabled else 'disable',unit])
        if was_active:attempt([*root,'/usr/bin/systemctl','start','--no-block',unit])
        if failures:
            raise RuntimeError(f'Boot setup failed; rollback incomplete ({len(failures)} errors). Backup: {backup}') from original
        raise


def setup(installer, config):
    if config['mode'] != 'coordinator':
        raise ValueError('Boot recovery is only required on the coordinator')
    value = validate_manifest(dict(version=1,identity=config['identity'],api=config['name'],
                                   transport=config['name']+'-transport',
                                   address=config['transport']['bind_address'],port=config['transport']['port'],uid=os.geteuid()))
    security = json.loads(installer.docker(config,'info','--format','{{json .SecurityOptions}}',capture_output=True,text=True).stdout)
    if any('rootless' in option for option in security):
        raise ValueError('This boot integration supports the rootful system Docker daemon only')
    # The root helper always uses the system socket; never target a different daemon.
    root = [] if os.geteuid() == 0 else ['sudo','-n']
    for role in ('api','transport'):
        name=value[role]
        item=json.loads(command([*root,*DOCKER,'inspect',name]).stdout)[0]
        validate_container(value,item,role)
    helper=Path(__file__).read_bytes()
    manifest=(json.dumps(value,sort_keys=True,indent=2)+'\n').encode()
    digest=hashlib.sha256(helper+manifest).hexdigest()[:16]
    generation=Path('/usr/local/lib/argon-recordings/boot')/value['identity']/digest
    unit='argon-recordings-'+value['identity']+'-boot-recovery.service'
    target=Path('/etc/systemd/system')/unit
    staging=Path(tempfile.mkdtemp(prefix='argon-recordings-boot-'))
    backup=Path('/var/backups/argon-recordings')/('transport-boot-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
    try:
        (staging/'helper.py').write_bytes(helper)
        (staging/'manifest.json').write_bytes(manifest)
        (staging/unit).write_text(unit_text(value,generation))
        # Verify a complete unit before any system configuration changes.
        command([*root,'/usr/bin/systemd-analyze','verify',str(staging/unit)])
        for service in ('docker.service','tailscaled.service'):
            if command([*root,'/usr/bin/systemctl','show',service,'-p','LoadState','--value']).stdout.strip() != 'loaded':
                raise ValueError('Required system service missing: '+service)
        trusted_directory(backup.parent,root)
        command([*root,'/usr/bin/install','-d','-m','700',str(backup)])
        trusted_directory(target.parent,root)
        if target.is_symlink():
            raise ValueError('Refusing to replace a symlink unit')
        previous=target.exists()
        if previous:
            info=target.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid!=0 or info.st_mode & 0o022:
                raise ValueError('Existing recovery unit has unsafe ownership or permissions')
        state=subprocess.run([*root,'/usr/bin/systemctl','is-enabled',unit],capture_output=True,text=True,timeout=30).stdout.strip()
        if state not in {'enabled','disabled','not-found',''}:
            raise ValueError('Unsupported existing boot-unit enablement: '+state)
        was_enabled=state=='enabled'
        active=subprocess.run([*root,'/usr/bin/systemctl','is-active',unit],capture_output=True,text=True,timeout=30).stdout.strip()
        if active not in {'active','inactive','failed','unknown',''}:
            raise ValueError('Boot recovery is busy; retry setup after its current attempt')
        was_active=active=='active'
        if previous:
            command([*root,'/usr/bin/cp','-a',str(target),str(backup/'previous.service')])
        trusted_directory(generation,root)
        lock=lock_path(value['identity'])
        trusted_directory(lock.parent,root)
        if not os.access(lock.parent,os.X_OK):
            raise ValueError('Lifecycle lock directory must be traversable by the installation owner')
        create_lock="import os,sys\ntry: fd=os.open(sys.argv[1],os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o600)\nexcept FileExistsError: pass\nelse:\n os.fchown(fd,int(sys.argv[2]),0); os.fchmod(fd,0o600); os.close(fd)"
        command([*root,'/usr/bin/python3','-I','-S','-c',create_lock,str(lock),str(value['uid'])])
        with lifecycle_lock(value,required=True):
            for name in ('helper.py','manifest.json'):
                candidate=generation/(name+'.new')
                command([*root,'/usr/bin/install','-o','root','-g','root','-m','644',str(staging/name),str(candidate)])
                command([*root,'/usr/bin/mv','-T',str(candidate),str(generation/name)])
            publish_unit(root,staging,unit,target,backup,previous,was_enabled,was_active)
        print('Boot recovery enabled:',unit)
        print('Previous-unit backup:',backup)
        print('No data, model, network or Caddy settings changed.')
    finally:
        import shutil
        shutil.rmtree(staging)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--recover',type=Path,required=True)
    args=parser.parse_args()
    try:
        recover(args.recover)
    except (OSError,ValueError,TypeError,KeyError,RuntimeError,subprocess.SubprocessError) as exc:
        # Keep Docker's complete inspect metadata and environment out of journald.
        print('Private transport recovery pending:',type(exc).__name__)
        raise SystemExit(1)


if __name__ == '__main__':
    main()
