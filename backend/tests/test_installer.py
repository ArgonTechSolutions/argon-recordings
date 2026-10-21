import importlib.util
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('recordings_installer', ROOT / 'recordings.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


def config(tmp_path, extra=''):
    path = tmp_path / 'recordings.toml'
    path.write_text('[app]\nport=8877\n' + extra)
    return installer.load_config(path)


def test_config_defaults_and_resource_limited_start(tmp_path):
    settings = config(tmp_path)
    args = installer.start_args(settings, {'image': 'test-image'})
    assert '127.0.0.1:8877:8000' in args
    assert '--read-only' in args and '--cap-drop' in args
    assert 'RECORDINGS_JOB_TIMEOUT_SECONDS=0' in args
    assert '--memory-swap' in args
    assert '6g' in args
    assert 'RECORDINGS_WORKER_TOKEN' not in ' '.join(args)
    assert str(tmp_path / 'models/turbo') + ':/models/turbo:ro' in args
    assert any(':/data' in value for value in args)


@pytest.mark.parametrize('body', ['[app]\nport=80', '[app]\nport=true', '[app]\nname="bad/name"',
                                  '[models]\nembedding="download-anything"', '[models]\ntranscription="tiny"',
                                  '[resources]\nmemory_gib=1', '[resources]\ncpus=true', '[docker]\nsudo="yes"',
                                  '[app]\nunknown="ignored"'])
def test_invalid_configuration_fails_before_docker(tmp_path, body):
    path = tmp_path / 'bad.toml'
    path.write_text(body)
    with pytest.raises(ValueError):
        installer.load_config(path)


def test_off_profile_external_model_and_scoped_identity(tmp_path):
    settings = config(tmp_path, '[models]\nembedding="off"\nturbo_path="../existing-model"\n[docker]\nsudo=true\n')
    assert settings['sudo'] is True
    assert settings['turbo'] == (tmp_path.parent / 'existing-model').resolve()
    args = installer.start_args(settings, {'image': 'test-image'})
    assert 'RECORDINGS_EMBEDDING_DIR=/models/embeddings' not in args
    assert all(':/models/embeddings:' not in value for value in args)
    assert '--label' in args


def test_source_hash_stable_and_state_rejects_model_change(tmp_path):
    assert installer.source_id() == installer.source_id()
    settings = config(tmp_path)
    installer.state_path(settings).parent.mkdir()
    import json
    installer.state_path(settings).write_text(json.dumps(dict(source=installer.source_id(), embedding='small', turbo=str(settings['turbo']))))
    with pytest.raises(ValueError, match='changed'):
        installer.state(settings)


@pytest.mark.parametrize('version,cgroup', [('27.5.1', '2'), ('29.1.0', '1')])
def test_doctor_rejects_unsupported_port_or_resource_runtime(tmp_path, monkeypatch, version, cgroup):
    import json
    from types import SimpleNamespace
    monkeypatch.setattr(installer, 'docker', lambda *args, **kwargs: SimpleNamespace(stdout=json.dumps(dict(ServerVersion=version, CgroupVersion=cgroup))))
    with pytest.raises(ValueError, match='Docker 28'):
        installer.doctor(config(tmp_path))


def test_lifecycle_refuses_a_differently_named_owned_container(tmp_path, monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(installer, 'docker', lambda *args, **kwargs: SimpleNamespace(stdout='old-installation-name\n'))
    with pytest.raises(ValueError, match='differently named'):
        installer.inspect(config(tmp_path))


def test_first_up_installs_before_starting(tmp_path, monkeypatch):
    settings = config(tmp_path)
    calls = []
    monkeypatch.setattr(installer.sys, 'argv', ['recordings.py', '--config', str(tmp_path / 'recordings.toml'), 'up'])
    monkeypatch.setattr(installer, 'install', lambda value: calls.append(('install', value['port'])))
    monkeypatch.setattr(installer, 'start', lambda value: calls.append(('start', value['port'])))
    installer.main()
    assert calls == [('install', settings['port']), ('start', settings['port'])]



def test_optional_timeout_can_be_enabled_explicitly(tmp_path):
    settings = config(tmp_path, '[resources]\njob_timeout_seconds=14400\n')
    assert 'RECORDINGS_JOB_TIMEOUT_SECONDS=14400' in installer.start_args(settings, {'image': 'test'})
