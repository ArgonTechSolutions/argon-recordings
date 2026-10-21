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
    def fake_ffmpeg(args):
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


def test_long_input_is_rejected_before_decode(tmp_path):
    class Result:
        stdout = json.dumps({'format': {'duration': '301'}, 'streams': []})
    with patch('argon_recordings.worker.subprocess.run', return_value=Result()), patch('argon_recordings.worker.ffmpeg') as decode:
        with pytest.raises(RuntimeError, match='five minutes'):
            extract(tmp_path / 'source', tmp_path)
        decode.assert_not_called()
