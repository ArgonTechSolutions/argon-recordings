#!/usr/bin/env python3
"""Argon Recordings local installer. Requires Python 3.11+ and working Docker."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import time
import tomllib
import urllib.request

ROOT = Path(__file__).resolve().parent
LABEL = 'com.argon.recordings.installation'


def load_config(path):
    path = path.resolve()
    data = tomllib.loads(path.read_text())
    allowed = {'app': {'port', 'name', 'max_upload_mb'}, 'models': {'transcription', 'embedding', 'turbo_path'},
               'resources': {'cpus', 'memory_gib', 'job_timeout_seconds'}, 'docker': {'sudo'}, 'auth': {'mode', 'public_url'}}
    for section, fields in data.items():
        if section not in allowed or not isinstance(fields, dict) or set(fields) - allowed[section]:
            raise ValueError(f'Unknown configuration key in {section}')
    app, models, resources = data.get('app', {}), data.get('models', {}), data.get('resources', {})
    port, name = app.get('port', 8787), app.get('name', 'argon-recordings')
    if type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError('app.port must be an integer from 1024 to 65535')
    if not isinstance(name, str) or not re.fullmatch(r'[a-z][a-z0-9-]{0,39}', name):
        raise ValueError('app.name must use lowercase letters, digits and hyphens')
    max_upload_mb = app.get('max_upload_mb', 256)
    if type(max_upload_mb) is not int or not 1 <= max_upload_mb <= (2**53 - 1) // 1024**2:
        raise ValueError('app.max_upload_mb must be a positive whole MiB value within the supported byte range')
    embedding = models.get('embedding', 'large')
    if not isinstance(embedding, str) or embedding not in {'large', 'base', 'small', 'off'} or models.get('transcription', 'turbo') != 'turbo':
        raise ValueError('Choose transcription=turbo and embedding=large, base, small, or off')
    cpus, memory = resources.get('cpus', 2), resources.get('memory_gib', 6)
    if type(cpus) not in {int, float} or not .5 <= cpus <= 8 or type(memory) is not int or not 4 <= memory <= 32:
        raise ValueError('resources: cpus must be 0.5..8 and memory_gib must be 4..32')
    timeout = resources.get('job_timeout_seconds', 0)
    if type(timeout) is not int or timeout < 0:
        raise ValueError('resources.job_timeout_seconds must be zero (disabled) or a positive integer')
    sudo = data.get('docker', {}).get('sudo', False)
    if type(sudo) is not bool:
        raise ValueError('docker.sudo must be true or false')
    external = models.get('turbo_path')
    if external is not None and (not isinstance(external, str) or not external.strip()):
        raise ValueError('models.turbo_path must be a nonempty path when specified')
    auth = data.get('auth', {})
    auth_mode, public_url = auth.get('mode', 'local'), auth.get('public_url', '')
    from urllib.parse import urlsplit
    if auth_mode not in {'local', 'accounts'} or not isinstance(public_url, str):
        raise ValueError('auth.mode must be local or accounts')
    if public_url:
        url = urlsplit(public_url)
        if auth_mode != 'accounts' or url.scheme != 'https' or not url.netloc or url.username or url.password or url.path not in {'', '/'} or url.query or url.fragment:
            raise ValueError('auth.public_url requires account mode and an HTTPS origin')
    home = path.parent
    if ':' in str(home) or (external and ':' in external):
        raise ValueError('Installation and model paths cannot contain a colon')
    turbo = (home / external).resolve() if external else home / 'models' / 'turbo'
    return dict(auth_mode=auth_mode, public_url=public_url.rstrip('/'), config_path=path, max_upload_mb=max_upload_mb, timeout=timeout, port=port, name=name, embedding=embedding, cpus=cpus, memory=memory, sudo=sudo,
                home=home, turbo=turbo, external=bool(external), identity=hashlib.sha256(str(home).encode()).hexdigest()[:16])


def docker(config, *args, **kwargs):
    command = (['sudo', '-n'] if config['sudo'] else []) + ['docker', *map(str, args)]
    return subprocess.run(command, check=True, **kwargs)


def source_id():
    digest = hashlib.sha256()
    paths = [ROOT / 'Dockerfile.install', ROOT / 'backend/requirements.txt', ROOT / 'backend/requirements-inference.lock']
    paths += list((ROOT / 'backend/argon_recordings').glob('*.py'))
    paths += [ROOT / 'scripts' / name for name in ['prepare_turbo.py', 'prepare_embeddings.py', 'check_media_runtime.py', 'evaluate_embeddings.py']]
    for directory, folders, filenames in os.walk(ROOT / 'web'):
        folders[:] = [name for name in folders if name not in {'node_modules', 'dist', '.git'}]
        paths += [Path(directory) / name for name in filenames if not name.endswith('.log')]
    for path in sorted(paths):
        digest.update(str(path.relative_to(ROOT)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()[:16]


def state_path(config):
    return config['home'] / '.recordings' / 'installation.json'


def state(config):
    data = json.loads(state_path(config).read_text())
    if data['source'] != source_id() or data['embedding'] != config['embedding'] or data['turbo'] != str(config['turbo']):
        raise ValueError('Source or model configuration changed; run install again before starting')
    return data


def inspect(config):
    owned = docker(config, 'container', 'ls', '-a', '--filter', 'label=' + LABEL + '=' + config['identity'], '--format', '{{.Names}}', capture_output=True, text=True).stdout.splitlines()
    if any(name != config['name'] for name in owned):
        raise ValueError('This installation already has a differently named container; restore app.name before managing it')
    result = docker(config, 'container', 'ls', '-a', '--filter', 'name=^/' + config['name'] + '$', '--format', '{{.ID}}', capture_output=True, text=True)
    if not result.stdout.strip():
        return None
    item = json.loads(docker(config, 'inspect', config['name'], capture_output=True, text=True).stdout)[0]
    if item['Config'].get('Labels', {}).get(LABEL) != config['identity']:
        raise ValueError('Container name belongs to another installation; choose a different app.name')
    return item


def doctor(config):
    if sys.platform != 'linux':
        raise ValueError('Install on Linux; access a remote installation through an SSH tunnel')
    info = json.loads(docker(config, 'info', '--format', '{{json .}}', capture_output=True, text=True).stdout)
    if int(info['ServerVersion'].split('.')[0]) < 28 or str(info['CgroupVersion']) != '2':
        raise ValueError('Docker 28+ with cgroup v2 is required for the local port/resource profile')
    if info.get('Architecture') not in {'x86_64', 'amd64'}:
        raise ValueError('This curated CPU installation currently supports Linux x86-64')
    print(f"Docker {info['ServerVersion']} / cgroup {info['CgroupVersion']} / CPUs {info['NCPU']} / RAM {info['MemTotal']}")
    print('Local URL:', f"http://localhost:{config['port']}")
    print('Models: Turbo CPU INT8; embeddings:', config['embedding'])
    print('Container budget:', config['cpus'], 'CPUs,', config['memory'], 'GiB RAM, no swap')
    print('Free disk GiB:', round(shutil.disk_usage(config['home']).free / 1024**3, 1))


def install(config):
    doctor(config)
    current = inspect(config)
    if current and current['State']['Running']:
        raise ValueError('Stop this installation before rebuilding or preparing models')
    if shutil.disk_usage(config['home']).free < 8 * 1024**3:
        raise ValueError('At least 8 GiB free required for builds, model preparation and work files')
    revision = source_id()
    image = 'argon-recordings:install-' + revision
    docker(config, 'build', '--label', 'com.argon.recordings.source=' + revision, '-f', ROOT / 'Dockerfile.install', '-t', image, ROOT)
    models = config['home'] / 'models'
    models.mkdir(exist_ok=True)
    if config['external']:
        if not config['turbo'].is_dir():
            raise ValueError('Configured existing turbo_path does not exist')
        docker(config, 'run', '--rm', '--network', 'none', '--cpus=1', '--memory=512m', '--memory-swap=512m',
               '-v', f"{config['turbo']}:/existing:ro", '--entrypoint', 'python', image,
               'scripts/prepare_turbo.py', '/existing', '--verify-only')
    else:
        docker(config, 'run', '--rm', '--user', '0:0', '--cpus=1', '--memory=512m', '--memory-swap=512m',
               '-v', f'{models}:/models', '--entrypoint', 'python', image, 'scripts/prepare_turbo.py', '/models/turbo')
    if config['embedding'] != 'off':
        docker(config, 'run', '--rm', '--user', '0:0', '--cpus=1', '--memory=512m', '--memory-swap=512m',
               '-v', f'{models}:/models', '--entrypoint', 'python', image,
               'scripts/prepare_embeddings.py', '/models/e5-' + config['embedding'], config['embedding'])
    path = state_path(config)
    path.parent.mkdir(exist_ok=True)
    temporary = path.with_suffix('.part')
    temporary.write_text(json.dumps(dict(image=image, source=revision, embedding=config['embedding'], turbo=str(config['turbo'])), indent=2) + '\n')
    temporary.replace(path)
    print('Installed. Start with: python3 recordings.py start')


def start_args(config, installed):
    volume = 'argon-recordings-' + config['identity']
    args = ['run', '-d', '--init', '--name', config['name'], '--label', LABEL + '=' + config['identity'],
            '--restart', 'unless-stopped', '--cpus', str(config['cpus']), '--memory', f"{config['memory']}g",
            '--memory-swap', f"{config['memory']}g", '--pids-limit', '256', '--read-only', '--cap-drop', 'ALL',
            '--security-opt', 'no-new-privileges', '--tmpfs', '/tmp:rw,size=64m',
            '--log-opt', 'max-size=10m', '--log-opt', 'max-file=3',
            '-p', f"127.0.0.1:{config['port']}:8000", '-v', volume + '-data:/data', '-v', volume + '-work:/work',
            '-v', str(config['turbo']) + ':/models/turbo:ro', '-e', 'RECORDINGS_JOB_TIMEOUT_SECONDS=' + str(config['timeout']), '-e', 'RECORDINGS_MAX_UPLOAD_MB=' + str(config['max_upload_mb'])]
    args += ['-e', 'RECORDINGS_AUTH_MODE=' + config['auth_mode'], '-e', 'RECORDINGS_PUBLIC_URL=' + config['public_url']]
    if config['embedding'] != 'off':
        args += ['-v', str(config['home'] / 'models' / ('e5-' + config['embedding'])) + ':/models/embeddings:ro',
                 '-e', 'RECORDINGS_EMBEDDING_DIR=/models/embeddings', '-e', 'RECORDINGS_EMBEDDING_PROFILE=' + config['embedding']]
    args += [installed['image']]
    return args


def start(config):
    installed = state(config)
    if not config['turbo'].is_dir() or (config['embedding'] != 'off' and not (config['home'] / 'models' / ('e5-' + config['embedding'])).is_dir()):
        raise ValueError('Prepared model directory is missing; run install again')
    actual_image = json.loads(docker(config, 'image', 'inspect', installed['image'], capture_output=True, text=True).stdout)[0]
    if actual_image['Config'].get('Labels', {}).get('com.argon.recordings.source') != installed['source']:
        raise ValueError('Installed image identity does not match; run install again')
    previous = inspect(config)
    if previous and previous['State']['Running']:
        raise ValueError('Already running; stop before changing configuration')
    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind(('127.0.0.1', config['port']))
    if previous:
        docker(config, 'rm', config['name'])  # Container only; data/work volumes are preserved.
    docker(config, *start_args(config, installed))
    url = f"http://127.0.0.1:{config['port']}/api/health"
    for _ in range(120):
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                health = json.load(response)
            running = inspect(config)
            if running and running['State']['Running'] and health.get('status') == 'ok' and health.get('worker_configured'):
                print('Ready:', f"http://localhost:{config['port']}")
                if health.get('accounts_setup_required'):
                    print('Create the initial administrator: python3 recordings.py setup-admin (see docs/ACCOUNTS.md for existing projects).')
                return
        except (OSError, ValueError):
            pass
        time.sleep(1)
    raise ValueError('Startup not ready after 120 seconds; inspect status and logs')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=ROOT / 'recordings.toml')
    parser.add_argument('command', choices=['init', 'doctor', 'install', 'up', 'start', 'stop', 'status', 'logs', 'evaluate', 'update', 'setup-admin', 'setup-oidc', 'recover-admin'])
    parser.add_argument('--adopt-existing', action='store_true')
    args = parser.parse_args()
    try:
        if args.command == 'init':
            with args.config.open('x') as out:
                out.write((ROOT / 'recordings.example.toml').read_text())
            print('Created', args.config, '— edit it, then run install.')
            return
        config = load_config(args.config)
        if args.command in {'setup-admin', 'setup-oidc', 'recover-admin'}:
            item = inspect(config)
            if config['auth_mode'] != 'accounts' or not item or not item['State']['Running'] or 'RECORDINGS_AUTH_MODE=accounts' not in item['Config']['Env']:
                raise ValueError('Set auth.mode=accounts and restart the app before interactive setup')
            command = ['exec', '-it', config['name'], 'python', '-m', 'argon_recordings.account_cli', args.command]
            if args.adopt_existing:
                command += ['--adopt-existing']
            docker(config, *command)
        elif args.command == 'update':
            from scripts.git_updates import update
            update(sys.modules[__name__], config)
        elif args.command == 'doctor':
            doctor(config)
        elif args.command == 'install':
            install(config)
        elif args.command in {'start', 'up'}:
            if args.command == 'up':
                try:
                    state(config)
                except (OSError, ValueError, KeyError):
                    install(config)
            start(config)
        elif args.command == 'evaluate':
            installed = state(config)
            if config['embedding'] == 'off':
                raise ValueError('Choose an embedding profile before evaluating retrieval')
            docker(config, 'run', '--rm', '--network', 'none', '--cpus', str(config['cpus']), '--memory=3g', '--memory-swap=3g',
                   '-v', str(config['home'] / 'models' / ('e5-' + config['embedding'])) + ':/models/embeddings:ro',
                   '--entrypoint', 'python', installed['image'], 'scripts/evaluate_embeddings.py', '/models/embeddings', '--profile', config['embedding'])
        else:
            item = inspect(config)
            if not item:
                print('Not started. Data volumes, if any, are preserved.')
            elif args.command == 'stop':
                docker(config, 'stop', '-t', '30', config['name'])
            elif args.command == 'logs':
                docker(config, 'logs', '--tail', '100', config['name'])
            else:
                print(json.dumps(dict(status=item['State']['Status'], image=item['Config']['Image'],
                                      ports=item['HostConfig']['PortBindings'], cpus=item['HostConfig']['NanoCpus'],
                                      memory_bytes=item['HostConfig']['Memory'], mounts=[dict(destination=m['Destination'], volume=m.get('Name'), source=m['Source']) for m in item['Mounts']]), indent=2))
    except (ValueError, OSError, subprocess.CalledProcessError, KeyError) as exc:
        parser.exit(1, f'Error: {exc}\n')


if __name__ == '__main__':
    main()
