"""Bounded synthetic media/runtime smoke test; no speech/model inference."""
from pathlib import Path
import inspect
import json
import subprocess
import tempfile
import ctranslate2
from faster_whisper import WhisperModel
from argon_recordings.worker import extract

assert 'int8' in ctranslate2.get_supported_compute_types('cpu')
assert 'hotwords' in inspect.signature(WhisperModel.transcribe).parameters
with tempfile.TemporaryDirectory(prefix='recordings-media-check-') as directory:
    root = Path(directory)
    source = root / 'synthetic.mp4'
    subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-threads', '1', '-f', 'lavfi', '-i',
                    'testsrc2=size=320x180:rate=1:duration=31', '-f', 'lavfi', '-i',
                    'anullsrc=r=16000:cl=mono', '-t', '31', '-c:v', 'libx264', '-preset', 'ultrafast',
                    '-threads', '1', '-c:a', 'aac', str(source)], check=True, timeout=30)
    duration, audio, artifacts = extract(source, root)
    frames = [a for a in artifacts if a['kind'] == 'frame']
    assert 31 <= duration < 31.2, duration  # AAC may retain a small encoder-padding tail.
    assert [f['time'] for f in frames] == [0, 15, 30]
    assert audio.stat().st_size < 2 * 1024**2
    for frame in frames:
        assert (root / frame['name']).read_bytes()[:3] == b'\xff\xd8\xff'
    assert (root / frames[0]['name']).read_bytes() != (root / frames[1]['name']).read_bytes()
    delayed = root / 'delayed.mp4'
    subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-threads', '1', '-f', 'lavfi', '-i',
                    'testsrc2=size=160x90:rate=1:duration=6', '-itsoffset', '2', '-f', 'lavfi', '-i',
                    'sine=frequency=440:sample_rate=16000:duration=3', '-c:v', 'libx264', '-preset', 'ultrafast',
                    '-threads', '1', '-c:a', 'aac', str(delayed)], check=True, timeout=30)
    delayed_output = root / 'delayed-output'
    delayed_output.mkdir()
    delayed_duration, delayed_audio, _ = extract(delayed, delayed_output)
    import wave
    import array
    with wave.open(str(delayed_audio)) as stream:
        samples = array.array('h', stream.readframes(stream.getnframes()))
        rate = stream.getframerate()
    assert max(abs(v) for v in samples[:int(rate * 1.5)]) < 20, 'Leading source-time gap was lost'
    assert max(abs(v) for v in samples[int(rate * 2.2):int(rate * 2.8)]) > 100, 'Expected delayed speech signal missing'
    assert delayed_duration > 4.9
    print(json.dumps({'status': 'passed', 'audio_seconds': duration,
                      'frame_times': [f['time'] for f in frames], 'int8': True,
                      'delayed_audio_alignment': True, 'speech_inference': False}))
