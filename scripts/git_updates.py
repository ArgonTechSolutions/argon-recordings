"""Small Git fast-forward updater; runtime data is never Git-managed."""
import hashlib
from pathlib import Path
import shutil
import subprocess
import sys
import os

REPOSITORY = 'ArgonTechSolutions/argon-recordings'
REMOTE_URLS = {f'git@github.com:{REPOSITORY}.git', f'https://github.com/{REPOSITORY}.git', f'https://github.com/{REPOSITORY}'}
PROTECTED = {'recordings.toml', 'models', 'data', '.recordings', '.git'}


def git(root, *args, check=True):
    environment = dict(os.environ, GIT_TERMINAL_PROMPT='0')
    # SSH keys/credentials are configured by the operator; never embed them in URLs.
    return subprocess.run(['git', '-C', str(root), *args], check=check, capture_output=True,
                          text=True, timeout=120, env=environment)


def tree(root, revision):
    result = {}
    for entry in git(root, 'ls-tree', '-rz', '--full-tree', revision).stdout.split('\0'):
        if not entry:
            continue
        metadata, name = entry.split('\t', 1)
        mode, kind, digest = metadata.split()
        path = Path(name)
        if kind != 'blob' or mode not in {'100644', '100755'} or path.is_absolute() or '..' in path.parts or path.parts[0] in PROTECTED:
            raise ValueError('Update tree contains an unsupported or protected path')
        result[name] = digest
    return result


def checked_checkout(root):
    root = Path(root).resolve()
    if not (root / '.git').is_dir() or Path(git(root, 'rev-parse', '--show-toplevel').stdout.strip()).resolve() != root:
        raise ValueError('Updates require a normal Git checkout; use the documented one-time bootstrap')
    if git(root, 'remote', 'get-url', 'origin').stdout.strip() not in REMOTE_URLS:
        raise ValueError('origin must point to the configured Argon Recordings repository')
    if git(root, 'branch', '--show-current').stdout.strip() != 'main':
        raise ValueError('Switch to main before updating')
    if git(root, 'status', '--porcelain', '--untracked-files=normal').stdout.strip():
        raise ValueError('Local source changes found; commit or preserve them before updating')
    return root


def update(installer, config):
    root = checked_checkout(installer.ROOT)
    previous = git(root, 'rev-parse', 'HEAD').stdout.strip()
    git(root, 'fetch', 'origin', 'main')  # Fetch/authentication errors occur before stopping the app.
    target = git(root, 'rev-parse', 'refs/remotes/origin/main').stdout.strip()
    tree(root, target)
    if git(root, 'merge-base', '--is-ancestor', previous, target, check=False).returncode:
        raise ValueError('Local history has diverged from origin/main; refusing to overwrite it')
    needs_install = True
    try:
        installer.state(config)
        needs_install = False
    except (OSError, ValueError, KeyError):
        pass
    if previous == target and not needs_install:
        print('Already up to date:', target[:12])
        return
    current = installer.inspect(config)
    was_running = bool(current and current['State']['Running'])
    if was_running:
        installer.docker(config, 'stop', '-t', '30', config['name'])
    git(root, 'merge', '--ff-only', target)
    # Execute the new installer, not Python functions loaded from the old version.
    command = [sys.executable, str(root / 'recordings.py'), '--config', str(config['config_path'])]
    subprocess.run([*command, 'install'], check=True)
    if was_running:
        subprocess.run([*command, 'start'], check=True)
    print(f'Updated {previous[:12]} -> {target[:12]}. Config, weights and data volumes preserved.')
    if not was_running:
        print('App remains stopped; run start when ready.')


def file_digest(path):
    if path.is_symlink() or not path.is_file():
        return None
    data = path.read_bytes()
    return hashlib.sha1(f'blob {len(data)}\0'.encode() + data).hexdigest()


def bootstrap(source, destination):
    """Recognize an unmodified source archive from Git history before adding .git."""
    source, destination = checked_checkout(source), Path(destination).resolve()
    if source == destination or (destination / '.git').exists():
        raise ValueError('Bootstrap destination must be the existing archive installation without .git')
    if not (destination / 'recordings.toml').is_file() or not (destination / 'recordings.py').is_file():
        raise ValueError('Destination must contain the existing installation and recordings.toml')
    if git(source, 'rev-parse', '--show-object-format').stdout.strip() != 'sha1':
        raise ValueError('Archive bootstrap currently requires a SHA-1 Git repository')
    baseline = None
    for revision in git(source, 'rev-list', '--max-count=100', 'HEAD').stdout.splitlines():
        files = tree(source, revision)
        if files and all(file_digest(destination / name) == digest for name, digest in files.items()):
            baseline = revision
            break
    if baseline is None:
        raise ValueError('Existing source does not match a known archive; preserve local edits before bootstrapping')
    # Refuse source-path collisions, including a user-created file at a newly introduced path.
    old_files = tree(source, baseline)
    for name, digest in tree(source, 'HEAD').items():
        path = destination / name
        if name not in old_files and (path.exists() or path.is_symlink()):
            raise ValueError(f'New source path already exists locally: {name}')
        if any(parent.is_symlink() for parent in path.parents if parent != destination and destination in parent.parents):
            raise ValueError('Symlinked source directories cannot be bootstrapped')
    shutil.copytree(source / '.git', destination / '.git', symlinks=False)
    # Only the index/branch moves. Existing source/config/model/data files are untouched here.
    git(destination, 'reset', '--mixed', baseline)
    checked_checkout(destination)
    print('Git enabled in existing installation at', baseline[:12])
