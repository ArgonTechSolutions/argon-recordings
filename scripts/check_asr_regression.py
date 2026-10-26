"""Real CPU Turbo stability and saved-boundary regression on a retained public fixture."""
import argparse
import copy
import hashlib
from importlib.metadata import version
import io
import json
from pathlib import Path
import resource
import subprocess
import sys
import tarfile
import tempfile
import time
import wave

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_SHA = 'b9d43bf8ceb535d3efe4ba7d0424bdf502b5b3220836d167192775d9391fad3a'


def dump(path, value):
    path.write_text(json.dumps(value,indent=2)+'\n')


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def phase(args):
    sys.path.insert(0,str(args.source/'backend'))
    from argon_recordings.worker import Turbo, crop_audio
    from argon_recordings.chunk_audio import plan, reconcile
    started = time.monotonic(); model = Turbo(args.model)
    result = {'model_verification_seconds':time.monotonic()-started}
    calls = []
    def transcribe(path,label):
        print(args.phase+': '+label,flush=True)
        begin = time.monotonic(); minute = [-1]
        def progress(position):
            current = int(position//60)
            if current > minute[0]:
                minute[0] = current
                print(f'{args.phase}: {label} reached {current*60}s',flush=True)
        value = [s.model_dump() for s in model.transcribe(path,'en',progress)]
        calls.append({'label':label,'seconds':time.monotonic()-begin,'segments':len(value),
                      'words':sum(len(s['words']) for s in value),'sha256':digest(value)})
        dump(args.output/(args.phase+'-'+label+'.json'),value)
        return value
    first = transcribe(args.output/'input-240.wav','first-call')
    result['first_call_sha256'] = digest(first)
    if args.phase == 'current':
        warm = transcribe(args.output/'input-240.wav','warm-call')
        result['warm_matches_first'] = warm == first
        _, tasks = plan(args.output/'input-240.wav')
        rows = []
        with tempfile.TemporaryDirectory(prefix='argon-asr-chunks-') as directory:
            for index,task in enumerate(tasks):
                path = Path(directory)/f'{index}.wav'
                crop_audio(args.output/'input-240.wav',path,task['start'],task['end'])
                segments = transcribe(path,f'chunk-{index}')
                for segment in segments:
                    segment['start'] += task['start']; segment['end'] += task['start']
                    for word in segment['words']:
                        word['start'] += task['start']; word['end'] += task['start']
                rows.append({**task,'segments':segments})
        dump(args.output/'saved-chunk-results.json',rows)
        try:
            reconciled = [s.model_dump() for s in reconcile(rows)]
            omitted = copy.deepcopy(rows)
            for row in omitted:
                if row['boundary']: row['segments'] = []
            missing_context = [s.model_dump() for s in reconcile(omitted)]
            cores_only = [s.model_dump() for s in reconcile([r for r in rows if not r['boundary']])]
            replayed = [s.model_dump() for s in reconcile(json.loads(json.dumps(omitted)))]
            dump(args.output/'reconciled.json',reconciled)
            dump(args.output/'omitted-context.json',missing_context)
            result['boundary'] = {'core_chunks':sum(not r['boundary'] for r in rows),
                'context_chunks':sum(r['boundary'] for r in rows),'plan':tasks,
                'omitted_context_matches_core_only':missing_context==cores_only,
                'saved_json_replay_identical':replayed==missing_context,
                'replay_inference_calls':0,'reconciled_sha256':digest(reconciled),
                'omitted_context_sha256':digest(missing_context)}
        except ValueError as error:
            result['boundary'] = {'error':str(error)}
    result.update(calls=calls,process_peak_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                  elapsed_seconds=time.monotonic()-started)
    dump(args.output/(args.phase+'-result.json'),result)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fixture',type=Path)
    parser.add_argument('--model',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--phase',choices=['baseline','current'])
    parser.add_argument('--source',type=Path)
    args = parser.parse_args()
    if args.phase:
        phase(args); return
    assert hashlib.sha256(args.fixture.read_bytes()).hexdigest()==FIXTURE_SHA
    args.output.mkdir(parents=True,exist_ok=False)
    with wave.open(str(args.fixture)) as incoming, wave.open(str(args.output/'input-240.wav'),'wb') as out:
        assert (incoming.getnchannels(),incoming.getsampwidth(),incoming.getframerate())==(1,2,16000)
        out.setparams(incoming.getparams()); out.writeframes(incoming.readframes(240*16000))
    baseline = subprocess.check_output(['git','rev-parse','3d4eda7^{commit}'],cwd=ROOT,text=True).strip()
    archive = subprocess.check_output(['git','archive',baseline,'backend'],cwd=ROOT)
    with tempfile.TemporaryDirectory(prefix='argon-asr-baseline-') as directory:
        old = Path(directory)
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar: tar.extractall(old,filter='data')
        for name,source in [('baseline',old),('current',ROOT)]:
            subprocess.run([sys.executable,str(Path(__file__).resolve()),'--phase',name,'--source',str(source),
                '--model',str(args.model.resolve()),'--output',str(args.output.resolve())],check=True,timeout=900)
    before = json.loads((args.output/'baseline-result.json').read_text())
    after = json.loads((args.output/'current-result.json').read_text())
    boundary = after.get('boundary',{})
    result = {'baseline':baseline,'candidate':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
        'fixture_sha256':FIXTURE_SHA,'input_sha256':hashlib.sha256((args.output/'input-240.wav').read_bytes()).hexdigest(),
        'model_manifest':json.loads((args.model/'benchmark-manifest.json').read_text()),
        'runtime':{p:version(p) for p in ('faster-whisper','ctranslate2','av','onnxruntime')},
        'source_sha256':{str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in
            [Path(__file__).resolve(),ROOT/'backend/argon_recordings/worker.py',ROOT/'backend/argon_recordings/chunk_audio.py']},
        'baseline_matches_current':before['first_call_sha256']==after['first_call_sha256'],
        'warm_matches_first':after['warm_matches_first'],'boundary':boundary,'before':before,'after':after,
        'limitations':['One 240-second public English excerpt; no human word-reference/WER accuracy claim',
            'Process-first call includes load and inference, not a disk-cache-cold benchmark',
            'Direct real inference/reconciliation, not API retry/cancellation/contention or installed-host qualification']}
    result['passed'] = (result['baseline_matches_current'] and result['warm_matches_first']
        and boundary.get('omitted_context_matches_core_only',False) and boundary.get('saved_json_replay_identical',False))
    dump(args.output/'result.json',result)
    print(json.dumps({k:result[k] for k in ('baseline_matches_current','warm_matches_first','passed')},indent=2))
    if not result['passed']: raise SystemExit(1)


if __name__=='__main__': main()
