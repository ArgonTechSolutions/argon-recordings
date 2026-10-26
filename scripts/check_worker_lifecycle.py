"""Bounded real ASR interruption/restart and single-worker media contention fixture."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time

TOKEN = 'synthetic-lifecycle-worker-only'
AUTH = {'Authorization':'Bearer '+TOKEN}
ROOT = Path(__file__).resolve().parents[1]
INPUT_SHA = 'b7be79fd63934cccea366293d772d5c9f3cbce68bec846e871ad4648763c5181'


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def event(output, name, **details):
    value = {'time':time.time(),'event':name,**details}
    with output.open('a') as stream:
        stream.write(json.dumps(value)+'\n')


def worker(args):
    import httpx
    from argon_recordings.worker import Turbo, run_once
    class ObservedTurbo(Turbo):
        def transcribe(self, audio, language, progress, hint=None):
            def observe(seconds):
                event(args.events,'inference_progress',seconds=seconds)
                progress(seconds)
            return super().transcribe(audio,language,observe,hint)
    model = ObservedTurbo(args.model)
    def response(response):
        if response.request.url.path == '/api/worker/tasks/claim':
            response.read()
            job = response.json()
            if job:
                event(args.events,'claim',kind=job.get('kind','prepare'),id=job.get('id',job.get('job_id')),
                      lease_id=job['lease_id'])
    with httpx.Client(base_url=args.url,headers=AUTH,trust_env=False,timeout=60,
                      event_hooks={'response':[response]}) as client:
        client.post('/api/worker/ready',json={'diarization_model':None}).raise_for_status()
        completed = 0
        while completed < args.calls:
            if run_once(client,model,args.work):
                completed += 1
                resident = int(Path('/proc/self/statm').read_text().split()[1])*os.sysconf('SC_PAGE_SIZE')
                event(args.events,'work_complete',number=completed,model_loaded=model.model is not None,resident_bytes=resident)
                print(f'Worker completed {completed}/{args.calls}',flush=True)
            else:
                time.sleep(1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--audio',type=Path)
    parser.add_argument('--video',type=Path)
    parser.add_argument('--model',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--role',choices=['api','worker'])
    parser.add_argument('--data',type=Path)
    parser.add_argument('--work',type=Path)
    parser.add_argument('--url')
    parser.add_argument('--port',type=int)
    parser.add_argument('--calls',type=int)
    parser.add_argument('--events',type=Path)
    args = parser.parse_args()
    if args.role == 'api':
        import uvicorn
        from argon_recordings.api import Settings, create_app
        app = create_app(Settings(args.data,worker_tokens={'one':TOKEN},worker_limit=1,reserve=0))
        uvicorn.run(app,host='127.0.0.1',port=args.port,access_log=False,log_level='warning',timeout_graceful_shutdown=5)
        return
    if args.role == 'worker':
        worker(args); return
    import httpx
    from argon_recordings.visual_runtime import probe_source, extract
    assert sha(args.audio)==INPUT_SHA
    assert sha(args.video)=='0cabfba7f372013acc1050fb98c4454d89cd88dc88214f092baae54f299270f3'
    args.output.mkdir(parents=True,exist_ok=False)
    with socket.socket() as listener:
        listener.bind(('127.0.0.1',0)); port = listener.getsockname()[1]
    base = f'http://127.0.0.1:{port}'
    processes, logs = [], []
    result = {'checks':{},'configuration':{'worker_limit':1,'device':'cpu','compute_type':'int8','cpu_threads':2}}
    def spawn(role, **values):
        log = (args.output/f'{role}-{len(processes)}.log').open('w'); logs.append(log)
        command = [sys.executable,str(Path(__file__).resolve()),'--role',role,
                   '--model',str(args.model),'--output',str(args.output)]
        for name,value in values.items(): command.extend(['--'+name,str(value)])
        process = subprocess.Popen(command,stdout=log,stderr=subprocess.STDOUT)
        processes.append(process); return process
    def stop(process, hard=False):
        if process.poll() is None:
            process.kill() if hard else process.terminate()
            try: process.wait(timeout=10)
            except subprocess.TimeoutExpired: process.kill(); process.wait(timeout=5)
    def events(path):
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
    def wait_for(predicate, seconds, label):
        until = time.monotonic()+seconds
        while time.monotonic()<until:
            value = predicate()
            if value: return value
            time.sleep(.5)
        raise AssertionError('Timed out: '+label)
    with tempfile.TemporaryDirectory(prefix='argon-lifecycle-') as directory:
        temporary = Path(directory); data = temporary/'data'; work = temporary/'work'; work.mkdir()
        def rows(sql, values=()):
            with sqlite3.connect(data/'recordings.sqlite') as db:
                db.row_factory = sqlite3.Row
                return [dict(r) for r in db.execute(sql,values)]
        def ready(client):
            try: return client.get('/api/health').status_code==200
            except httpx.TransportError: return False
        try:
            server = spawn('api',data=data,port=port)
            with httpx.Client(base_url=base,trust_env=False,timeout=15) as client:
                wait_for(lambda:ready(client),20,'initial API start')
                def request(method,path,**kwargs):
                    response=client.request(method,path,**kwargs)
                    assert response.is_success,(path,response.status_code,response.text)
                    return response
                pid = request('POST','/api/projects',json={'name':'Disposable lifecycle'}).json()['id']
                def upload(path,title):
                    rid=request('POST',f'/api/projects/{pid}/recordings',json={
                        'title':title,'filename':path.name,'language':'en'}).json()['id']
                    request('PUT',f'/api/recordings/{rid}/source',content=path.read_bytes())
                    return rid
                visual = upload(args.video,'Visual-only prepared fixture; no ASR claim')
                # Fixture setup only. ASR below uses real preparation/inference/publication.
                frame=extract(args.video,temporary/'visual-reference',{'kind':'image','start':0})
                with sqlite3.connect(data/'recordings.sqlite') as db:
                    db.execute("UPDATE recordings SET status='ready',duration=? WHERE id=?",(probe_source(args.video).duration,visual))
                    db.execute("UPDATE jobs SET state='completed',progress=1 WHERE recording_id=?",(visual,))
                    db.execute('INSERT INTO artifacts VALUES(?,?,?,?,?)',
                        (visual,'frame-0000.jpg',frame['actual_start'],'frame',str(temporary/'visual-reference'/frame['filename'])))
                rid = upload(args.audio,'Real ASR interruption fixture')
                prep_events=args.output/'prepare.jsonl'
                preparation=spawn('worker',url=base,work=work,calls=1,events=prep_events)
                assert preparation.wait(timeout=90)==0,'Preparation failed'
                evidence=request('GET',f'/api/recordings/{visual}/evidence').json()
                tasks=[]
                for timestamp in (0,20):
                    tasks.append(request('POST',f'/api/recordings/{visual}/visual-jobs',json={
                        'reference':{'recording_id':visual,'source_sha256':evidence['source_sha256'],
                            'interval':{'start':0,'end':87},'versions':evidence['versions']},
                        'recipe':{'kind':'image','start':timestamp,'end':timestamp}}).json())
                first_events=args.output/'interrupted.jsonl'
                first=spawn('worker',url=base,work=work,calls=1,events=first_events)
                wait_for(lambda:any(e['event']=='inference_progress' and e['seconds']>1 for e in events(first_events)),240,'real inference progress')
                old=next(e for e in events(first_events) if e['event']=='claim')
                assert old['kind']=='chunk'
                assert request('POST','/api/worker/tasks/claim',headers=AUTH).json() is None
                assert request('POST','/api/worker/visual/claim',headers=AUTH).json() is None
                cancelled=request('POST',f'/api/recordings/{visual}/visual-jobs/{tasks[0]["id"]}/cancel').json()
                assert cancelled['state']=='cancelled'
                result['checks']['busy_worker_denies_second_asr_and_visual_claims']=True
                result['checks']['queued_visual_cancellation_during_real_asr']=True
                print('Actual inference observed; single-worker contention and queued cancellation passed',flush=True)
                begin=time.monotonic(); stop(first,hard=True)
                result['interruption_seconds']=time.monotonic()-begin
                assert first.returncode==-9 and not Path(f'/proc/{first.pid}').exists()
                assert request('GET',f'/api/recordings/{rid}/transcript').json()['segments']==[]
                assert client.get(f'/api/recordings/{rid}/media/audio.wav').status_code==404
                assert request('POST','/api/worker/tasks/claim',headers=AUTH).json() is None
                result['checks']['native_process_stopped_no_partial_publication']=True
                print('Native worker interrupted without partial publication; restarting API and waiting for natural lease expiry',flush=True)
                stop(server)
                server=spawn('api',data=data,port=port)
                wait_for(lambda:ready(client),20,'API restart')
                assert request('GET',f'/api/recordings/{rid}/transcript').json()['segments']==[]
                second_events=args.output/'restarted.jsonl'
                begin=time.monotonic()
                second=spawn('worker',url=base,work=work,calls=4,events=second_events)
                claimed=wait_for(lambda:next((e for e in events(second_events) if e['event']=='claim'),None),165,'natural lease recovery')
                result['restart_to_reclaim_seconds']=time.monotonic()-begin
                assert claimed['id']==old['id'] and claimed['lease_id']!=old['lease_id']
                stale={**AUTH,'X-Lease-Id':old['lease_id']}
                for suffix,method,body in [('heartbeat','POST',{'progress':.5,'stage':'stale'}),
                                           ('complete','POST',{'segments':[]}),('audio','GET',None)]:
                    response=client.request(method,f'/api/worker/chunks/{old["id"]}/{suffix}',headers=stale,
                                            **({'json':body} if body is not None else {}))
                    assert response.status_code==409,(suffix,response.text)
                result['checks']['natural_expiry_same_chunk_new_lease_stale_operations_denied']=True
                print('Natural lease expiry and API restart recovered the same chunk; stale lease fenced',flush=True)
                assert second.wait(timeout=600)==0,'Restarted worker failed'
                transcript=request('GET',f'/api/recordings/{rid}/transcript').json()
                expected=json.loads((ROOT/'models/validation/asr-real/run-01/reconciled.json').read_text())
                actual=[{k:s[k] for k in ('start','end','text','words')} for s in transcript['segments']]
                assert actual==expected,'API publication differs from frozen direct real-inference result'
                assert request('GET',f'/api/recordings/{rid}').json()['status']=='ready'
                assets=request('GET',f'/api/recordings/{visual}/visual-evidence').json()
                assert len(assets)==1
                media=request('GET',assets[0]['url']).content
                assert hashlib.sha256(media).hexdigest()==assets[0]['manifest']['sha256']
                states=rows('SELECT id,state FROM visual_jobs WHERE recording_id=?',(visual,))
                assert {s['id']:s['state'] for s in states}=={tasks[0]['id']:'cancelled',tasks[1]['id']:'completed'}
                completed=[e for e in events(second_events) if e['event']=='work_complete']
                assert len(completed)==4 and completed[-2]['model_loaded'] and not completed[-1]['model_loaded']
                result['checks']['real_asr_publication_equals_direct_reference']=True
                result['checks']['only_uncancelled_visual_published_with_valid_checksum']=True
                result['checks']['turbo_released_for_actual_ffmpeg_task']=True
                result['resident_before_visual_bytes']=completed[-2]['resident_bytes']
                result['resident_after_visual_bytes']=completed[-1]['resident_bytes']
                result['published_words']=sum(len(s['words']) for s in actual)
                result['chunk_attempts']=rows('SELECT ordinal,attempts,state FROM chunks ORDER BY ordinal')
                result['input_sha256']=sha(args.audio)
                result['direct_reference_sha256']=sha(ROOT/'models/validation/asr-real/run-01/reconciled.json')
                result['model_manifest']=json.loads((args.model/'benchmark-manifest.json').read_text())
                result['source_sha256']={str(p.relative_to(ROOT)):sha(p) for p in
                    [Path(__file__).resolve(),*sorted((ROOT/'backend/argon_recordings').glob('*.py'))]}
                result['candidate']=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
                result['limitations']=['Temporary loopback API and worker processes in one validation container',
                    'run_once entrypoint, not installed isolated.worker loop or production credential rotation',
                    'Visual ready state is a seeded fixture; ASR preparation/inference/publication are real',
                    'SIGKILL interruption is not a public ASR cancel endpoint or graceful cancellation claim']
                result['passed']=all(result['checks'].values())
        finally:
            for process in reversed(processes): stop(process)
            for log in logs: log.close()
            result['all_owned_processes_terminal']=all(p.poll() is not None for p in processes)
            (args.output/'result.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result['checks'],indent=2))


if __name__=='__main__': main()
