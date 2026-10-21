import json
from pathlib import Path
from unittest.mock import patch
import wave
import pytest

from argon_recordings.worker import extract


def test_frames_use_explicit_original_time_seeks(tmp_path):
    commands = []
    def fake_run(args, **kwargs):
        class Result:
            stdout = json.dumps({'format': {'duration': '31'}, 'streams': [{'codec_type': 'audio'}, {'codec_type': 'video'}]})
        return Result()
    def fake_ffmpeg(args, **kwargs):
        commands.append(args)
        destination = Path(args[-1])
        if destination.suffix == '.wav':
            with wave.open(str(destination), 'wb') as output:
                output.setnchannels(1); output.setsampwidth(2); output.setframerate(16000)
                output.writeframes(b'\0\0' * 16000 * 31)
        else:
            destination.write_bytes(b'\xff\xd8\xff')
    with patch('argon_recordings.worker.subprocess.run', fake_run), patch('argon_recordings.worker.ffmpeg', fake_ffmpeg):
        duration, audio, artifacts = extract(tmp_path / 'source', tmp_path)
    assert duration == 31 and audio.exists()
    assert [a['time'] for a in artifacts if a['kind'] == 'frame'] == [0, 15, 30]
    assert [args[1] for args in commands[1:]] == ['0', '15', '30']
    assert '-t' not in commands[0]


def test_two_hour_video_has_no_duration_cutoff_and_bounded_frames(tmp_path):
    commands = []
    class Probe:
        stdout = json.dumps({'format': {'duration': '7200'}, 'streams': [{'codec_type': 'audio'}, {'codec_type': 'video'}]})
    class Audio:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def getnframes(self): return 7200 * 16000
        def getframerate(self): return 16000
    def fake_ffmpeg(args, **kwargs):
        commands.append(args)
        Path(args[-1]).write_bytes(b'synthetic')
    with patch('argon_recordings.worker.subprocess.run', return_value=Probe()), patch('argon_recordings.worker.ffmpeg', fake_ffmpeg), patch('argon_recordings.worker.wave.open', return_value=Audio()):
        duration, _, artifacts = extract(tmp_path / 'source', tmp_path)
    assert duration == 7200
    frames = [a for a in artifacts if a['kind'] == 'frame']
    assert 1 < len(frames) <= 256
    assert frames[-1]['time'] > 7100
    assert '-t' not in commands[0]
    assert '-fs' in commands[0]


def test_insufficient_audio_disk_budget_rejects_before_decode(tmp_path):
    class Probe:
        stdout = json.dumps({'format': {'duration': '7200'}, 'streams': []})
    class Disk:
        free = 1024**3 + 100
    with patch('argon_recordings.worker.subprocess.run', return_value=Probe()), patch('argon_recordings.worker.shutil.disk_usage', return_value=Disk()), patch('argon_recordings.worker.ffmpeg') as decode:
        with pytest.raises(RuntimeError, match='workspace'):
            extract(tmp_path / 'source', tmp_path)
        decode.assert_not_called()
