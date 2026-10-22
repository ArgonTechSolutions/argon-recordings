"""Local Docker stack lifecycle; no Docker socket is mounted in application containers."""
import json
import re
import socket
import time
import urllib.request


ROLE = 'com.argon.recordings.role'


def prefix(config):
    return 'argon-recordings-' + config['identity']


def worker_name(config, index):
    return config['name'] + '-worker-' + str(index)


def credential_volume(config, index):
    return prefix(config) + '-credentials-' + str(index)


def common(config, name, role, cpus, memory):
    return ['run', '-d', '--init', '--name', name, '--label', 'com.argon.recordings.installation=' + config['identity'],
            '--label', ROLE + '=' + role, '--restart', 'unless-stopped', '--cpus', str(cpus),
            '--memory', str(memory), '--memory-swap', str(memory), '--pids-limit', '256', '--read-only',
            '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges', '--tmpfs', '/tmp:rw,size=64m',
            '--log-opt', 'max-size=10m', '--log-opt', 'max-file=3']


def api_args(config, installed, label=None):
    args = common(config, config['name'], 'api', config['api_cpus'], config['api_memory'] * 1024**3)
    args += ['-p', f"127.0.0.1:{config['port']}:8000", '-v', prefix(config) + '-data:/data',
             '-e', 'RECORDINGS_AUTH_MODE=' + config['auth_mode'], '-e', 'RECORDINGS_PUBLIC_URL=' + config['public_url'],
             '-e', 'RECORDINGS_ALLOW_TOTP=' + str(config['allow_totp']).lower(),
             '-e', 'RECORDINGS_MAX_UPLOAD_MB=' + str(config['max_upload_mb']),
             '-e', 'RECORDINGS_WORKER_COUNT=' + str(config['workers']),
             '-e', 'RECORDINGS_CHUNK_SECONDS=' + str(config['chunk_seconds']),
             '-e', 'RECORDINGS_OVERLAP_SECONDS=' + str(config['overlap_seconds']),
             '-e', 'RECORDINGS_WORKER_SUBNETS=' + config.get('_worker_subnets', '')]
    for index in range(1, config['workers'] + 1):
        args += ['-v', credential_volume(config, index) + ':/run/worker-credentials/' + str(index)]
    if config['embedding'] != 'off':
        args += ['-v', str(config['home'] / 'models' / ('e5-' + config['embedding'])) + ':/models/embeddings:ro',
                 '-e', 'RECORDINGS_EMBEDDING_DIR=/models/embeddings', '-e', 'RECORDINGS_EMBEDDING_PROFILE=' + config['embedding']]
    return [*args, '--entrypoint', 'python', installed['image'], '-m', 'argon_recordings.isolated', 'api']


def worker_args(config, installed, index, api_ip):
    memory = (config['memory'] - config['api_memory']) * 1024**3 // config['workers']
    args = common(config, worker_name(config, index), 'worker', (config['cpus'] - config['api_cpus']) / config['workers'], memory)
    work = prefix(config) + ('-work' if index == 1 else '-work-' + str(index))
    args += ['--network', prefix(config) + '-workers', '--dns', '127.0.0.1', '--add-host', 'api:' + api_ip,
             '-v', work + ':/work', '-v', str(config['turbo']) + ':/models/turbo:ro',
             '-v', credential_volume(config, index) + ':/run/worker-credentials/self:ro',
             '-e', 'RECORDINGS_WORKER_ID=worker-' + str(index),
             '-e', 'RECORDINGS_JOB_TIMEOUT_SECONDS=' + str(config['timeout'])]
    return [*args, '--entrypoint', 'python', installed['image'], '-m', 'argon_recordings.isolated', 'worker']


def stack_items(installer, config):
    installer.inspect(config)  # Reject changed app.name / another installation's primary container.
    names = installer.docker(config, 'container', 'ls', '-a', '--filter', 'label=' + installer.LABEL + '=' + config['identity'],
                             '--format', '{{.Names}}', capture_output=True, text=True).stdout.splitlines()
    items = []
    for name in names:
        item = json.loads(installer.docker(config, 'inspect', name, capture_output=True, text=True).stdout)[0]
        role = item['Config'].get('Labels', {}).get(ROLE)
        if name == config['name']:
            if role not in {None, 'api'}:
                raise ValueError('Unexpected primary container role')
        elif not re.fullmatch(re.escape(config['name']) + r'-worker-[1-8]', name) or role != 'worker':
            raise ValueError('Unexpected container in this installation; refusing lifecycle changes')
        items.append(item)
    return items


def stop_stack(installer, config):
    items = stack_items(installer, config)
    # Stop workers first; their publication/heartbeats remain lease-fenced.
    items.sort(key=lambda item: item['Name'].lstrip('/') == config['name'])
    for item in items:
        if item['State']['Running']:
            installer.docker(config, 'stop', '-t', '30', item['Name'].lstrip('/'))


def prepare_network(installer, config):
    name = prefix(config) + '-workers'
    names = installer.docker(config, 'network', 'ls', '--format', '{{.Name}}', capture_output=True, text=True).stdout.splitlines()
    if name not in names:
        installer.docker(config, 'network', 'create', '--internal', '--label', installer.LABEL + '=' + config['identity'], name)
    item = json.loads(installer.docker(config, 'network', 'inspect', name, capture_output=True, text=True).stdout)[0]
    if not item.get('Internal') or item.get('Labels', {}).get(installer.LABEL) != config['identity'] or item['Driver'] != 'bridge':
        raise ValueError('Worker network is not an owned internal bridge')
    subnets = [entry['Subnet'] for entry in item['IPAM']['Config'] if entry.get('Subnet')]
    if not subnets:
        raise ValueError('Worker network has no subnet')
    return ','.join(subnets)


def prepare_credentials(installer, config, index):
    name = credential_volume(config, index)
    names = installer.docker(config, 'volume', 'ls', '--format', '{{.Name}}', capture_output=True, text=True).stdout.splitlines()
    options = {'type': 'tmpfs', 'device': 'tmpfs', 'o': 'size=65536,uid=10001,gid=10001,mode=0700'}
    if name not in names:
        args = ['volume', 'create', '--driver', 'local', '--label', installer.LABEL + '=' + config['identity']]
        for key, value in options.items():
            args += ['--opt', key + '=' + value]
        installer.docker(config, *args, name)
    item = json.loads(installer.docker(config, 'volume', 'inspect', name, capture_output=True, text=True).stdout)[0]
    if item.get('Labels', {}).get(installer.LABEL) != config['identity'] or item['Driver'] != 'local' or item.get('Options') != options:
        raise ValueError('Worker credential storage must be an owned RAM-backed volume')


def start_stack(installer, config):
    installed = installer.state(config)
    if not config['turbo'].is_dir() or (config['embedding'] != 'off' and not (config['home'] / 'models' / ('e5-' + config['embedding'])).is_dir()):
        raise ValueError('Prepared model directory is missing; run install again')
    image = json.loads(installer.docker(config, 'image', 'inspect', installed['image'], capture_output=True, text=True).stdout)[0]
    if image['Config'].get('Labels', {}).get('com.argon.recordings.source') != installed['source']:
        raise ValueError('Installed image identity does not match; run install again')
    previous = stack_items(installer, config)
    if any(item['State']['Running'] for item in previous):
        raise ValueError('Already running; stop the installation before changing configuration')
    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind(('127.0.0.1', config['port']))
    runtime = dict(config, _worker_subnets=prepare_network(installer, config))
    for index in range(1, config['workers'] + 1):
        prepare_credentials(installer, runtime, index)
    for item in previous:
        installer.docker(config, 'rm', item['Name'].lstrip('/'))  # Containers only; never data/model/work volumes.
    try:
        installer.docker(runtime, *api_args(runtime, installed))
        name = prefix(runtime) + '-workers'
        installer.docker(runtime, 'network', 'connect', '--alias', 'api', name, runtime['name'])
        item = installer.inspect(runtime)
        api_ip = item['NetworkSettings']['Networks'][name]['IPAddress']
        for index in range(1, runtime['workers'] + 1):
            installer.docker(runtime, *worker_args(runtime, installed, index, api_ip))
        for _ in range(120):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{runtime['port']}/api/health", timeout=1) as reply:
                    health = json.load(reply)
                members = stack_items(installer, runtime)
                if len(members) == runtime['workers'] + 1 and all(m['State']['Running'] for m in members) and health.get('workers_online') == runtime['workers']:
                    print('Ready:', f"http://localhost:{runtime['port']}")
                    if health.get('accounts_setup_required'):
                        print('Create the initial administrator: python3 recordings.py setup-admin')
                    return
            except (OSError, ValueError):
                pass
            time.sleep(1)
        raise ValueError('Isolated stack not ready after120seconds; inspect logs before retrying')
    except BaseException:
        stop_stack(installer, runtime)
        raise
