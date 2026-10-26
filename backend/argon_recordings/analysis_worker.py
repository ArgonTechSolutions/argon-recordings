"""Run bounded visual analysis in the shared worker slot, releasing Turbo memory."""
import gc
import hashlib
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time


def run(client, transcriber, work_root=None):
    response = client.post('/api/worker/analysis/claim'); response.raise_for_status()
    task = response.json()
    if not task: return False
    transcriber.model = None; gc.collect()
    base = f'/api/worker/analysis/{task["id"]}'
    headers = {'X-Lease-Id':task['lease_id']}
    done, lost = threading.Event(), threading.Event(); process = None

    def heartbeat():
        while not done.is_set():
            try:
                client.post(base+'/heartbeat',headers=headers,json={'progress':.5,'stage':'Analyzing visual changes'}).raise_for_status()
            except Exception:
                lost.set(); return
            done.wait(20)

    thread = threading.Thread(target=heartbeat,daemon=True); thread.start()
    try:
        from .analysis_jobs import AnalysisResult
        from .visual_analysis import ANALYSIS_RECIPE_VERSION
        if task.get('analysis_recipe_version') != ANALYSIS_RECIPE_VERSION or not 0 < task['source_bytes'] <= 8*1024**3:
            raise RuntimeError('Unsupported visual analysis or input budget')
        with tempfile.TemporaryDirectory(prefix='recordings-analysis-',dir=work_root) as directory:
            root = Path(directory)
            if shutil.disk_usage(root).free < task['source_bytes']+128*1024**2:
                raise RuntimeError('Insufficient analysis workspace capacity')
            size, digest = 0, hashlib.sha256()
            with client.stream('GET',base+'/source',headers=headers) as response:
                response.raise_for_status()
                with (root/'source').open('wb') as output:
                    for block in response.iter_bytes(1024**2):
                        size += len(block)
                        if size > task['source_bytes'] or lost.is_set(): raise RuntimeError('Analysis input budget or lease lost')
                        output.write(block); digest.update(block)
            if size != task['source_bytes'] or digest.hexdigest()!=task['source_sha256']:
                raise RuntimeError('Analysis source integrity mismatch')
            with (root/'runtime.log').open('wb') as log:
                interval = task['interval']
                process = subprocess.Popen([sys.executable,'-m','argon_recordings.visual_analysis',
                    '--source',str(root/'source'),'--output',str(root/'result.json'),
                    '--start',str(interval['start']),'--end',str(interval['end'])],
                    stdout=log,stderr=log,start_new_session=True)
                deadline = time.monotonic()+90
                while process.poll() is None:
                    if lost.is_set() or time.monotonic()>deadline: raise RuntimeError('Analysis cancelled or timed out')
                    done.wait(.25)
                if process.returncode: raise RuntimeError('Analysis failed')
            result = root/'result.json'
            if result.stat().st_size>256*1024 or lost.is_set(): raise RuntimeError('Analysis result budget or lease lost')
            body = AnalysisResult.model_validate_json(result.read_text())
            client.post(base+'/complete',headers=headers,json=body.model_dump()).raise_for_status()
        return True
    except Exception:
        if not lost.is_set():
            client.post(base+'/fail',headers=headers,json={'message':'Visual analysis failed. Existing marks and sparse references remain available.'}).raise_for_status()
        raise
    finally:
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
