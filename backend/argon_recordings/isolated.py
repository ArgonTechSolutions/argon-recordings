"""Isolated API and inference entrypoints. Credentials are ephemeral, never Docker env."""
import gc
import ipaddress
import json
import logging
import os
from pathlib import Path
import secrets
import signal
import socket
import sys
import threading
import time

import httpx


class Interfaces:
    """Route by trusted listening socket and peer address, never forwarded headers."""
    def __init__(self, app, subnets):
        self.app = app
        self.worker_routes = tuple(route for route in app.routes if getattr(route, 'path', '').startswith('/api/worker/'))
        self.subnets = [ipaddress.ip_network(value) for value in subnets]

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        from starlette.responses import JSONResponse
        try:
            peer = ipaddress.ip_address(scope['client'][0])
            worker_peer = any(peer in subnet for subnet in self.subnets)
        except (KeyError, TypeError, ValueError):
            worker_peer = False
        worker_port = scope.get('server', ('', 0))[1] == 8001
        from starlette.routing import Match
        worker_path = scope.get('path', '').startswith('/api/worker/')
        registered = any(route.matches(scope)[0] == Match.FULL for route in self.worker_routes)
        if (worker_port and (not worker_peer or not worker_path or not registered)) or (not worker_port and (worker_peer or worker_path)):
            return await JSONResponse({'detail': 'Not found'}, status_code=404)(scope, receive, send)
        scope['worker_interface'] = worker_port
        await self.app(scope, receive, send)


def write_credentials(index, value):
    folder = Path('/run/worker-credentials') / str(index)
    if not folder.is_dir():
        raise RuntimeError('RAM-backed worker credential mount is missing')
    target = folder / 'credential.json'
    temporary = folder / 'credential.new'
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as out:
        os.fchmod(out.fileno(), 0o600)
        json.dump(value, out)
    temporary.replace(target)


def api():
    from .api import Settings, create_app
    import uvicorn
    count = int(os.environ['RECORDINGS_WORKER_COUNT'])
    subnets = os.environ.get('RECORDINGS_WORKER_SUBNETS', '').split(',')
    if not 1 <= count <= 8 or not all(subnets):
        raise RuntimeError('Explicit worker count and internal network subnets are required')
    # Validate before generating or publishing credentials.
    for subnet in subnets:
        ipaddress.ip_network(subnet)
    tokens = {}
    for index in range(1, count + 1):
        token = secrets.token_urlsafe(32)
        worker_id = 'worker-' + str(index)
        tokens[worker_id] = token
        write_credentials(index, {'id': worker_id, 'token': token})
    settings = Settings(worker_token='', worker_tokens=tokens, worker_limit=count,
                        chunk_seconds=int(os.environ.get('RECORDINGS_CHUNK_SECONDS', '180')),
                        overlap_seconds=float(os.environ.get('RECORDINGS_OVERLAP_SECONDS', '2')))
    app = Interfaces(create_app(settings), subnets)
    sockets = []
    try:
        for port in [8000, 8001]:
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(('0.0.0.0', port))
            listener.listen(128)
            sockets.append(listener)
        server = uvicorn.Server(uvicorn.Config(app, proxy_headers=False, access_log=False, timeout_graceful_shutdown=5))
        server.run(sockets=sockets)
    finally:
        for listener in sockets:
            listener.close()


def worker():
    from .worker import Turbo, run_once
    work = Path('/work')
    if not work.is_dir():
        raise RuntimeError('Private worker workspace is missing')
    transcriber = Turbo('/models/turbo')
    stopping = threading.Event()
    for signum in [signal.SIGTERM, signal.SIGINT]:
        signal.signal(signum, lambda *_: stopping.set())
    last_work = time.monotonic()
    while not stopping.is_set():
        watchdog = None
        try:
            credential = json.loads(Path('/run/worker-credentials/self/credential.json').read_text())
            if credential['id'] != os.environ['RECORDINGS_WORKER_ID'] or len(credential['token']) < 40:
                raise ValueError('Invalid private worker credential')
            with httpx.Client(base_url='http://api:8001', headers={'Authorization': 'Bearer ' + credential['token']},
                              timeout=60, trust_env=False) as client:
                client.post('/api/worker/ready').raise_for_status()
                timeout = int(os.environ.get('RECORDINGS_JOB_TIMEOUT_SECONDS', '0'))
                watchdog = threading.Timer(timeout, lambda: os._exit(124)) if timeout else None
                if watchdog:
                    watchdog.daemon = True
                    watchdog.start()
                if os.statvfs(work).f_bavail * os.statvfs(work).f_frsize < 1024**3:
                    raise RuntimeError('Worker requires 1GiB free workspace')
                worked = run_once(client, transcriber, work)
            if worked:
                last_work = time.monotonic()
            else:
                if transcriber.model is not None and time.monotonic() - last_work > 120:
                    transcriber.model = None
                    gc.collect()
                stopping.wait(3)
        except FileNotFoundError:
            # API startup/restart atomically publishes a new in-memory credential.
            stopping.wait(3)
        except Exception as exc:
            logging.error('Isolated worker task failed (%s)', type(exc).__name__)
            stopping.wait(3)
        finally:
            if watchdog:
                watchdog.cancel()


def main():
    if sys.argv[1:] == ['api']:
        api()
    elif sys.argv[1:] == ['worker']:
        worker()
    else:
        raise SystemExit('Choose api or worker role')


if __name__ == '__main__':
    main()
