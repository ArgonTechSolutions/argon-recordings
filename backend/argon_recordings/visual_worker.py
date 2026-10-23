"""Cancellable visual subprocess using the same worker slot as transcription."""
import gc
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time


def run(client, transcriber, work_root=None):
    response = client.post('/api/worker/visual/claim')
    response.raise_for_status()
    task = response.json()
    if not task: return False
    transcriber.model = None
    gc.collect()
    base = f'/api/worker/visual/{task["id"]}'
    headers = {'X-Lease-Id':task['lease_id']}
    done, lost = threading.Event(), threading.Event()
    process = None

    def heartbeat():
        while not done.is_set():
            try:
                client.post(base+'/heartbeat',headers=headers,json={'progress':.5,'stage':'Extracting visual evidence'}).raise_for_status()
            except Exception:
                lost.set(); return
            done.wait(20)

    thread = threading.Thread(target=heartbeat,daemon=True); thread.start()
    try:
        from .visual_jobs import RECIPE_VERSION
        if task.get('recipe_version') != RECIPE_VERSION or not 0 < task['source_bytes'] <= 8*1024**3:
            raise RuntimeError('Unsupported visual recipe or input budget')
        with tempfile.TemporaryDirectory(prefix='recordings-visual-',dir=work_root) as directory:
            root = Path(directory)
            if shutil.disk_usage(root).free < task['source_bytes']+256*1024**2:
                raise RuntimeError('Insufficient visual workspace capacity')
            size, digest = 0, hashlib.sha256()
            with client.stream('GET',base+'/source',headers=headers) as response:
                response.raise_for_status()
                with (root/'source').open('wb') as output:
                    for block in response.iter_bytes(1024**2):
                        size += len(block)
                        if size > task['source_bytes'] or lost.is_set():
                            raise RuntimeError('Visual source exceeds budget or lease lost')
                        output.write(block); digest.update(block)
            if size != task['source_bytes'] or digest.hexdigest()!=task['source_sha256']:
                raise RuntimeError('Visual source integrity mismatch')
            (root/'recipe.json').write_text(json.dumps(task['recipe']))
            with (root/'runtime.log').open('wb') as log:
                process = subprocess.Popen([sys.executable,'-m','argon_recordings.visual_runtime',
                    '--source',str(root/'source'),'--output',str(root/'output'),
                    '--recipe',str(root/'recipe.json'),'--manifest',str(root/'manifest.json')],stdout=log,stderr=log,
                    start_new_session=True)
                deadline = time.monotonic()+180
                while process.poll() is None:
                    if lost.is_set() or time.monotonic()>deadline:
                        raise RuntimeError('Visual extraction cancelled or timed out')
                    done.wait(.25)
                if process.returncode: raise RuntimeError('Visual extraction failed')
            manifest_path = root/'manifest.json'
            if manifest_path.stat().st_size>16384 or lost.is_set(): raise RuntimeError('Invalid visual result budget')
            from .visual_jobs import VisualManifest
            body = VisualManifest.model_validate_json(manifest_path.read_text())
            path = root/'output'/body.filename
            if path.stat().st_size != body.bytes: raise RuntimeError('Invalid visual result size')
            with path.open('rb') as stream:
                client.put(base+'/media',headers=headers,content=stream).raise_for_status()
            client.post(base+'/complete',headers=headers,json=body.model_dump()).raise_for_status()
        return True
    except Exception:
        if not lost.is_set():
            client.post(base+'/fail',headers=headers,json={'message':'Visual extraction failed; try a shorter clip or smaller image. Retained references remain available.'}).raise_for_status()
        raise
    finally:
        # Stop both Python and any FFmpeg descendants on cancellation.
        if process is not None:
            import os
            import signal
            try: os.killpg(process.pid,signal.SIGTERM)
            except ProcessLookupError: pass
            if process.poll() is None:
                try: process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    try: os.killpg(process.pid,signal.SIGKILL)
                    except ProcessLookupError: pass
                    process.wait(timeout=5)
        done.set(); thread.join(timeout=5)
