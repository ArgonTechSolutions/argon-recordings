"""Bounded repeated real Turbo reload/visual-extraction memory measurements."""
import argparse
import hashlib
from importlib.metadata import version
import json
import os
from pathlib import Path
import resource
import subprocess
import tempfile
import time

from fastapi.testclient import TestClient

from argon_recordings.api import Settings, create_app
from argon_recordings.worker import Turbo, crop_audio
from argon_recordings.visual_worker import run as run_visual
from argon_recordings.visual_runtime import extract, probe_source

ROOT = Path(__file__).resolve().parents[1]
SIZES = [30,60,30,60,30,60]


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def memory():
    result = {'resident_bytes':int(Path('/proc/self/statm').read_text().split()[1])*os.sysconf('SC_PAGE_SIZE'),
              'process_peak_rss_kib':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}
    status = Path('/proc/self/status').read_text().splitlines()
    result['status_kib'] = {name: int(next(line.split()[1] for line in status if line.startswith(name+':')))
                            for name in ('VmSize','VmRSS','RssAnon','RssFile','VmSwap')}
    for name in ('memory.current','memory.peak','memory.swap.current'):
        path = Path('/sys/fs/cgroup')/name
        result[name] = int(path.read_text()) if path.exists() else None
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--audio',type=Path,required=True)
    parser.add_argument('--video',type=Path,required=True)
    parser.add_argument('--model',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    assert sha(args.audio)=='b7be79fd63934cccea366293d772d5c9f3cbce68bec846e871ad4648763c5181'
    assert sha(args.video)=='0cabfba7f372013acc1050fb98c4454d89cd88dc88214f092baae54f299270f3'
    args.output.mkdir(parents=True,exist_ok=False)
    result={'sizes_seconds':SIZES,'cycles':[],'initial_memory':memory()}
    def save(): (args.output/'result.json').write_text(json.dumps(result,indent=2)+'\n')
    save()
    outputs={}
    with tempfile.TemporaryDirectory(prefix='argon-memory-') as directory:
        temporary=Path(directory); work=temporary/'work'; work.mkdir()
        for seconds in sorted(set(SIZES)):
            crop_audio(args.audio,temporary/f'{seconds}.wav',0,seconds)
        app=create_app(Settings(temporary/'data',worker_tokens={'one':'synthetic-memory-only'},worker_limit=1,reserve=0))
        with TestClient(app) as client:
            client.headers['Authorization']='Bearer synthetic-memory-only'
            def request(method,path,**kwargs):
                response=client.request(method,path,**kwargs)
                assert response.is_success,(path,response.status_code,response.text)
                return response.json()
            pid=request('POST','/api/projects',json={'name':'Disposable memory cycles'})['id']
            rid=request('POST',f'/api/projects/{pid}/recordings',json={
                'title':'Visual-only prepared fixture','filename':'screen.webm'})['id']
            request('PUT',f'/api/recordings/{rid}/source',content=args.video.read_bytes())
            frame=extract(args.video,temporary/'reference',{'kind':'image','start':0})
            with app.state.store.db() as db:
                db.execute("UPDATE recordings SET status='ready',duration=? WHERE id=?",(probe_source(args.video).duration,rid))
                db.execute("UPDATE jobs SET state='completed',progress=1 WHERE recording_id=?",(rid,))
                db.execute('INSERT INTO artifacts VALUES(?,?,?,?,?)',
                    (rid,'frame-0000.jpg',frame['actual_start'],'frame',str(temporary/'reference'/frame['filename'])))
            transcriber=Turbo(args.model)
            result['initialized_memory']=memory(); save()
            for index,seconds in enumerate(SIZES):
                print(f'Cycle {index+1}/{len(SIZES)}: real {seconds}s ASR followed by video',flush=True)
                began=time.monotonic()
                segments=[s.model_dump() for s in transcriber.transcribe(temporary/f'{seconds}.wav','en',lambda _:None)]
                assert segments and sum(len(s['words']) for s in segments)>0
                canonical=hashlib.sha256(json.dumps(segments,sort_keys=True,separators=(',',':')).encode()).hexdigest()
                if seconds in outputs: assert canonical==outputs[seconds],'Reload changed same-input result'
                outputs[seconds]=canonical
                (args.output/f'cycle-{index+1}-transcript.json').write_text(json.dumps(segments,indent=2)+'\n')
                cycle={'index':index+1,'input_seconds':seconds,'inference_seconds':time.monotonic()-began,
                       'output_sha256':canonical,'words':sum(len(s['words']) for s in segments),'after_asr':memory()}
                state=request('GET',f'/api/recordings/{rid}/evidence')
                task=request('POST',f'/api/recordings/{rid}/visual-jobs',json={
                    'reference':{'recording_id':rid,'source_sha256':state['source_sha256'],
                        'interval':{'start':0,'end':87},'versions':state['versions']},
                    'recipe':{'kind':'image','start':index,'end':index}})
                assert run_visual(client,transcriber,work)
                assert transcriber.model is None
                completed=request('GET',f'/api/recordings/{rid}/visual-jobs')
                assert next(item for item in completed if item['id']==task['id'])['state']=='completed'
                cycle['after_visual']=memory()
                result['cycles'].append(cycle); save()
            assert len(request('GET',f'/api/recordings/{rid}/visual-evidence'))==len(SIZES)
    result.update(completed=True,same_input_outputs_identical=True,temporary_data_removed=True,
        candidate=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
        model_manifest=json.loads((args.model/'benchmark-manifest.json').read_text()),
        runtime={name:version(name) for name in ('faster-whisper','ctranslate2','av','onnxruntime')},
        source_sha256={str(path.relative_to(ROOT)):sha(path) for path in
            [Path(__file__).resolve(),ROOT/'backend/argon_recordings/worker.py',ROOT/'backend/argon_recordings/visual_worker.py',
             ROOT/'backend/argon_recordings/visual_runtime.py']},
        limitations=['Six cycles at two short input sizes, not an unbounded leak/plateau proof',
            'Direct real ASR plus actual visual-worker API calls; scheduling/restart qualified separately',
            'TestClient API shares this process; cgroup peak includes subprocesses and file cache',
            'No WER, private/Spanish, installed-host or deployment acceptance'])
    result['post_visual_memory_range_bytes']=[min(c['after_visual']['resident_bytes'] for c in result['cycles']),
                                             max(c['after_visual']['resident_bytes'] for c in result['cycles'])]
    result['post_visual_first_to_last_bytes']=result['cycles'][-1]['after_visual']['resident_bytes']-result['cycles'][0]['after_visual']['resident_bytes']
    save()
    print(json.dumps({k:result[k] for k in ('completed','same_input_outputs_identical',
          'post_visual_memory_range_bytes','post_visual_first_to_last_bytes')},indent=2))


if __name__=='__main__': main()
