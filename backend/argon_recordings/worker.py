"""One-job pull worker. Model weights are supplied explicitly; never downloaded here."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import wave

import httpx

from .contracts import Completion, Segment

MODEL_REVISION = '0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf'


class Turbo:
    def __init__(self, directory):
        self.directory = Path(directory)
        manifest = json.loads((self.directory / 'benchmark-manifest.json').read_text())
        if manifest.get('revision') != MODEL_REVISION or manifest.get('repo') != 'mobiuslabsgmbh/faster-whisper-large-v3-turbo':
            raise RuntimeError('Supply the pinned Turbo model prepared by the benchmark package')
        required = {'config.json', 'model.bin', 'preprocessor_config.json', 'tokenizer.json', 'vocabulary.json'}
        if set(manifest.get('files', {})) != required:
            raise RuntimeError('Incomplete model manifest')
        for filename, expected in manifest['files'].items():
            digest = hashlib.sha256()
            with (self.directory / filename).open('rb') as stream:
                for chunk in iter(lambda: stream.read(4 * 1024**2), b''):
                    digest.update(chunk)
            if digest.hexdigest() != expected:
                raise RuntimeError('Model checksum mismatch')
        self.model = None

    def transcribe(self, audio, language, progress):
        if self.model is None:
            from faster_whisper import WhisperModel
            self.model = WhisperModel(str(self.directory), device='cpu', compute_type='int8',
                                      cpu_threads=2, num_workers=1, local_files_only=True)
        segments, _ = self.model.transcribe(str(audio), language=None if language == 'auto' else language,
                                            beam_size=5, temperature=0, word_timestamps=True, vad_filter=True)
        result = []
        for s in segments:
            result.append(Segment(start=s.start, end=s.end, text=s.text,
                                  words=[dict(start=w.start, end=w.end, word=w.word, probability=w.probability) for w in (s.words or [])]))
            progress(s.end)
        return result


def ffmpeg(arguments):
    subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-threads', '2', *arguments], check=True, timeout=120)


def extract(source, output):
    probe = subprocess.run(['ffprobe', '-v', 'error', '-show_format', '-show_streams', '-of', 'json', str(source)],
                           check=True, capture_output=True, text=True, timeout=20)
    metadata = json.loads(probe.stdout)
    duration = float(metadata['format']['duration'])
    if not 0 < duration <= 300:
        raise RuntimeError('Foundation profile accepts recordings up to five minutes')
    audio = output / 'audio.wav'
    # Decode at most 300 seconds even if the source's duration metadata is dishonest.
    ffmpeg(['-i', str(source), '-t', '300', '-map', '0:a:0', '-vn', '-ac', '1', '-ar', '16000',
            '-threads', '2', '-c:a', 'pcm_s16le', str(audio)])
    with wave.open(str(audio)) as stream:
        duration = stream.getnframes() / stream.getframerate()
    if duration < 1:
        raise RuntimeError('Recording contains less than one second of audio')
    artifacts = [dict(name='audio.wav', kind='audio', time=None)]
    if any(s['codec_type'] == 'video' for s in metadata['streams']):
        for i, timestamp in enumerate(range(0, int(duration) + 1, 15)):
            if timestamp >= duration:
                break
            path = output / f'frame-{i:04d}.jpg'
            ffmpeg(['-ss', str(timestamp), '-i', str(source), '-an', '-frames:v', '1',
                    '-vf', "scale='min(1280,iw)':-2", '-q:v', '3', '-threads', '2', str(path)])
            if path.is_file():
                artifacts.append(dict(name=path.name, kind='frame', time=float(timestamp)))
    return duration, audio, artifacts


def run_once(client, transcriber, work_root=None):
    claim = client.post('/api/worker/claim', json={'worker': 'cpu-turbo'})
    claim.raise_for_status()
    job = claim.json()
    if not job:
        return False
    jid, lease = job['job_id'], job['lease_id']
    headers = {'X-Lease-Id': lease}
    cancelled, done = threading.Event(), threading.Event()
    progress = {'progress': 0.0, 'stage': 'Downloading source'}

    def heartbeat():
        while not done.is_set():
            try:
                response = client.post(f'/api/worker/jobs/{jid}/heartbeat', headers=headers, json=progress.copy())
                response.raise_for_status()
            except Exception:
                cancelled.set()
                return
            done.wait(20)

    thread = threading.Thread(target=heartbeat, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix='recordings-job-', dir=work_root) as directory:
            output = Path(directory)
            source, digest, size = output / 'source', hashlib.sha256(), 0
            with client.stream('GET', f'/api/worker/jobs/{jid}/source', headers=headers) as response:
                response.raise_for_status()
                with source.open('wb') as stream:
                    for block in response.iter_bytes(1024**2):
                        size += len(block)
                        if cancelled.is_set() or size > 256 * 1024**2:
                            raise RuntimeError('Lease lost or source exceeds worker budget')
                        digest.update(block)
                        stream.write(block)
            if size != job['recording']['bytes'] or digest.hexdigest() != job['recording']['sha256']:
                raise RuntimeError('Source integrity mismatch')
            progress.update(progress=.05, stage='Preparing audio and visual references')
            duration, audio, artifacts = extract(source, output)
            progress.update(progress=.1, stage='Transcribing with Turbo')
            def advance(seconds):
                if cancelled.is_set():
                    raise RuntimeError('Worker lease lost')
                progress['progress'] = min(.9, .1 + .8 * seconds / duration)
            segments = transcriber.transcribe(audio, job['recording']['language'], advance)
            if cancelled.is_set():
                raise RuntimeError('Worker lease lost')
            progress.update(progress=.95, stage='Publishing transcript and references')
            for artifact in artifacts:
                with (output / artifact['name']).open('rb') as stream:
                    response = client.put(f'/api/worker/jobs/{jid}/artifacts/{artifact["name"]}', headers=headers, content=stream)
                    response.raise_for_status()
            completion = Completion(duration=duration, segments=segments, artifacts=artifacts)
            response = client.post(f'/api/worker/jobs/{jid}/complete', headers=headers, json=completion.model_dump())
            response.raise_for_status()
            print(json.dumps({'job_id': jid, 'status': 'completed', 'segments': len(segments)}), flush=True)
        return True
    except Exception as exc:
        # No blind inference retry; the lease fence prevents stale publication.
        if not cancelled.is_set():
            try:
                client.post(f'/api/worker/jobs/{jid}/fail', headers=headers, json={'message': str(exc)[:500]}).raise_for_status()
            except Exception:
                pass
        raise
    finally:
        done.set()
        thread.join(timeout=5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--api-url', required=True)
    parser.add_argument('--model-dir', type=Path, required=True)
    parser.add_argument('--work-dir', type=Path, required=True)
    args = parser.parse_args()
    token = os.environ.get('RECORDINGS_WORKER_TOKEN')
    if not token:
        parser.error('Supply RECORDINGS_WORKER_TOKEN through your private environment')
    if not args.work_dir.is_dir() or os.statvfs(args.work_dir).f_bavail * os.statvfs(args.work_dir).f_frsize < 1024**3:
        parser.error('Work directory must exist on disk with at least 1 GiB free')
    with httpx.Client(base_url=args.api_url, headers={'Authorization': 'Bearer ' + token}, timeout=60, trust_env=False) as client:
        # Model verification occurs only once and no weights are downloaded.
        run_once(client, Turbo(args.model_dir), args.work_dir)


if __name__ == '__main__':
    main()
