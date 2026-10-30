"""Split-host lifecycle. No production network, certificate or data provisioning."""
import ipaddress
import json
import os
import socket
import ssl
import stat
import time
from urllib.parse import urlsplit
import urllib.request

from scripts.worker_stack import (ROLE, api_args, common, credential_volume, prefix,
                                 prepare_credentials, prepare_network, stack_items, stop_stack, worker_args)


def configuration(data, home, mode):
    if mode not in {'local','coordinator','worker'}:
        raise ValueError('deployment.mode must be local, coordinator, or worker')
    allowed = {'origin','bind_address','port','certificate','private_key','peer_certificate'}
    if not isinstance(data,dict) or set(data)-allowed:
        raise ValueError('Unknown transport setting')
    if mode == 'local':
        if data:
            raise ValueError('transport settings require a split deployment mode')
        return {}
    required = {'certificate','private_key','peer_certificate'}
    if not required <= data.keys():
        raise ValueError('Split roles require certificate, private_key and peer_certificate paths')
    value = {}
    for field in required:
        path = data[field]
        if not isinstance(path,str) or not path.strip() or ':' in path or ',' in str(home / path):
            raise ValueError('Transport files require nonempty local paths')
        value[field] = home / path
    if mode == 'coordinator':
        if 'origin' in data:
            raise ValueError('Only workers configure transport.origin')
        bind = data.get('bind_address','127.0.0.1')
        try:
            address = ipaddress.ip_address(bind)
        except (ValueError, TypeError) as exc:
            raise ValueError('Transport bind must be a loopback or Tailscale IPv4 address') from exc
        if address.version != 4 or not (address.is_loopback or address in ipaddress.ip_network('100.64.0.0/10')):
            raise ValueError('Transport bind must be a loopback or Tailscale IPv4 address')
        port = data.get('port',8788)
        if type(port) is not int or not 1024<=port<=65535:
            raise ValueError('Invalid private transport port')
        value.update(bind_address=str(address),port=port)
    else:
        if 'port' in data or 'bind_address' in data:
            raise ValueError('Worker transport has no published listener')
        origin = data.get('origin','')
        if not isinstance(origin,str):
            raise ValueError('transport.origin must be an HTTPS origin')
        parsed=urlsplit(origin)
        try:
            port=parsed.port
        except ValueError as exc:
            raise ValueError('Invalid coordinator port') from exc
        if parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password or parsed.path not in {'','/'} or parsed.query or parsed.fragment:
            raise ValueError('transport.origin must be an HTTPS origin without credentials or a path')
        value['origin']=origin.rstrip('/')
    return value


def tls_bundle(config):
    """Read existing owner-provided files; never generate, log or persist keys."""
    value={}
    for field,name in [('certificate','self.crt'),('private_key','self.key'),('peer_certificate','peer.crt')]:
        path=config['transport'][field]
        info=path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size>65536:
            raise ValueError('Transport credentials must be small regular files, not symlinks')
        if field=='private_key' and (info.st_mode & 0o077 or info.st_uid!=os.geteuid()):
            raise ValueError('Transport private key must be owned by the launcher user with mode0600')
        value[name]=path.read_text()
    # Fail before creating containers when a key/certificate is invalid.
    role=config['mode']
    context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER) if role=='coordinator' else ssl.create_default_context()
    context.load_cert_chain(str(config['transport']['certificate']),str(config['transport']['private_key']),password=lambda:'')
    if value['peer.crt'].count('-----BEGIN CERTIFICATE-----')!=1:
        raise ValueError('Use exactly one peer certificate')
    context.load_verify_locations(cadata=value['peer.crt'])
    if sum(len(v.encode()) for v in value.values())>48*1024:
        raise ValueError('Transport credential files exceed the supported size budget')
    return value


def transport_args(config,installed,api_ip=None):
    coordinator=config['mode']=='coordinator'
    name=config['name']+'-transport' if coordinator else config['name']
    args=common(config,name,'transport',.1,128*1024**2)
    args += ['--user',str(config.get('_transport_uid',os.geteuid()))+':10001', '-e','RECORDINGS_TRANSPORT_MODE='+config['mode']]
    for field,name in [('certificate','self.crt'),('private_key','self.key'),('peer_certificate','peer.crt')]:
        args += ['--mount',f"type=bind,src={config['transport'][field].absolute()},dst=/run/transport-tls/{name},readonly"]
    if coordinator:
        args += ['-p',f"{config['transport']['bind_address']}:{config['transport']['port']}:8443",
                 '--add-host','api:'+api_ip,
                 '-v',credential_volume(config,1)+':/run/worker-credentials/self:ro']
    else:
        args += ['-e','RECORDINGS_COORDINATOR_ORIGIN='+config['transport']['origin'],
                 '-e','RECORDINGS_WORKER_SUBNETS='+config['_worker_subnets']]
    return [*args,'--entrypoint','python',installed['image'],'-m','argon_recordings.transport']


def start(installer,config):
    installed=installer.state(config)
    tls_bundle(config)
    coordinator=config['mode']=='coordinator'
    needed = config['turbo'] if not coordinator else None
    if not coordinator and config['embedding'] != 'off' and not (config['home']/'models'/('e5-'+config['embedding'])).is_dir():
        raise ValueError('Prepared embedding directory missing; run install on the worker')
    if needed and not needed.is_dir():
        raise ValueError('Prepared model directory missing; run install')
    image=json.loads(installer.docker(config,'image','inspect',installed['image'],capture_output=True,text=True).stdout)[0]
    if image['Config'].get('Labels',{}).get('com.argon.recordings.source')!=installed['source']:
        raise ValueError('Installed image identity does not match')
    previous=stack_items(installer,config)
    if any(item['State']['Running'] for item in previous):
        raise ValueError('Stop this installation before changing deployment mode')
    if coordinator:
        for bind,port in [('127.0.0.1',config['port']),(config['transport']['bind_address'],config['transport']['port'])]:
            with socket.socket() as probe:
                probe.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
                probe.bind((bind,port))
    security=json.loads(installer.docker(config,'info','--format','{{json .SecurityOptions}}',capture_output=True,text=True).stdout)
    uid=0 if any('rootless' in option for option in security) else os.geteuid()
    runtime=dict(config,_transport_uid=uid,_worker_subnets=prepare_network(installer,config))
    if coordinator:
        prepare_credentials(installer,runtime,1)
    for item in previous:
        installer.docker(config,'rm',item['Name'].lstrip('/'))
    network=prefix(runtime)+'-workers'
    try:
        if coordinator:
            api=api_args(dict(runtime,api_cpus=runtime['cpus']-.1,api_memory=runtime['memory']-0.125),installed)
            installer.docker(runtime,*api)
            installer.docker(runtime,'network','connect',network,runtime['name'])
            info=installer.inspect(runtime)
            api_ip=info['NetworkSettings']['Networks'][network]['IPAddress']
            installer.docker(runtime,*transport_args(runtime,installed,api_ip))
            installer.docker(runtime,'network','connect',network,runtime['name']+'-transport')
        else:
            installer.docker(runtime,*transport_args(runtime,installed))
            installer.docker(runtime,'network','connect',network,runtime['name'])
            info=installer.inspect(runtime)
            relay_ip=info['NetworkSettings']['Networks'][network]['IPAddress']
            # Full configured inference allocation less the small transport budget;
            # local API reservation is not deducted on the worker-only host.
            worker=worker_args(dict(runtime,api_cpus=.1,api_memory=.125),installed,1,relay_ip)
            index=worker.index(credential_volume(runtime,1)+':/run/worker-credentials/self:ro')
            del worker[index-1:index+1]
            if runtime['embedding'] != 'off':
                worker[-6:-6] = ['-v', str(runtime['home']/'models'/('e5-'+runtime['embedding']))+':/models/embeddings:ro', '-e', 'RECORDINGS_EMBEDDING_DIR=/models/embeddings', '-e', 'RECORDINGS_EMBEDDING_PROFILE='+runtime['embedding'], '-e', 'RECORDINGS_REMOTE_INFERENCE=true']
            worker[-1]='remote-worker'
            installer.docker(runtime,*worker)
        for _ in range(60):
            members=stack_items(installer,runtime)
            ready=False
            if len(members)==2 and all(m['State']['Running'] for m in members):
                relay_name=runtime['name']+'-transport' if coordinator else runtime['name']
                result=installer.docker(runtime,'exec',relay_name,'python','-c',
                                        "from pathlib import Path; print(Path('/tmp/transport-ready').is_file())",
                                        capture_output=True,text=True)
                ready=result.stdout.strip()=='True'
            if ready:
                if coordinator:
                    try:
                        with urllib.request.urlopen(f"http://127.0.0.1:{runtime['port']}/api/health",timeout=1) as response:
                            if json.load(response).get('status')=='ok':
                                print('Coordinator ready; remote worker may be offline.')
                                return
                    except (OSError,ValueError):
                        pass
                else:
                    # Validate the model locally; an offline coordinator is allowed.
                    result=installer.docker(runtime,'exec',runtime['name']+'-worker-1','python','-c',
                                            "from pathlib import Path; print(Path('/tmp/worker-ready').is_file())",
                                            capture_output=True,text=True)
                    if result.stdout.strip()=='True':
                        print('Worker services started; coordinator connection retries automatically.')
                        return
            time.sleep(1)
        raise ValueError('Split services did not start; inspect logs')
    except BaseException:
        stop_stack(installer,runtime)
        raise
