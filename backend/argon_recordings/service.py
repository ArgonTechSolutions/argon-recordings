"""Bundled local API and supervised one-at-a-time background worker."""
import gc
import json
import logging
import os
from pathlib import Path
import secrets
import signal
import subprocess
import sys
import threading
import time

import httpx


def worker_loop():
    from .worker import Turbo, run_once
    # Check pinned artifact identity once per process. Load inference weights only for a job.
    transcriber = Turbo('/models/turbo')
    last_work = time.monotonic()
    with httpx.Client(base_url='http://127.0.0.1:8000', headers={'Authorization': 'Bearer ' + os.environ['RECORDINGS_WORKER_TOKEN']},
                      timeout=60, trust_env=False) as client:
        while True:
            # A native call cannot defer this deadline: terminate the child process.
            watchdog = threading.Timer(1800, lambda: os._exit(124))
            watchdog.daemon = True
            watchdog.start()
            try:
                space = os.statvfs('/work')
                if space.f_bavail * space.f_frsize < 1024**3:
                    raise RuntimeError('Worker requires at least 1 GiB free work space')
                worked = run_once(client, transcriber, Path('/work'))
                if worked:
                    last_work = time.monotonic()
                else:
                    if transcriber.model is not None and time.monotonic() - last_work > 120:
                        transcriber.model = None
                        gc.collect()
                    time.sleep(3)
            except Exception as exc:
                # Avoid formatting HTTP clients/headers or credentials in logs.
                logging.error('Worker task failed (%s); inspect the recording error in the app', type(exc).__name__)
                time.sleep(3)
            finally:
                watchdog.cancel()


def main():
    if '--worker-loop' in sys.argv:
        worker_loop()
        return
    # Owner-approved bundled behavior. Generate after Docker creates the container;
    # this value never appears in image metadata, Docker Config.Env, config files or logs.
    environment = dict(os.environ, RECORDINGS_WORKER_TOKEN=secrets.token_urlsafe(32))
    stopping = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stopping.set())
    api = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'argon_recordings.api:create_app', '--factory',
                            '--host', '0.0.0.0', '--port', '8000', '--no-proxy-headers', '--no-access-log'], env=environment)
    worker = None
    code = 0
    try:
        with httpx.Client(base_url='http://127.0.0.1:8000', timeout=1, trust_env=False) as client:
            for _ in range(120):
                if stopping.is_set() or api.poll() is not None:
                    raise RuntimeError('API stopped before startup')
                try:
                    if client.get('/api/health').is_success:
                        break
                except httpx.HTTPError:
                    pass
                stopping.wait(1)
            else:
                raise RuntimeError('API startup timed out')
        worker_command = [sys.executable, '-m', 'argon_recordings.service', '--worker-loop']
        worker = subprocess.Popen(worker_command, env=environment)
        while not stopping.wait(1):
            if api.poll() is not None:
                raise RuntimeError('API exited')
            if worker.poll() is not None:
                if worker.returncode == 124:
                    # Publication remains fenced; a new worker waits for the old lease to expire.
                    logging.error('Worker job deadline reached; restarting worker')
                    worker = subprocess.Popen(worker_command, env=environment)
                else:
                    raise RuntimeError('Background worker exited')
    except Exception as exc:
        logging.error('%s', exc)
        code = 1
    finally:
        for process in [worker, api]:
            if process and process.poll() is None:
                process.terminate()
        for process in [worker, api]:
            if process:
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
    sys.exit(code)


if __name__ == '__main__':
    main()
