"""Synthetic decoder output exercises recovery; it does not prove model accuracy."""
import logging
import threading
from types import SimpleNamespace
import wave

import httpx
import pytest

from argon_recordings.worker import Turbo, run_once
from argon_recordings import worker as worker_module
from test_chunk_pipeline import auth, claim, prepared, result


def segment(start=0, end=1, words=None):
    if words is None:
        words = [dict(start=start, end=end, word=' private speech', probability=.9)]
    return SimpleNamespace(start=start, end=end, text=' private speech',
                           words=[SimpleNamespace(**w) for w in words])


def dense():
    return segment(words=[dict(start=0, end=0, word=' private speech', probability=.9)] * 51)


class Decoder:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def transcribe(self, audio, **kwargs):
        self.calls.append(kwargs)
        response = next(self.responses)
        return iter(response), None


def turbo(*responses):
    value = Turbo.__new__(Turbo)
    value.model = Decoder(responses)
    return value


@pytest.fixture
def audio(tmp_path):
    path = tmp_path/'chunk.wav'
    with wave.open(str(path), 'wb') as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(16000)
        stream.writeframes(b'\0\0' * 32000)
    return path


def test_valid_chunk_keeps_words_and_uses_independent_windows(audio):
    worker = turbo([segment()])
    progress = []
    output = worker.transcribe_chunk(audio, 'es', progress.append)
    assert output[0].words[0].word == ' private speech'
    assert progress == [1]
    assert len(worker.model.calls) == 1
    options = worker.model.calls[0]
    assert options['condition_on_previous_text'] is False
    assert options['beam_size'] == 5 and options['vad_filter'] is True
    assert options['word_timestamps'] is True and options['language'] == 'es'


def test_dense_output_stops_early_and_recovers_once(audio, caplog):
    consumed, closed, recovery = [], [], []

    def repeated():
        try:
            for index in range(10000):
                consumed.append(index)
                yield segment(0, .01, [dict(start=0, end=0, word=' private speech', probability=.9)])
        finally:
            closed.append(True)

    worker = turbo(repeated(), [segment()])
    with caplog.at_level(logging.INFO):
        output = worker.transcribe_chunk(audio, 'auto', lambda _: None,
                                        on_recovery=lambda: recovery.append(True))
    assert len(consumed) == 51 and closed == [True] and recovery == [True]
    assert len(output) == 1 and len(output[0].words) == 1
    options = worker.model.calls[1]
    assert options['vad_filter'] is False and options['beam_size'] == 1
    assert options['condition_on_previous_text'] is False and options['language'] is None
    assert 'Word timeline exceeds bounded speech density' in caplog.text
    assert 'chunk_transcription_validated recovery=True' in caplog.text
    assert 'private speech' not in caplog.text


@pytest.mark.parametrize('invalid', [
    dense(),
    segment(end=3),
    segment(words=[]),
    segment(words=[dict(start=0, end=1, word='a', probability=.9),
                   dict(start=.5, end=1, word='b', probability=.9)]),
    segment(words=[dict(start=0, end=1, word='private speech', probability=2)]),
])
def test_invalid_recovery_fails_without_dropping_or_retiming_words(audio, invalid):
    worker = turbo([invalid], [invalid])
    with pytest.raises(RuntimeError, match='Chunk transcription recovery failed') as error:
        worker.transcribe_chunk(audio, 'es', lambda _: None)
    assert len(worker.model.calls) == 2
    assert 'private speech' not in str(error.value)


def test_retried_chunk_goes_directly_to_recovery(audio):
    worker = turbo([segment()])
    worker.transcribe_chunk(audio, 'es', lambda _: None, recovery=True)
    assert len(worker.model.calls) == 1 and worker.model.calls[0]['vad_filter'] is False


def test_silence_is_valid_but_empty_recovery_requires_review(audio):
    assert turbo([]).transcribe_chunk(audio, 'es', lambda _: None) == []
    with pytest.raises(RuntimeError, match='Recovery produced no timed speech'):
        turbo([dense()], []).transcribe_chunk(audio, 'es', lambda _: None)


def test_lease_loss_aborts_inference_without_a_recovery_pass(audio):
    worker = turbo([segment()])

    def lost(_):
        raise RuntimeError('Worker lease lost')

    with pytest.raises(RuntimeError, match='Worker lease lost'):
        worker.transcribe_chunk(audio, 'es', lost)
    assert len(worker.model.calls) == 1


def test_recovery_completes_failed_chunk_and_retains_prior_work(prepared, tmp_path):
    client, rid, jid, _, _ = prepared
    first = claim(client, 'one')
    assert client.post(f"/api/worker/chunks/{first['id']}/complete",
                       headers=auth('one', first['lease_id']), json=result(0, 1, 'valid')).status_code == 200
    with client.app.state.store.db() as db:
        db.execute("UPDATE chunks SET error=? WHERE job_id=? AND ordinal=1",
                   ('Chunk result rejected (HTTP 422): Word timeline exceeds bounded speech density', jid))
    stages = []

    def forward(request):
        response = client.request(request.method, str(request.url), headers=dict(request.headers), content=request.read())
        if request.url.path.endswith('/heartbeat'):
            stages.append(client.get(f'/api/recordings/{rid}').json()['job']['stage'])
        return response

    worker = turbo([segment()])
    with httpx.Client(base_url='http://testserver', headers=auth('one'),
                      transport=httpx.MockTransport(forward)) as transport:
        assert run_once(transport, worker, tmp_path)
    with client.app.state.store.db() as db:
        rows = db.execute('SELECT ordinal,state FROM chunks WHERE job_id=? ORDER BY ordinal', (jid,)).fetchall()
    assert rows[0]['state'] == rows[1]['state'] == 'completed'
    assert rows[2]['state'] == 'queued'
    assert len(worker.model.calls) == 1 and worker.model.calls[0]['beam_size'] == 1
    assert any('Recovering timestamps for chunk 2/5 · attempt 1/3' in stage for stage in stages)


def test_chunk_attempt_and_phase_are_visible_in_recording_status(prepared):
    client, rid, _, _, _ = prepared
    job = claim(client, 'one')
    assert client.get(f'/api/recordings/{rid}').json()['job']['stage'] == 'Starting chunk 1/5 · attempt 1/3'
    base = f"/api/worker/chunks/{job['id']}"
    headers = auth('one', job['lease_id'])
    assert client.post(base+'/heartbeat', headers=headers,
                       json={'stage':'Recovering chunk timestamps','progress':.5}).status_code == 200
    assert client.get(f'/api/recordings/{rid}').json()['job']['stage'] == 'Recovering timestamps for chunk 1/5 · attempt 1/3 · 50%'
    assert client.post(base+'/fail', headers=headers, json={'message':'Synthetic failure'}).status_code == 200
    assert client.get(f'/api/recordings/{rid}').json()['job']['stage'] == 'Waiting to retry chunk 1/5 · attempt 2/3'


@pytest.mark.parametrize('heartbeat_fails', [False, True])
def test_no_progress_restarts_stalled_inference_and_preserves_completed_work(prepared, tmp_path, monkeypatch, heartbeat_fails):
    client, rid, jid, _, _ = prepared
    first = claim(client, 'one')
    assert client.post(f"/api/worker/chunks/{first['id']}/complete",
                       headers=auth('one', first['lease_id']), json=result(0, 1, 'valid')).status_code == 200
    release = threading.Event()
    decoding = threading.Event()
    exits = []

    class Stalled:
        def transcribe(self, audio, language, advance):
            decoding.set()
            assert release.wait(5), 'Stall watchdog did not run'
            return []

    def exit_worker(code):
        exits.append(code)
        release.set()

    def forward(request):
        if heartbeat_fails and decoding.is_set() and request.url.path.endswith('/heartbeat'):
            return httpx.Response(503)
        return client.request(request.method, str(request.url), headers=dict(request.headers), content=request.read())

    monkeypatch.setattr(worker_module, 'CHUNK_STALL_SECONDS', .1)
    monkeypatch.setattr(worker_module, 'CHUNK_HEARTBEAT_SECONDS', .01)
    monkeypatch.setattr(worker_module.os, '_exit', exit_worker)
    with httpx.Client(base_url='http://testserver', headers=auth('one'),
                      transport=httpx.MockTransport(forward)) as transport:
        with pytest.raises(RuntimeError, match='Worker lease lost'):
            run_once(transport, Stalled(), tmp_path)
    assert exits == [124]
    with client.app.state.store.db() as db:
        states = db.execute('SELECT state,error FROM chunks WHERE job_id=? ORDER BY ordinal', (jid,)).fetchall()
    assert states[0]['state'] == 'completed' and states[1]['state'] == 'queued'
    assert states[1]['error'] == 'Chunk made no audio progress for 5 minutes; worker restarted'
    assert 'attempt 2/3' in client.get(f'/api/recordings/{rid}').json()['job']['stage']
