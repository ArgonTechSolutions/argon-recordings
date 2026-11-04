"""One-task pull worker. Model weights are supplied explicitly; never downloaded here."""
import argparse
import hashlib
import json
import logging
import math
import shutil
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import wave

import httpx
from pydantic import ValidationError

from .contracts import Segment

from .models import TURBO_FILES, TURBO_REPO, TURBO_REVISION

MODEL_REVISION = TURBO_REVISION
logger = logging.getLogger(__name__)
CHUNK_STALL_SECONDS = 300
CHUNK_HEARTBEAT_SECONDS = 5


class InvalidChunkTranscript(ValueError):
    """A safe, fixed diagnostic; never includes transcript text or model inputs."""


class Turbo:
    def __init__(self, directory):
        self.directory = Path(directory)
        manifest = json.loads((self.directory / 'benchmark-manifest.json').read_text())
        if manifest.get('revision') != MODEL_REVISION or manifest.get('repo') != TURBO_REPO:
            raise RuntimeError('Supply the pinned Turbo model prepared by the benchmark package')
        if manifest.get('files') != TURBO_FILES:
            raise RuntimeError('Manifest does not match curated Turbo artifacts')
        for filename, expected in manifest['files'].items():
            digest = hashlib.sha256()
            with (self.directory / filename).open('rb') as stream:
                for chunk in iter(lambda: stream.read(4 * 1024**2), b''):
                    digest.update(chunk)
            if digest.hexdigest() != expected:
                raise RuntimeError('Model checksum mismatch')
        self.model = None

    def _load_model(self):
        if self.model is None:
            from faster_whisper import WhisperModel
            self.model = WhisperModel(str(self.directory), device='cpu', compute_type='int8',
                                      cpu_threads=2, num_workers=1, local_files_only=True)

    def transcribe(self, audio, language, progress, hint=None):
        self._load_model()
        segments, _ = self.model.transcribe(str(audio), language=None if language == 'auto' else language,
                                            beam_size=5, temperature=0, word_timestamps=True, vad_filter=True,
                                            initial_prompt=hint, hotwords=hint)
        result = []
        for s in segments:
            result.append(Segment(start=s.start, end=s.end, text=s.text,
                                  words=[dict(start=w.start, end=w.end, word=w.word, probability=w.probability) for w in (s.words or [])]))
            progress(s.end)
        return result

    def transcribe_chunk(self, audio, language, progress, *, recovery=False, on_recovery=None):
        """One normal pass and at most one distinct recovery, with early validation.

        Chunk retries start with recovery directly. No words are dropped, retimed,
        or accepted outside the coordinator's existing timestamp constraints.
        """
        self._load_model()
        with wave.open(str(audio), 'rb') as stream:
            duration = stream.getnframes() / stream.getframerate()
        from .chunk_audio import WordTimelineError, validate_words
        for alternate in ([True] if recovery else [False, True]):
            if alternate and on_recovery:
                on_recovery()
            started = time.monotonic()
            segments, _ = self.model.transcribe(
                str(audio), language=None if language == 'auto' else language,
                beam_size=1 if alternate else 5, best_of=1, temperature=0,
                word_timestamps=True, vad_filter=not alternate,
                condition_on_previous_text=False)
            result = []

            def checked():
                for raw in segments:
                    try:
                        segment = Segment(start=raw.start, end=raw.end, text=raw.text,
                                          words=[dict(start=w.start, end=w.end, word=w.word,
                                                      probability=w.probability) for w in (raw.words or [])])
                    except ValidationError:
                        raise InvalidChunkTranscript('Invalid segment or word interval') from None
                    if len(result) >= 4000:
                        raise InvalidChunkTranscript('Chunk segment count exceeds supported limit')
                    if segment.end > duration + .01 or (result and segment.start < result[-1].start):
                        raise InvalidChunkTranscript('Chunk timestamps must be local and ordered')
                    if segment.text.strip() and not segment.words:
                        raise InvalidChunkTranscript('Word timestamps required for safe reconciliation')
                    result.append(segment)
                    yield segment.model_dump()
                    progress(segment.end)

            try:
                validate_words(checked(), 0, duration)
                if alternate and not result:
                    raise InvalidChunkTranscript('Recovery produced no timed speech; audio review required')
            except (InvalidChunkTranscript, WordTimelineError) as exc:
                reason = str(exc)  # Only fixed messages from the validators above.
                logger.warning('chunk_validation_rejected recovery=%s segments=%d words=%d elapsed_seconds=%.1f reason=%s',
                               alternate, len(result), sum(len(s.words) for s in result), time.monotonic()-started, reason)
                if alternate:
                    raise RuntimeError('Chunk transcription recovery failed: ' + reason) from None
            else:
                logger.info('chunk_transcription_validated recovery=%s segments=%d words=%d elapsed_seconds=%.1f',
                            alternate, len(result), sum(len(s.words) for s in result), time.monotonic()-started)
                return result
            finally:
                if close := getattr(segments, 'close', None):
                    close()


def ffmpeg(arguments, timeout=120):
    subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-threads', '2', *arguments], check=True, timeout=timeout)


def extract(source, output):
    probe = subprocess.run(['ffprobe', '-v', 'error', '-show_format', '-show_streams', '-of', 'json', str(source)],
                           check=True, capture_output=True, text=True, timeout=20)
    metadata = json.loads(probe.stdout)
    duration = float(metadata['format']['duration'])
    if not math.isfinite(duration) or duration <= 0:
        raise RuntimeError('Recording duration must be positive and finite')
    audio_budget = min(shutil.disk_usage(output).free - 1024**3, 4 * 1024**3 - 1024**2)
    if duration * 32000 + 4096 > audio_budget:
        raise RuntimeError('Insufficient workspace or WAV format capacity for retained audio')
    audio = output / 'audio.wav'
    # Bound disk use even when the source duration metadata is dishonest.
    # Preserve leading track offsets so transcript times still match video time.
    ffmpeg(['-i', str(source), '-fs', str(audio_budget), '-map', '0:a:0', '-vn', '-ac', '1', '-ar', '16000',
            '-af', 'aresample=async=1:first_pts=0', '-threads', '2', '-c:a', 'pcm_s16le', str(audio)], timeout=max(120, min(3600, math.ceil(duration))))
    if audio.stat().st_size >= audio_budget - 4096:
        raise RuntimeError('Audio decode exceeded the workspace budget; no partial transcript published')
    with wave.open(str(audio)) as stream:
        duration = stream.getnframes() / stream.getframerate()
    if duration < 1:
        raise RuntimeError('Recording contains less than one second of audio')
    artifacts = [dict(name='audio.wav', kind='audio', time=None)]
    if any(s['codec_type'] == 'video' for s in metadata['streams']):
        for i, timestamp in enumerate(range(0, math.ceil(duration), max(15, math.ceil(duration / 256)))):
            if timestamp >= duration:
                break
            path = output / f'frame-{i:04d}.jpg'
            ffmpeg(['-ss', str(timestamp), '-i', str(source), '-an', '-frames:v', '1',
                    '-vf', "scale='min(1280,iw)':-2", '-q:v', '3', '-threads', '2', str(path)])
            if path.is_file():
                artifacts.append(dict(name=path.name, kind='frame', time=float(timestamp)))
    return duration, audio, artifacts


class TranscriptionTracker:
    """Coverage on the source timeline, not a guessed total of Whisper passages."""
    def __init__(self, duration, clock=time.monotonic):
        self.duration, self.clock = duration, clock
        self.started = clock()
        self.position = (0.0, 0)

    def advance(self, seconds):
        previous, passages = self.position
        self.position = (min(self.duration, max(previous, seconds)), passages + 1)

    def snapshot(self):
        seconds, passages = self.position
        elapsed = max(0, self.clock() - self.started)
        # Wait for several passages and meaningful work before estimating.
        eta = elapsed * (self.duration - seconds) / seconds if elapsed >= 30 and seconds >= 30 and passages >= 3 else None
        return dict(audio_processed_seconds=seconds, audio_total_seconds=self.duration,
                    passages=passages, elapsed_seconds=elapsed, eta_seconds=eta)


def run_once(client, transcriber, work_root=None):
    claim = client.post('/api/worker/tasks/claim')
    claim.raise_for_status()
    job = claim.json()
    if not job:
        if run_correction(client, transcriber, work_root):
            return True
        from .visual_worker import run as run_visual
        if run_visual(client, transcriber, work_root):
            return True
        from .analysis_worker import run as run_analysis
        if run_analysis(client, transcriber, work_root):
            return True
        from .diarization_worker import run as run_diarization
        return run_diarization(client, transcriber, work_root)
    if job.get('kind') == 'chunk':
        return run_chunk(client, transcriber, job, work_root)
    jid, lease = job['job_id'], job['lease_id']
    headers = {'X-Lease-Id': lease}
    cancelled, done = threading.Event(), threading.Event()
    progress = {'progress': 0.0, 'stage': 'Downloading source'}

    def heartbeat():
        while not done.is_set():
            try:
                snapshot = progress.copy()
                response = client.post(f'/api/worker/jobs/{jid}/heartbeat', headers=headers, json=snapshot)
                response.raise_for_status()
            except Exception:
                cancelled.set()
                return
            done.wait(5)

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
                        if cancelled.is_set() or size > job['recording']['bytes']:
                            raise RuntimeError('Lease lost or source exceeds worker budget')
                        digest.update(block)
                        stream.write(block)
            if size != job['recording']['bytes'] or digest.hexdigest() != job['recording']['sha256']:
                raise RuntimeError('Source integrity mismatch')
            progress.update(progress=.05, stage='Preparing audio and visual references')
            duration, audio, artifacts = extract(source, output)
            from .chunk_audio import plan
            _, planned = plan(audio, job.get('chunk_seconds', 180), job.get('overlap_seconds', 2))
            progress = dict(progress=.08, stage='Uploading prepared audio and references')
            for artifact in artifacts:
                with (output / artifact['name']).open('rb') as stream:
                    client.put(f'/api/worker/jobs/{jid}/artifacts/{artifact["name"]}', headers=headers, content=stream).raise_for_status()
            manifest = []
            for index, chunk in enumerate(planned):
                if cancelled.is_set():
                    raise RuntimeError('Worker lease lost')
                path = output / f'chunk-{index:04d}.wav'
                crop_audio(audio, path, chunk['start'], chunk['end'])
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                with path.open('rb') as stream:
                    client.put(f'/api/worker/jobs/{jid}/chunks/{index}', headers=headers, content=stream).raise_for_status()
                manifest.append({**chunk,'bytes':path.stat().st_size,'sha256':digest})
                path.unlink()
            client.post(f'/api/worker/jobs/{jid}/prepare', headers=headers,
                        json={'duration':duration,'artifacts':artifacts,'chunks':manifest},timeout=600).raise_for_status()
            print(json.dumps({'job_id': jid, 'status': 'prepared', 'chunks': len(manifest)}), flush=True)
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


def _check_chunk_completion(response):
    if response.status_code == 422:
        # These are fixed coordinator messages. Never persist arbitrary response
        # bodies, validation inputs, transcript text, or credentials as errors.
        reasons = {
            'Chunk timestamps must be local and ordered',
            'Chunk word timestamps must be local and ordered',
            'Word timestamps required for safe reconciliation',
            'Chunk word count exceeds bounded speech density',
            'Word timestamp outside plausible chunk interval',
            'Word timeline overlaps excessively or is unordered',
            'Word timeline exceeds bounded speech density',
        }
        try:
            payload = response.json()
        except ValueError:
            payload = None
        detail = payload.get('detail') if isinstance(payload, dict) else None
        reason = 'Unrecognized validation response'
        if isinstance(detail, str) and detail in reasons:
            reason = detail
        elif isinstance(detail, list):
            reason = 'Transcript schema validation failed'
        raise RuntimeError('Chunk result rejected (HTTP 422): ' + reason)
    response.raise_for_status()


def run_chunk(client, transcriber, job, work_root=None):
    base = f"/api/worker/chunks/{job['id']}"
    headers = {'X-Lease-Id': job['lease_id']}
    done, lost = threading.Event(), threading.Event()
    tracker = TranscriptionTracker(job['end']-job['start'])
    phase = 'Downloading chunk audio'
    started = last_progress = time.monotonic()
    attempt = job.get('attempts', 0) + 1
    logger.info('chunk_started chunk=%s attempt=%d duration_seconds=%.2f',
                job['id'], attempt, job['end']-job['start'])
    def heartbeat():
        last_log = time.monotonic()
        while not done.is_set():
            try:
                now = time.monotonic()
                if now-last_progress >= CHUNK_STALL_SECONDS:
                    lost.set()
                    logger.error('chunk_stalled chunk=%s attempt=%d phase=%s idle_seconds=%.1f restarting_worker=true',
                                 job['id'], attempt, phase, now-last_progress)
                    try:
                        client.post(base+'/fail',headers=headers,
                                    json={'message':'Chunk made no audio progress for 5 minutes; worker restarted'}, timeout=5).raise_for_status()
                    except Exception:
                        # A disconnected coordinator will expire the lease.
                        logger.warning('chunk_stall_report_failed chunk=%s', job['id'])
                    # Native inference cannot be interrupted safely in a thread.
                    # Docker restarts the worker; the next claim uses recovery.
                    os._exit(124)
                    return
                if lost.is_set():
                    done.wait(CHUNK_HEARTBEAT_SECONDS)
                    continue
                client.post(base+'/heartbeat', headers=headers,
                            json={'progress':min(.95, tracker.position[0]/tracker.duration), 'stage':phase}).raise_for_status()
                now = time.monotonic()
                if now - last_log >= 60:
                    logger.info('chunk_activity chunk=%s attempt=%d phase=%s audio_seconds=%.2f elapsed_seconds=%.1f idle_seconds=%.1f',
                                job['id'], attempt, phase, tracker.position[0], now-started, now-last_progress)
                    last_log = now
            except Exception:
                lost.set()
                logger.warning('chunk_heartbeat_failed chunk=%s attempt=%d', job['id'], attempt)
            done.wait(CHUNK_HEARTBEAT_SECONDS)
    thread = threading.Thread(target=heartbeat, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix='recordings-chunk-',dir=work_root) as directory:
            audio = Path(directory)/'chunk.wav'
            digest, size = hashlib.sha256(), 0
            with client.stream('GET',base+'/audio',headers=headers) as response:
                response.raise_for_status()
                with audio.open('wb') as stream:
                    for block in response.iter_bytes(1024**2):
                        size += len(block)
                        if lost.is_set() or size > job['bytes']:
                            raise RuntimeError('Lease lost or chunk exceeds budget')
                        digest.update(block); stream.write(block)
            if size != job['bytes'] or digest.hexdigest() != job['sha256']:
                raise RuntimeError('Chunk integrity mismatch')
            def advance(seconds):
                nonlocal last_progress
                if lost.is_set():
                    raise RuntimeError('Worker lease lost')
                if seconds > tracker.position[0]:
                    last_progress = time.monotonic()
                tracker.advance(seconds)
            def recovery():
                nonlocal phase, tracker, last_progress
                phase = 'Recovering chunk timestamps'
                tracker = TranscriptionTracker(job['end']-job['start'])
                last_progress = time.monotonic()
                logger.warning('chunk_recovery_started chunk=%s attempt=%d strategy=greedy_original_audio', job['id'], attempt)
                client.post(base+'/heartbeat',headers=headers,json={'progress':0,'stage':phase}).raise_for_status()
            phase = 'Transcribing chunk'
            if transcribe_chunk := getattr(transcriber, 'transcribe_chunk', None):
                segments = transcribe_chunk(audio,job['language'],advance,
                                            recovery=bool(job.get('error') or job.get('attempts',0)), on_recovery=recovery)
            else:
                segments = transcriber.transcribe(audio,job['language'],advance)
            if lost.is_set():
                raise RuntimeError('Worker lease lost')
            response = client.post(base+'/complete',headers=headers,json={'segments':[s.model_dump() for s in segments]})
            _check_chunk_completion(response)
            logger.info('chunk_completed chunk=%s attempt=%d elapsed_seconds=%.1f', job['id'], attempt, time.monotonic()-started)
        return True
    except Exception as exc:
        logger.error('chunk_failed chunk=%s attempt=%d error_type=%s elapsed_seconds=%.1f',
                     job['id'], attempt, type(exc).__name__, time.monotonic()-started)
        if not lost.is_set():
            try:
                client.post(base+'/fail',headers=headers,json={'message':str(exc)[:500]}).raise_for_status()
            except Exception:
                pass
        raise
    finally:
        done.set()
        thread.join(timeout=5)


def crop_audio(source, target, start, end):
    with wave.open(str(source), 'rb') as incoming:
        if incoming.getnchannels() != 1 or incoming.getsampwidth() != 2 or incoming.getframerate() != 16000:
            raise RuntimeError('Expected retained 16 kHz mono PCM audio')
        first, last = round(start * 16000), round(end * 16000)
        if first < 0 or last > incoming.getnframes() + 160 or last <= first:
            raise RuntimeError('Passage is outside retained audio')
        incoming.setpos(first)
        with wave.open(str(target), 'wb') as out:
            out.setparams(incoming.getparams())
            out.writeframes(incoming.readframes(last - first))


def run_correction(client, transcriber, work_root=None):
    response = client.post('/api/worker/corrections/claim')
    response.raise_for_status()
    job = response.json()
    if not job:
        return False
    base = f"/api/worker/corrections/{job['id']}"
    headers = {'X-Lease-Id': job['lease_id']}
    done, lost = threading.Event(), threading.Event()
    def heartbeat():
        while not done.is_set():
            try:
                client.post(base + '/heartbeat', headers=headers, json={'progress': .5, 'stage': 'Retrying selected passage'}).raise_for_status()
            except Exception:
                lost.set()
                return
            done.wait(20)
    thread = threading.Thread(target=heartbeat, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix='recordings-correction-', dir=work_root) as directory:
            audio, crop = Path(directory) / 'retained.wav', Path(directory) / 'passage.wav'
            size = 0
            with client.stream('GET', base + '/audio', headers=headers) as response:
                response.raise_for_status()
                with audio.open('wb') as out:
                    for block in response.iter_bytes(1024**2):
                        size += len(block)
                        if size > math.ceil(job['recording_duration'] * 32000) + 1024**2 or lost.is_set():
                            raise RuntimeError('Audio exceeds budget or lease lost')
                        out.write(block)
            crop_audio(audio, crop, job['start'], job['end'])
            def advance(_):
                if lost.is_set():
                    raise RuntimeError('Correction lease lost')
            segments = transcriber.transcribe(crop, job['language'], advance, hint=job['hint'])
            proposal = []
            for segment in segments:
                value = segment.model_dump()
                value['start'] = max(job['start'], min(job['end'], value['start'] + job['start']))
                value['end'] = max(value['start'], min(job['end'], value['end'] + job['start']))
                for word in value['words']:
                    word['start'] = max(value['start'], min(value['end'], word['start'] + job['start']))
                    word['end'] = max(word['start'], min(value['end'], word['end'] + job['start']))
                proposal.append(Segment(**value).model_dump())
            if lost.is_set():
                raise RuntimeError('Correction lease lost')
            client.post(base + '/complete', headers=headers, json={'segments': proposal}).raise_for_status()
        return True
    except Exception as exc:
        if not lost.is_set():
            client.post(base + '/fail', headers=headers, json={'message': str(exc)[:500]}).raise_for_status()
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
