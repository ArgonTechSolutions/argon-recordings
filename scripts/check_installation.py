"""Bounded smoke of an explicitly selected disposable installation; preserves its volumes."""
import argparse
import io
import json
from pathlib import Path
import subprocess
import sys
import time
import urllib.error
import urllib.request
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import recordings  # noqa: E402

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--config', type=Path, required=True)
args = parser.parse_args()
config = recordings.load_config(args.config)
base = f"http://127.0.0.1:{config['port']}"


def action(command):
    subprocess.run([sys.executable, str(ROOT / 'recordings.py'), '--config', str(args.config), command], check=True, timeout=150)


def request(path, body=None, method=None, raw=False):
    data = body if raw else json.dumps(body).encode() if body is not None else None
    headers = {'Content-Type': 'application/octet-stream' if raw else 'application/json'}
    with urllib.request.urlopen(urllib.request.Request(base + path, data=data, method=method, headers=headers), timeout=10) as response:
        return json.load(response)


try:
    action('start')
    health = request('/api/health')
    assert health['embedding_profile'] == config['embedding']
    try:
        request('/api/worker/claim', {'worker': 'no-credential'})
        raise AssertionError('Anonymous worker access accepted')
    except urllib.error.HTTPError as exc:
        assert exc.code == 401
    item = recordings.inspect(config)
    assert item['Config']['User'] == '10001:10001'
    assert item['HostConfig']['ReadonlyRootfs'] is True
    assert item['HostConfig']['NanoCpus'] == int(config['cpus'] * 1e9)
    assert item['HostConfig']['PortBindings']['8000/tcp'][0]['HostIp'] == '127.0.0.1'
    assert item['HostConfig']['Memory'] == config['memory'] * 1024**3
    assert item['HostConfig']['MemorySwap'] == item['HostConfig']['Memory']
    assert all(not value.startswith('RECORDINGS_WORKER_TOKEN=') for value in item['Config']['Env'])
    mounts = {m['Destination']: m['Name'] for m in item['Mounts'] if m['Type'] == 'volume'}
    pid = request('/api/projects', {'name': 'Disposable installation smoke'})['id']
    rid = request(f'/api/projects/{pid}/recordings', {'title': 'Synthetic silence smoke', 'filename': 'silence.wav', 'language': 'es'})['id']
    target = io.BytesIO()
    with wave.open(target, 'wb') as out:
        out.setnchannels(1); out.setsampwidth(2); out.setframerate(16000)
        out.writeframes(b'\0\0' * 16000 * 5)
    request(f'/api/recordings/{rid}/source', target.getvalue(), method='PUT', raw=True)
    start = time.monotonic()
    while True:
        recording = request(f'/api/recordings/{rid}')
        if recording['status'] == 'ready':
            break
        if recording['status'] == 'failed' or time.monotonic() - start > 180:
            raise AssertionError(f'Automatic worker did not finish: {recording["status"]}: {recording.get("error")}')
        time.sleep(2)
    assert 4.9 < recording['duration'] < 5.1
    assert request(f'/api/recordings/{rid}/transcript')['segments'] == []
    action('stop')
    action('start')
    assert any(p['id'] == pid for p in request('/api/projects'))
    assert request(f'/api/recordings/{rid}')['status'] == 'ready'
    item = recordings.inspect(config)
    assert {m['Destination']: m['Name'] for m in item['Mounts'] if m['Type'] == 'volume'} == mounts
    print(json.dumps(dict(status='passed', uid=10001, embedding_profile=health['embedding_profile'],
                          automatic_turbo_silence_job=True, persistence_after_restart=True,
                          anonymous_worker_denied=True, credential_absent_from_docker_environment=True,
                          cpu_and_memory_limits=True, real_speech_accuracy=False)))
finally:
    action('stop')
