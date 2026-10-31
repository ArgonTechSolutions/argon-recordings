"""Private, role-aware pre-update snapshots. Never backs up models or RAM credentials."""
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import uuid

from scripts.worker_stack import prefix, stack_items

MANAGED = re.compile(r'update-\d{8}T\d{6}Z-[a-f0-9]{8}')
PARTIAL = re.compile(r'\.update-\d{8}T\d{6}Z-[a-f0-9]{8}\.partial')
LEGACY = re.compile(r'remote-inference-\d{8}T\d{6}Z')


def safe_directory(path):
    if path.resolve() != path.absolute():
        raise ValueError('Backup directory must not contain symlinks')
    if path.exists() and not path.is_dir():
        raise ValueError('Backup directory must be a directory')
    missing=[]
    ancestor=path
    while not ancestor.exists():
        missing.append(ancestor);ancestor=ancestor.parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o700,exist_ok=True)
        sync_path(directory.parent,directory=True)
    info=path.stat()
    if info.st_uid!=os.geteuid() or info.st_mode & 0o022:
        raise ValueError('Backup/update directory must belong to the installation user and not be group/world writable')


def write(path, data):
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'wb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())



def sync_path(path,directory=False):
    flags=os.O_RDONLY|os.O_NOFOLLOW|(os.O_DIRECTORY if directory else 0)
    fd=os.open(path,flags)
    try:os.fsync(fd)
    finally:os.close(fd)


def digest(path):
    if path.is_symlink() or not path.is_file():
        raise ValueError('Backup payload must be a regular file')
    result=hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda:stream.read(1024**2),b''):
            result.update(block)
    return result.hexdigest()


@contextmanager
def update_lock(config,blocking=False):
    folder=config['home']/'.recordings'
    safe_directory(folder)
    path=folder/'update.lock'
    inherited=os.environ.get('RECORDINGS_UPDATE_LOCK_FD')
    borrowed=False
    if inherited is not None:
        try:
            fd=int(inherited)
            actual=os.fstat(fd);expected=path.stat()
            if fd<3 or (actual.st_dev,actual.st_ino)!=(expected.st_dev,expected.st_ino):
                raise ValueError('Invalid inherited update lock')
            borrowed=True
        except (OSError,ValueError) as exc:raise ValueError('Invalid inherited update lock') from exc
    else:
        fd=os.open(path,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    try:
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.geteuid() or info.st_mode & 0o077:
            raise ValueError('Invalid private update lock')
        try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError as exc:
            if not blocking:raise ValueError('An update is already in progress') from exc
            print('An update or lifecycle operation is in progress; waiting...',flush=True)
            fcntl.flock(fd,fcntl.LOCK_EX)
        yield fd
    finally:
        if not borrowed:os.close(fd)


def plan(installer, config):
    members=stack_items(installer,config)
    if not members:
        raise ValueError('No installed containers to back up; run install/start first')
    volumes=[]
    for item in members:
        labels=item['Config'].get('Labels') or {}
        if labels.get(installer.LABEL)!=config['identity']:
            raise ValueError('Backup container belongs to a different installation')
        for mount in item['Mounts']:
            location=mount['Destination']
            if location not in {'/data','/work'}:continue
            if location=='/data':
                expected=prefix(config)+'-data';filename='data.tar'
            else:
                match=re.fullmatch(re.escape(config['name'])+r'-worker-([1-8])',item['Name'].lstrip('/'))
                if not match:raise ValueError('Unexpected worker workspace owner')
                index=int(match[1]);suffix='' if index==1 else '-'+str(index)
                expected=prefix(config)+'-work'+suffix;filename='work'+suffix+'.tar'
            if mount['Type']!='volume' or mount.get('Name')!=expected:
                raise ValueError('Backup requires the expected named data/work volumes')
            volumes.append(dict(volume=expected,archive=filename,image=item['Image']))
    names={v['archive'] for v in volumes}
    required={'data.tar'} if config['mode']=='coordinator' else {'work.tar'} if config['mode']=='worker' else {'data.tar','work.tar'}
    if not required<=names or len(names)!=len(volumes):
        raise ValueError('Existing containers do not match the configured deployment backup layout')
    running=[dict(id=item['Id'],name=item['Name'],role=item['Config']['Labels']['com.argon.recordings.role']) for item in members if item['State']['Running']]
    return dict(volumes=volumes,was_running=bool(running),running=running)



def restore_running(installer,config,layout):
    failures=[]
    for before in layout['running']:
        try:
            item=json.loads(installer.docker(config,'inspect',before['id'],capture_output=True,text=True).stdout)[0]
            labels=item['Config'].get('Labels') or {}
            if item['Id']!=before['id'] or item['Name']!=before['name'] or labels.get(installer.LABEL)!=config['identity'] or labels.get('com.argon.recordings.role')!=before['role']:
                raise ValueError('Original container identity changed')
            installer.docker(config,'start',before['id'])
        except Exception as exc:
            failures.append(type(exc).__name__)
    if failures:
        raise RuntimeError('Pre-update backup/stop failed; some original containers could not be restarted. Source and backups are retained; inspect installation status.')


def utility(installer,config,volume,*args,**kwargs):
    return installer.docker(config,'run','--rm','--network','none','--read-only','--user','10001:10001',
                            '--cap-drop','ALL','--cpus','1','--memory','256m','--memory-swap','256m',
                            '--pids-limit','64','--mount','type=volume,src='+volume['volume']+',dst=/source,readonly',
                            *args,**kwargs)


def preflight(installer,config,layout):
    root=config['home']/'.recordings/backups'
    safe_directory(root)
    total=0
    code="import os\ndef fail(error): raise error\nprint(sum(os.lstat(os.path.join(p,n)).st_size for p,ds,fs in os.walk('/source',onerror=fail) for n in fs))"
    for volume in layout['volumes']:
        result=utility(installer,config,volume,'--entrypoint','python',volume['image'],'-c',code,capture_output=True,text=True)
        total+=int(result.stdout.strip())
    required=total+max(64*1024**2,total//20)
    if shutil.disk_usage(root).free<required:
        raise ValueError('Insufficient free space for a new backup while preserving the previous one')


def snapshot(installer,config,layout,revision,git_root,git):
    if any(item['State']['Running'] for item in stack_items(installer,config)):
        raise ValueError('All installation containers must be stopped before snapshotting')
    root=config['home']/'.recordings/backups'
    safe_directory(root)
    name='update-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid.uuid4().hex[:8]
    stage=root/('.'+name+'.partial');stage.mkdir(mode=0o700)
    metadata=dict(version=1,identity=config['identity'],mode=config['mode'],revision=revision,
                  verified=False,volumes=layout['volumes'],files={})
    write(stage/'backup.json',json.dumps(metadata,sort_keys=True).encode())
    for source,filename in [(config['config_path'],'recordings.toml'),(config['home']/'.recordings/installation.json','installation.json')]:
        if source.is_symlink() or not source.is_file():raise ValueError('Missing regular installation/config file')
        write(stage/filename,source.read_bytes())
    git(git_root,'bundle','create',str(stage/'source.bundle'),'HEAD')
    (stage/'source.bundle').chmod(0o600)
    git(git_root,'bundle','verify',str(stage/'source.bundle'))
    for volume in layout['volumes']:
        archive=stage/volume['archive']
        fd=os.open(archive,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'wb') as output:
            utility(installer,config,volume,'--entrypoint','tar',volume['image'],'-C','/source','-cpf','-','.',stdout=output,stderr=subprocess.PIPE)
            output.flush();os.fsync(output.fileno())
        with archive.open('rb') as source:
            utility(installer,config,volume,'-i','--entrypoint','tar',volume['image'],'-C','/source','-df','-',stdin=source,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
    for path in stage.iterdir():sync_path(path)
    metadata['files']={path.name:digest(path) for path in stage.iterdir() if path.name!='backup.json'}
    metadata['verified']=True
    temporary=stage/'backup.new'
    write(temporary,json.dumps(metadata,sort_keys=True,indent=2).encode()+b'\n')
    temporary.replace(stage/'backup.json')
    sync_path(stage,directory=True)
    final=root/name
    stage.rename(final)
    sync_path(root,directory=True)
    print('Verified pre-update backup:',final)
    return final


def verified(folder,config):
    if folder.resolve()!=folder.absolute() or not folder.is_dir() or folder.stat().st_uid!=os.geteuid() or folder.stat().st_mode & 0o022:
        raise ValueError('Unsafe managed backup path')
    manifest=folder/'backup.json'
    if manifest.is_symlink() or not manifest.is_file() or manifest.stat().st_size>65536:
        raise ValueError('Missing managed backup manifest')
    data=json.loads(manifest.read_text())
    if data.get('version')!=1 or data.get('identity')!=config['identity'] or data.get('mode')!=config['mode'] or data.get('verified') is not True:
        raise ValueError('Backup identity, role or verification mismatch')
    files=data.get('files',{})
    names={'recordings.toml','installation.json','source.bundle'}|{v['archive'] for v in data['volumes']}
    if set(files)!=names or {p.name for p in folder.iterdir()}!=names|{'backup.json'}:
        raise ValueError('Unexpected managed backup contents')
    for name,value in files.items():
        if not re.fullmatch(r'(?:data|work(?:-[2-8])?)\.tar|recordings\.toml|installation\.json|source\.bundle',name) or digest(folder/name)!=value:
            raise ValueError('Backup checksum mismatch')
    return data


def prune(config,keep):
    """Prune only after successful update, rechecking the kept archive checksums."""
    data=verified(keep,config)
    volumes={v['volume'] for v in data['volumes']}
    removed=0
    for folder in keep.parent.iterdir():
        if folder==keep or folder.is_symlink() or not folder.is_dir():continue
        if folder.stat().st_uid!=os.geteuid() or folder.stat().st_mode & 0o022:continue
        safe=False
        try:
            if MANAGED.fullmatch(folder.name):
                older=verified(folder,config)
                safe={v['volume'] for v in older['volumes']}<=volumes
            elif PARTIAL.fullmatch(folder.name):
                marker=folder/'backup.json'
                if marker.is_symlink() or not marker.is_file() or marker.stat().st_size>65536:continue
                partial=json.loads(marker.read_text())
                if partial.get('version')!=1 or partial.get('identity')!=config['identity'] or partial.get('mode')!=config['mode']:continue
                archived={v['archive'] for v in partial['volumes']}
                if not archived or any(not re.fullmatch(r'(?:data|work(?:-[2-8])?)\.tar',name) for name in archived):continue
                if not {v['volume'] for v in partial['volumes']}<=volumes:continue
                allowed=archived|{'backup.json','backup.new','recordings.toml','installation.json','source.bundle'}
                safe=all(not p.is_symlink() and p.is_file() and p.name in allowed for p in folder.iterdir())
            elif LEGACY.fullmatch(folder.name):
                filename='work.tar' if config['mode']=='worker' else 'data.tar'
                allowed={filename,'recordings.toml','installation.json','revision.txt','volume.txt','image.txt','SHA256SUMS',filename+'.part'}
                contents=list(folder.iterdir())
                if any(p.is_symlink() or not p.is_file() or p.name not in allowed for p in contents):continue
                if (folder/'volume.txt').read_text().strip() not in volumes:continue
                fields=(folder/'SHA256SUMS').read_text().split()
                safe=len(fields)==2 and fields[1]==filename and digest(folder/filename)==fields[0]
                partial=folder/(filename+'.part')
                if partial.exists() and partial.stat().st_size!=0:safe=False
        except (OSError,ValueError,KeyError,TypeError):
            continue  # Unknown, damaged or foreign backups require explicit operator action.
        if safe:
            removed+=sum(p.stat().st_blocks*512 for p in folder.iterdir())
            shutil.rmtree(folder)
            print('Removed older managed backup:',folder.name)
    sync_path(keep.parent,directory=True)
    print(f'Backup retention complete; approximately {removed/1024**3:.2f} GiB removed. Keeping: {keep}')
