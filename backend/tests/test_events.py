"""Fan-out is shared, commit-driven, bounded, and isolated by project."""
import asyncio
import json

from argon_recordings.events import ProjectEvents
from argon_recordings.store import Store
from test_workflow import client, project


def test_shared_snapshots_coalescing_project_isolation_and_cleanup():
    async def check():
        calls = []
        state = {'one': [{'id': 'a', 'progress': 0}], 'two': [{'id': 'private'}]}
        def snapshot(pid):
            calls.append(pid)
            return state[pid]
        broker = ProjectEvents(snapshot, debounce=.001, keepalive=.05)
        streams = [broker.stream(pid) for pid in ['one', 'one', 'two']]
        try:
            for stream in streams:
                assert 'retry: 3000' in await anext(stream)
            frames = await asyncio.gather(*(anext(stream) for stream in streams))
            assert calls.count('one') == 1 and calls.count('two') == 1
            assert frames[0] == frames[1] and 'private' not in frames[0]
            assert 'private' in frames[2]
            calls.clear()
            # No database polling: idle connections only emit transport keepalives.
            assert await anext(streams[0]) == ': keepalive\n\n'
            assert calls == []
            for number in range(1, 4):
                state['one'] = [{'id': 'a', 'progress': number}]
                broker.notify()
                await asyncio.sleep(.02)
            assert all(queue.qsize() <= 1 for queues in broker.subscribers.values() for queue in queues)
            assert json.loads((await anext(streams[1])).split('data: ')[1]) == state['one']
            assert await anext(streams[2]) == ': keepalive\n\n'  # unchanged project receives no data
            # Reconnect gets the latest state even with no retained event history.
            reconnect = broker.stream('one')
            await anext(reconnect)
            assert '"progress":3' in await anext(reconnect)
            await reconnect.aclose()
        finally:
            for stream in streams:
                await stream.aclose()
            assert not broker.subscribers and not broker.latest
            await broker.close()
    asyncio.run(check())


def test_store_notifies_only_successful_write_commits(tmp_path):
    store = Store(tmp_path)
    calls = []
    store.on_commit = lambda: calls.append('commit')
    with store.db() as db:
        db.execute('SELECT * FROM projects').fetchall()
    assert calls == []
    try:
        with store.db() as db:
            db.execute("INSERT INTO projects VALUES('rolled-back','Hidden',0)")
            raise ValueError('abort')
    except ValueError:
        pass
    assert calls == []
    with store.db() as db:
        db.execute("INSERT INTO projects VALUES('saved','Visible',0)")
    assert calls == ['commit']


def test_event_route_preserves_project_and_origin_checks(client):
    pid = project(client)
    assert client.get('/api/projects/missing/events').status_code == 404
    assert client.get(f'/api/projects/{pid}/events', headers={'Origin': 'https://other.example'}).status_code == 403
    assert client.get(f'/api/projects/{pid}/events', headers={'Sec-Fetch-Site': 'cross-site'}).status_code == 403


def test_live_sse_snapshots_reconnect_and_bounded_shutdown(tmp_path):
    """Exercise actual HTTP streaming; TestClient buffers never-ending responses."""
    import os
    import socket
    import subprocess
    import sys
    import time
    import httpx

    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    environment = dict(os.environ, RECORDINGS_DATA=str(tmp_path / 'live'), RECORDINGS_WORKER_TOKEN='synthetic-event-test')
    with (tmp_path / 'server.log').open('w') as log:
        process = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'argon_recordings.api:create_app', '--factory',
                                    '--host', '127.0.0.1', '--port', str(port), '--timeout-graceful-shutdown', '2'],
                                   env=environment, stdout=log, stderr=log)
        try:
            with httpx.Client(base_url=f'http://127.0.0.1:{port}', timeout=5, trust_env=False) as http:
                for _ in range(100):
                    try:
                        if http.get('/api/health').is_success:
                            break
                    except httpx.HTTPError:
                        pass
                    time.sleep(.05)
                else:
                    raise AssertionError('Temporary server failed to start')
                pid = http.post('/api/projects', json={'name': 'Live SSE'}).json()['id']
                url = f'/api/projects/{pid}/events'
                def snapshot(lines):
                    for line in lines:
                        if line.startswith('data: '):
                            return json.loads(line[6:])
                    raise AssertionError('Stream closed before snapshot')
                with http.stream('GET', url) as first, http.stream('GET', url) as second:
                    assert first.headers['content-type'].startswith('text/event-stream')
                    assert first.headers['x-accel-buffering'] == 'no'
                    first_lines, second_lines = first.iter_lines(), second.iter_lines()
                    assert snapshot(first_lines) == [] and snapshot(second_lines) == []
                    recording = http.post(f'/api/projects/{pid}/recordings', json={
                        'title': 'Pushed creation', 'filename': 'test.wav', 'language': 'en'}).json()
                    assert snapshot(first_lines)[0]['id'] == recording['id']
                    pushed = snapshot(second_lines)
                    assert pushed == http.get(f'/api/projects/{pid}/recordings').json()
                    # Event ids are deliberately not a replay API: reconnect always resynchronizes.
                    with http.stream('GET', url, headers={'Last-Event-ID': 'old-or-unknown'}) as reconnect:
                        assert snapshot(reconnect.iter_lines())[0]['id'] == recording['id']
                    process.terminate()  # an open viewer must not block an update indefinitely
                    process.wait(timeout=8)
                    assert process.returncode in {0, -15}  # Uvicorn re-raises SIGTERM after cleanup.
                    assert 'Application shutdown complete.' in (tmp_path / 'server.log').read_text()
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
