"""Predeclared, resumable English clustering calibration with disjoint evaluation."""
import argparse
import hashlib
import importlib.metadata
import json
from pathlib import Path
import resource
import sys
import time
import wave

from score import Turn, read_rttm, score

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'backend'))
from argon_recordings.diarization_runtime import FILES, verify

THRESHOLDS=[.9,1.0,1.1,1.2]
DEV=['aepyx','aggyz']
FRESH=['aorju','auzru','bidnq','bjruf','bmsyn']
REGRESSION=['bgvvt','kgjaa','lbfnx','ofbxh','sebyw']
CONFIG={'window_shift_ratio':.1,'threads':2,'min_duration_on':.3,'min_duration_off':.5,'num_clusters':-1}


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(path,value):
    temp=path.with_suffix('.new')
    temp.write_text(json.dumps(value,indent=2)+'\n'); temp.replace(path)


def references(item,root):
    start,end=item['offset'],item['offset']+item['seconds']
    return [Turn(t.speaker,max(t.start,start)-start,min(t.end,end)-start)
            for t in read_rttm(root/item['directory']/(item['id']+'.rttm')).get(item['id'],[])
            if t.start<end and t.end>start]


def prepare(root):
    def fixture(name,directory):
        audio=root/directory/(name+'.wav'); ref=root/directory/(name+'.rttm')
        with wave.open(str(audio)) as stream:
            assert (stream.getnchannels(),stream.getsampwidth(),stream.getframerate())==(1,2,16000)
            seconds=min(240,stream.getnframes()/16000)
        return {'id':name,'directory':directory,'offset':0,'seconds':seconds,
                'audio_sha256':sha(audio),'reference_sha256':sha(ref)}
    dev=[fixture(name,'wespeaker-dev') for name in DEV]
    fresh=[fixture(name,'wespeaker-heldout-final') for name in FRESH]
    regression=[fixture(name,'heldout') for name in REGRESSION]
    assert not set(DEV)&set(FRESH+REGRESSION) and not set(FRESH)&set(REGRESSION)
    assert all(item['seconds']==240 for item in fresh+regression)
    # Select a real one-speaker control using reference labels before inference.
    control=None
    for item in dev:
        turns=references(item,root)
        for offset in range(int(item['seconds'])-19):
            clipped=[Turn(t.speaker,max(t.start,offset),min(t.end,offset+20))
                     for t in turns if t.start<offset+20 and t.end>offset]
            if len({t.speaker for t in clipped})==1 and sum(t.end-t.start for t in clipped)>=10:
                control={**item,'offset':offset,'seconds':20}; break
        if control: break
    assert control,'No predeclared 20-second single-speaker control found'
    return {'protocol':'eres2net-english-grid-v1','thresholds':THRESHOLDS,'config':CONFIG,
        'runtime':'sherpa-onnx==1.13.8','model_files':FILES,'development':dev,'control':control,
        'reserved_candidate_evaluation':fresh,'known_failure_regression':regression,
        'selection_rule':'Maximize dev exact-count hits, then minimize weighted plan DER, then lowest threshold',
        'exposure_note':'Reserved recordings were previously evaluated with another embedding; no ERes2Net predictions were used for calibration. Original ERes2Net failures are a separate regression set.',
        'gates':{'exact_counts_each_five_clip_set':4,'max_plan_der':.25,'max_rtf':1,
                 'max_peak_rss_kib':2*1024**2,'single_control_speakers':1}}


def aggregate(rows):
    result={}
    for policy in ('plan','zero_collar_overlap_inclusive'):
        components={key:sum(row['metrics'][policy][key] for row in rows)
                    for key in ('miss_seconds','false_alarm_seconds','confusion_seconds','scored_speech_seconds')}
        assert components['scored_speech_seconds']>0
        components['der']=sum(components[key] for key in ('miss_seconds','false_alarm_seconds','confusion_seconds'))/components['scored_speech_seconds']
        result[policy]=components
    return {'clips':rows,'correct_counts':sum(r['reference_speakers']==r['hypothesis_speakers'] for r in rows),
            'aggregate':result,'rtf':sum(r['inference_seconds'] for r in rows)/sum(r['seconds'] for r in rows)}


def recorded_peak(output,rows,current):
    """A scoring-only resume must retain the original native inference peak."""
    return max([current]+[json.loads((output/row['raw_file']).read_text())['peak_rss_kib'] for row in rows])


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--models',type=Path,default=ROOT/'models/diarization-evaluation')
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--prepare-only',action='store_true')
    args=parser.parse_args(); args.output.mkdir(parents=True,exist_ok=True)
    verify(args.models)
    plan=prepare(args.models); plan_id=digest(plan)
    plan_file=args.output/'plan.json'
    if plan_file.exists(): assert json.loads(plan_file.read_text())==plan,'Frozen protocol/input changed; use fresh output'
    else: save(plan_file,plan)
    if args.prepare_only:
        print(json.dumps({'plan_id':plan_id,'thresholds':THRESHOLDS,'control':plan['control'],'development':DEV,'reserved':FRESH,'regression':REGRESSION},indent=2)); return
    assert importlib.metadata.version('sherpa-onnx')=='1.13.8'
    import numpy as np
    import sherpa_onnx
    def evaluate(item,threshold,phase):
        identity={'item':item,'threshold':threshold,'config':plan['config'],'model_files':FILES,'runtime':plan['runtime']}
        key=digest(identity); path=args.output/(phase+'-'+item['id']+'-'+str(threshold)+'.json')
        if path.exists():
            raw=json.loads(path.read_text()); assert raw['identity']==identity,'Case identity changed'
            assert digest(raw['turns'])==raw['turns_sha256']; reused=True
        else:
            with wave.open(str(args.models/item['directory']/(item['id']+'.wav'))) as stream:
                stream.setpos(round(item['offset']*16000)); pcm=stream.readframes(round(item['seconds']*16000))
            assert len(pcm)==round(item['seconds']*16000)*2
            audio=np.frombuffer(pcm,dtype='<i2').astype('float32')/32768
            cfg=plan['config']
            config=sherpa_onnx.OfflineSpeakerDiarizationConfig(
                segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
                    pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(
                        model=str(args.models/'sherpa-onnx-pyannote-segmentation-3-0/model.int8.onnx'),
                        window_shift_ratio=cfg['window_shift_ratio']),num_threads=cfg['threads']),
                embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(args.models/'embedding.onnx'),num_threads=cfg['threads']),
                clustering=sherpa_onnx.FastClusteringConfig(num_clusters=cfg['num_clusters'],threshold=threshold),
                min_duration_on=cfg['min_duration_on'],min_duration_off=cfg['min_duration_off'])
            assert config.validate()
            diarizer=sherpa_onnx.OfflineSpeakerDiarization(config)
            print(f'{phase} {item["id"]} threshold={threshold}: inference',flush=True)
            began=time.monotonic(); turns=diarizer.process(audio).sort_by_start_time(); elapsed=time.monotonic()-began
            values=[{'speaker':str(t.speaker),'start':max(0,t.start),'end':min(item['seconds'],t.end)}
                    for t in turns if min(item['seconds'],t.end)>max(0,t.start)]
            raw={'identity':identity,'identity_sha256':key,'turns':values,'turns_sha256':digest(values),
                 'inference_seconds':elapsed,'peak_rss_kib':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}
            save(path,raw); reused=False
        hyp=[Turn(**t) for t in raw['turns']]; ref=references(item,args.models)
        metrics={policy:score(ref,hyp,duration=item['seconds'],collar=collar,ignore_overlap=ignore)
                 for policy,collar,ignore in [('plan',.25,True),('zero_collar_overlap_inclusive',0,False)]}
        row={'id':item['id'],'offset':item['offset'],'seconds':item['seconds'],'threshold':threshold,
             'reference_speakers':len({t.speaker for t in ref}),'hypothesis_speakers':len({t.speaker for t in hyp}),
             'metrics':metrics,'inference_seconds':raw['inference_seconds'],
             'raw_file':path.name,'raw_sha256':sha(path)}
        print(json.dumps({'phase':phase,'id':row['id'],'threshold':threshold,'reference':row['reference_speakers'],
                          'predicted':row['hypothesis_speakers'],'der':metrics['plan']['der'],'reused':reused}),flush=True)
        return row
    calibration=[]
    for threshold in plan['thresholds']:
        summary=aggregate([evaluate(item,threshold,'development') for item in plan['development']])
        calibration.append({'threshold':threshold,**summary})
        save(args.output/'calibration.json',calibration)
    chosen=min(calibration,key=lambda row:(-row['correct_counts'],row['aggregate']['plan']['der'],row['threshold']))['threshold']
    frozen={'plan_id':plan_id,'threshold':chosen,'calibration_sha256':sha(args.output/'calibration.json')}
    selected=args.output/'frozen-candidate.json'
    if selected.exists():
        assert json.loads(selected.read_text())==frozen,'Frozen selection or calibration changed'
    else: save(selected,frozen)
    print('Frozen candidate: '+str(chosen),flush=True)
    control=evaluate(plan['control'],chosen,'single-control')
    fresh=aggregate([evaluate(item,chosen,'reserved') for item in plan['reserved_candidate_evaluation']])
    save(args.output/'reserved-result.json',fresh)
    regression=aggregate([evaluate(item,chosen,'regression') for item in plan['known_failure_regression']])
    save(args.output/'regression-result.json',regression)
    gates=plan['gates']
    measured=[row for candidate in calibration for row in candidate['clips']]+[control]+fresh['clips']+regression['clips']
    peak=recorded_peak(args.output,measured,resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    accepted=(control['hypothesis_speakers']==1 and peak<=gates['max_peak_rss_kib'] and
        all(row['correct_counts']>=gates['exact_counts_each_five_clip_set'] and
            row['aggregate']['plan']['der']<=gates['max_plan_der'] and row['rtf']<=gates['max_rtf'] for row in (fresh,regression)))
    result={'plan_id':plan_id,'threshold':chosen,'control':control,'reserved':fresh,'regression':regression,
        'peak_rss_kib':peak,'gates_passed':accepted,'source_sha256':{str(p.relative_to(ROOT)):sha(p)
            for p in (Path(__file__).resolve(),Path(__file__).with_name('score.py'))},
        'limitations':['English public fixtures only; no Spanish/private/installed acceptance',
            'Reserved IDs previously used for another embedding; candidate-specific disjoint evaluation',
            'No app default or worker model identity changed by this experiment']}
    save(args.output/'result.json',result)
    print(json.dumps({'threshold':chosen,'control_speakers':control['hypothesis_speakers'],
        'reserved_correct':fresh['correct_counts'],'regression_correct':regression['correct_counts'],
        'gates_passed':accepted},indent=2))


if __name__=='__main__': main()
