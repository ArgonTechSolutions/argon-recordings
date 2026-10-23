"""Optional speaker-task runner using a cancellable inference subprocess."""
import gc
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from functools import lru_cache


def capability(directory):
    if not directory:
        return None
    from .diarization_runtime import FILES
    try:
        signature = tuple((p.stat().st_size,p.stat().st_mtime_ns) for p in (Path(directory)/name for name in FILES))
        return verified_capability(str(directory),signature)
    except OSError:
        return None


@lru_cache(maxsize=2)
def verified_capability(directory, signature):
    from .diarization_runtime import MODEL, verify
    import importlib.metadata
    try:
        if importlib.metadata.version('sherpa-onnx') != '1.13.8': return None
        verify(directory)
        return MODEL
    except (OSError, ValueError, importlib.metadata.PackageNotFoundError):
        return None


def run(client, transcriber, work_root, directory=None):
    directory = directory or os.environ.get('RECORDINGS_DIARIZATION_DIR')
    if not directory:
        return False
    # Fail locally before claiming if provisioning is absent or corrupt.
    from .diarization_runtime import verify
    verify(directory)
    response = client.post('/api/worker/diarization/claim')
    response.raise_for_status()
    task = response.json()
    if not task:
        return False
    transcriber.model = None
    gc.collect()
    base = f'/api/worker/diarization/{task["id"]}'
    headers = {'X-Lease-Id': task['lease_id']}
    done, lost = threading.Event(), threading.Event()
    def heartbeat():
        while not done.is_set():
            try:
                client.post(base+'/heartbeat', headers=headers, json={'progress': .5, 'stage': 'Analyzing speakers'}).raise_for_status()
            except Exception:
                lost.set()
                return
            done.wait(20)
    thread = threading.Thread(target=heartbeat, daemon=True)
    thread.start()
    process = None
    try:
        from .diarization_runtime import MODEL
        if task.get('model') != MODEL:
            raise RuntimeError('Speaker task requires a different model configuration')
        with tempfile.TemporaryDirectory(prefix='recordings-speakers-', dir=work_root) as temporary:
            root = Path(temporary)
            duration = task['reference']['interval']['end']
            if duration > 7200:
                raise ValueError('Diarization exceeds the two-hour inference memory budget')
            budget = math.ceil(duration * 32000) + 1024**2
            size = 0
            with client.stream('GET', base+'/audio', headers=headers) as response:
                response.raise_for_status()
                with (root/'audio.wav').open('wb') as output:
                    for block in response.iter_bytes(1024**2):
                        size += len(block)
                        if size > budget or lost.is_set():
                            raise RuntimeError('Audio budget exceeded or speaker lease lost')
                        output.write(block)
            (root/'options.json').write_text(json.dumps(task['options']))
            with (root/'runtime.log').open('wb') as log:
                process = subprocess.Popen([sys.executable, '-m', 'argon_recordings.diarization_runtime',
                    '--audio', str(root/'audio.wav'), '--models', str(directory), '--options', str(root/'options.json'),
                    '--output', str(root/'result.json')], stdout=log, stderr=log)
                deadline = time.monotonic() + min(7200, max(180, 2*duration))
                while process.poll() is None:
                    if lost.is_set() or time.monotonic() > deadline:
                        raise RuntimeError('Speaker inference cancelled or timed out')
                    done.wait(.25)
                if process.returncode:
                    raise RuntimeError('Speaker inference failed; inspect optional model/runtime configuration')
            result = root/'result.json'
            if result.stat().st_size > 8*1024**2 or lost.is_set():
                raise RuntimeError('Speaker result budget exceeded or lease lost')
            from .diarization import DiarizationCompletion
            body = DiarizationCompletion.model_validate_json(result.read_text())
            client.post(base+'/complete', headers=headers, json=body.model_dump()).raise_for_status()
        return True
    except Exception:
        if not lost.is_set():
            client.post(base+'/fail', headers=headers, json={'message': 'Speaker analysis failed; retained transcript remains available'}).raise_for_status()
        raise
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        done.set()
        thread.join(timeout=5)
