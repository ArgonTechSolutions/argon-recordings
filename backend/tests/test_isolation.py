"""Production socket separation and aggregate container budgets."""
import importlib.util
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from starlette.staticfiles import StaticFiles

from argon_recordings.isolated import Interfaces
from scripts.worker_stack import api_args, worker_args

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('isolation_installer', ROOT / 'recordings.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


@pytest.fixture
def anyio_backend():
    return 'asyncio'


@pytest.mark.anyio
async def test_worker_socket_is_a_closed_route_allowlist(tmp_path):
    app = FastAPI()
    @app.post('/api/worker/ready')
    def ready():
        return {'ready': True}
    @app.get('/api/auth/session')
    def session():
        return {'admin': True}
    (tmp_path / 'index.html').write_text('frontend')
    app.mount('/', StaticFiles(directory=tmp_path, html=True))
    isolated = Interfaces(app, ['172.20.0.0/24'])
    cases = [
        ('172.20.0.3', 8001, '/api/worker/ready', 200),
        ('172.20.0.3', 8000, '/api/worker/ready', 404),
        ('172.20.0.3', 8000, '/api/auth/session', 404),
        ('172.20.0.3', 8001, '/api/auth/session', 404),
        ('172.20.0.3', 8001, '/api/worker/unknown', 404),
        ('192.0.2.1', 8001, '/api/worker/ready', 404),
        ('192.0.2.1', 8000, '/api/worker/ready', 404),
    ]
    for peer, port, path, status in cases:
        transport = httpx.ASGITransport(app=isolated, client=(peer, 12345))
        async with httpx.AsyncClient(transport=transport, base_url=f'http://api:{port}') as client:
            response = await client.post(path, headers={'X-Forwarded-For': '172.20.0.3', 'X-Forwarded-Port': '8001'})
        assert response.status_code == status, (peer, port, path, response.text)


def test_multiworker_budget_and_credential_mounts(tmp_path):
    path = tmp_path / 'recordings.toml'
    path.write_text('[resources]\ncpus=3\nmemory_gib=8\n[workers]\ncount=2\n')
    cfg = installer.load_config(path)
    args = [api_args(cfg, {'image': 'test'})] + [worker_args(cfg, {'image': 'test'}, i, '172.20.0.2') for i in [1, 2]]
    assert sum(float(a[a.index('--cpus')+1]) for a in args) == 3
    assert sum(int(a[a.index('--memory')+1]) for a in args) <= 8 * 1024**3
    for index, worker in enumerate(args[1:], 1):
        mounts = [worker[i+1] for i, item in enumerate(worker) if item == '-v']
        assert len(mounts) == 3
        assert any(f'-credentials-{index}:/run/worker-credentials/self:ro' in m for m in mounts)
        assert all(':/data' not in m and 'docker.sock' not in m for m in mounts)
        assert all('TOKEN=' not in item for item in worker)
        assert worker[worker.index('--memory')+1] == worker[worker.index('--memory-swap')+1]


@pytest.mark.parametrize('body', [
    '[workers]\ncount=0', '[workers]\ncount=true', '[workers]\ncount=9',
    '[workers]\ncount=3', '[workers]\noverlap_seconds=0',
    '[workers]\nchunk_seconds=10', '[workers]\napi_memory_gib=5',
    '[resources]\ncpus=1\n[workers]\ncount=2',
])
def test_invalid_worker_config_fails_closed(tmp_path, body):
    path = tmp_path / 'recordings.toml'
    path.write_text(body)
    with pytest.raises(ValueError):
        installer.load_config(path)


def test_isolated_credentials_cannot_use_legacy_publication(tmp_path):
    from fastapi.testclient import TestClient
    from argon_recordings.api import Settings, create_app
    app = create_app(Settings(tmp_path, worker_tokens={'one': 'synthetic-isolated-token'}, reserve=0))
    auth = {'Authorization': 'Bearer synthetic-isolated-token'}
    with TestClient(app) as client:
        assert client.post('/api/worker/claim', headers=auth, json={'worker': 'spoof'}).status_code == 404
        assert client.post('/api/worker/jobs/unassigned/complete', headers=auth,
                           json={'duration': 1, 'segments': [], 'artifacts': []}).status_code == 404
        assert client.post('/api/worker/ready', headers=auth).status_code == 200
        assert client.post('/api/worker/tasks/claim', headers=auth).json() is None
        assert client.post('/api/worker/ready', headers=auth, content=b' '* (8*1024**2+1)).status_code == 413
