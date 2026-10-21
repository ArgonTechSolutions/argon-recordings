"""Real disposable Git repositories; Docker/install execution is isolated."""
import importlib.util
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('git_updates_test', ROOT / 'scripts/git_updates.py')
updates = importlib.util.module_from_spec(spec)
spec.loader.exec_module(updates)


def command(*args):
    return subprocess.run(list(map(str, args)), check=True, capture_output=True, text=True).stdout.strip()


def commit(root, message):
    command('git', '-C', root, 'add', '.')
    command('git', '-C', root, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-m', message)
    return command('git', '-C', root, 'rev-parse', 'HEAD')


@pytest.fixture
def repos(tmp_path, monkeypatch):
    remote, writer, client = [tmp_path / name for name in ['remote.git', 'writer', 'client']]
    command('git', 'init', '--bare', '--initial-branch=main', remote)
    command('git', 'init', '--initial-branch=main', writer)
    (writer / '.gitignore').write_text('recordings.toml\nmodels/\ndata/\n.recordings/\n')
    (writer / 'recordings.py').write_text('print("old installer")\n')
    (writer / 'README.md').write_text('first version\n')
    old = commit(writer, 'feat: initial fixture')
    command('git', '-C', writer, 'remote', 'add', 'origin', remote)
    command('git', '-C', writer, 'push', '-u', 'origin', 'main')
    command('git', 'clone', '--no-local', remote, client)
    (writer / 'README.md').write_text('second version\n')
    (writer / 'recordings.py').write_text('print("new installer")\n')
    new = commit(writer, 'feat: updated fixture')
    command('git', '-C', writer, 'push', 'origin', 'main')
    monkeypatch.setattr(updates, 'REMOTE_URLS', {str(remote)})
    return SimpleNamespace(remote=remote, writer=writer, client=client, old=old, new=new)


def fake_installer(root, running=True):
    events = []
    installer = SimpleNamespace(ROOT=root, state=lambda config: {}, inspect=lambda config: {'State': {'Running': running}},
                                docker=lambda *args: events.append('stop'))
    return installer, events


def test_update_fast_forwards_and_executes_new_installer(repos, monkeypatch):
    config = repos.client / 'recordings.toml'
    config.write_text('private operator config\n')
    (repos.client / 'models').mkdir()
    (repos.client / 'models' / 'retained').write_text('model fixture')
    installer, events = fake_installer(repos.client)
    real_run = subprocess.run
    def run(args, **kwargs):
        if args[0] == sys.executable:
            events.append(args[-1])
            assert (repos.client / 'recordings.py').read_text() == 'print("new installer")\n'
            return SimpleNamespace(returncode=0)
        return real_run(args, **kwargs)
    monkeypatch.setattr(updates.subprocess, 'run', run)
    updates.update(installer, {'name': 'fixture', 'config_path': config})
    assert events == ['stop', 'install', 'start']
    assert command('git', '-C', repos.client, 'rev-parse', 'HEAD') == repos.new
    assert config.read_text() == 'private operator config\n'
    assert (repos.client / 'models' / 'retained').read_text() == 'model fixture'


def test_update_refuses_dirty_or_diverged_source_before_stop(repos):
    installer, events = fake_installer(repos.client)
    (repos.client / 'README.md').write_text('operator edits\n')
    with pytest.raises(ValueError, match='Local source changes'):
        updates.update(installer, {})
    commit(repos.client, 'fix: local divergent edit')
    with pytest.raises(ValueError, match='diverged'):
        updates.update(installer, {})
    assert not events


def test_update_noop_and_preserves_stopped_state(repos, monkeypatch):
    installer, events = fake_installer(repos.client, running=False)
    real_run = subprocess.run
    def run(args, **kwargs):
        if args[0] == sys.executable:
            events.append(args[-1]); return SimpleNamespace(returncode=0)
        return real_run(args, **kwargs)
    monkeypatch.setattr(updates.subprocess, 'run', run)
    updates.update(installer, {'config_path': repos.client / 'recordings.toml'})
    assert events == ['install']
    events.clear()
    updates.update(installer, {})
    assert events == []


def test_archive_bootstrap_recognizes_old_version_and_preserves_data(repos, tmp_path):
    destination = tmp_path / 'existing-archive'
    destination.mkdir()
    for name in updates.tree(repos.writer, repos.old):
        (destination / name).parent.mkdir(parents=True, exist_ok=True)
        (destination / name).write_text(command('git', '-C', repos.writer, 'show', repos.old + ':' + name) + '\n')
    (destination / 'recordings.toml').write_text('operator config')
    (destination / 'models').mkdir()
    (destination / 'models' / 'keep').write_text('weights')
    updates.bootstrap(repos.writer, destination)
    assert command('git', '-C', destination, 'rev-parse', 'HEAD') == repos.old
    assert not updates.git(destination, 'status', '--porcelain').stdout.strip()
    assert (destination / 'recordings.toml').read_text() == 'operator config'
    assert (destination / 'models' / 'keep').read_text() == 'weights'


def test_bootstrap_refuses_unknown_source_without_creating_git(repos, tmp_path):
    destination = tmp_path / 'modified-archive'
    destination.mkdir()
    (destination / 'recordings.toml').write_text('operator config')
    (destination / 'recordings.py').write_text('operator code edits')
    with pytest.raises(ValueError, match='known archive'):
        updates.bootstrap(repos.writer, destination)
    assert not (destination / '.git').exists()


def test_updates_refuse_wrong_origin(repos, monkeypatch):
    monkeypatch.setattr(updates, 'REMOTE_URLS', {'https://github.com/another/repository.git'})
    with pytest.raises(ValueError, match='origin must'):
        updates.checked_checkout(repos.client)


def test_fetch_failure_keeps_running_app_untouched(repos):
    installer, events = fake_installer(repos.client)
    repos.remote.rename(repos.remote.with_name('unavailable.git'))
    with pytest.raises(subprocess.CalledProcessError):
        updates.update(installer, {})
    assert events == []
    assert command('git', '-C', repos.client, 'rev-parse', 'HEAD') == repos.old
