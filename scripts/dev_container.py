"""Run bounded development checks in a named, disposable personal Docker container."""
import argparse
import json
import os
import re
import signal
import subprocess
import sys
import uuid

PROJECT_LABEL = 'com.argon.recordings.project'
SESSION_LABEL = 'com.argon.recordings.session'


def docker_command():
    if os.environ.get('DOCKER_HOST'):
        raise ValueError('Unset DOCKER_HOST; this launcher requires a personal rootless context')
    context = subprocess.check_output(['docker', 'context', 'show'], text=True, timeout=10).strip()
    endpoint = subprocess.check_output(['docker', 'context', 'inspect', context,
        '--format', '{{.Endpoints.docker.Host}}'], text=True, timeout=10).strip()
    if endpoint != f'unix:///run/user/{os.getuid()}/docker.sock':
        raise ValueError('Only the current user rootless Docker socket is permitted')
    return ['docker', '--context', context]


def cleanup(docker, name, session):
    """Revalidate session ownership; never remove volumes or unrelated containers."""
    inspected = subprocess.run([*docker, 'inspect', '--type', 'container', name],
        capture_output=True, text=True, timeout=10)
    if inspected.returncode:
        return  # --rm has already removed a successfully completed container.
    item = json.loads(inspected.stdout)[0]
    labels = item['Config'].get('Labels') or {}
    if labels.get(PROJECT_LABEL) != 'argon-recordings' or labels.get(SESSION_LABEL) != session:
        raise ValueError('Container session ownership differs; cleanup refused')
    cid = item['Id']
    if item['State']['Running']:
        stopped = subprocess.run([*docker, 'stop', '--time', '2', cid],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
        if stopped.returncode:
            raise RuntimeError('Could not stop this temporary container; inspect its named session')
    # The daemon's --rm may win the race; absence is success. No force or -v.
    remaining = subprocess.run([*docker, 'inspect', '--type', 'container', cid],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
    if remaining.returncode == 0:
        subprocess.run([*docker, 'rm', cid], check=True, timeout=10)


def run(purpose, arguments, timeout, docker):
    if not re.fullmatch(r'[a-z][a-z0-9-]{0,39}', purpose):
        raise ValueError('Use a short lowercase purpose, such as backend-tests or visual-eval')
    reserved = {'--name', '--rm', '--cidfile', '--detach', '-d', '--restart', '--label-file'}
    if any(arg.split('=', 1)[0] in reserved or (re.fullmatch(r'-[dit]+',arg) and 'd' in arg)
           or PROJECT_LABEL+'=' in arg or SESSION_LABEL+'=' in arg for arg in arguments):
        raise ValueError('Session name, labels, removal and foreground lifecycle belong to this launcher')
    session = uuid.uuid4().hex
    name = f'argon-recordings-{purpose}-{os.getpid()}-{session[:8]}'
    command = [*docker, 'run', '--rm', '--init', '--name', name,
        '--label', PROJECT_LABEL+'=argon-recordings', '--label', SESSION_LABEL+'='+session,
        '--label', 'com.argon.recordings.purpose='+purpose,
        '--label', 'com.argon.recordings.temporary=true', *arguments]
    print(f'Temporary container: {name}', file=sys.stderr, flush=True)
    process = subprocess.Popen(command)
    try:
        return process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        print('Development check timed out; closing its container', file=sys.stderr)
        return 124
    except KeyboardInterrupt:
        return 130
    finally:
        try:
            cleanup(docker, name, session)
        finally:
            if process.poll() is None:
                process.terminate()
                try: process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill(); process.wait(timeout=5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--timeout', type=int, default=600, help='Wall-clock seconds, including startup')
    parser.add_argument('purpose')
    parser.add_argument('docker_args', nargs=argparse.REMAINDER, help='Docker run flags, image and command after --')
    args = parser.parse_args()
    arguments = args.docker_args[1:] if args.docker_args[:1] == ['--'] else args.docker_args
    if not 1 <= args.timeout <= 7200 or not arguments:
        parser.error('Supply Docker arguments and a timeout of 1–7200 seconds')
    def interrupted(*_):
        raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM, interrupted)
    try:
        return run(args.purpose, arguments, args.timeout, docker_command())
    except (ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
