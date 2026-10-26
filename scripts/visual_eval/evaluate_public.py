"""Frozen local screencast evaluation; OCR is an evaluation tool, never an app dependency."""
import argparse
import hashlib
import json
from pathlib import Path
import resource
import shutil
import subprocess
import time

from argon_recordings.visual_analysis import analyze
from argon_recordings.visual_runtime import extract, probe_source


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def normalized(text):
    return ' '.join(''.join(c.lower() if c.isalnum() else ' ' for c in text).split())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--reference', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    reference = json.loads(args.reference.read_text())
    assert args.source.stat().st_size == reference['source_bytes']
    assert sha(args.source) == reference['source_sha256']
    assert shutil.which('tesseract'), 'Install the separate local OCR evaluator first'
    output = args.output
    output.mkdir(parents=True, exist_ok=True)
    assert not (output/'result.json').exists(), 'Choose a fresh output directory'
    (output/'ATTRIBUTION.txt').write_text(
        f"{reference['author']}: Wikimedia Screencast (2014)\n{reference['page_url']}\n"
        f"{reference['license']} {reference['license_url']}\n"
        'Derived frames/clips: resizing, sampling, muted clips and JPEG/H.264 encoding. Same license.\n')
    info = probe_source(args.source)
    started = time.monotonic()
    analysis = analyze(args.source)
    analysis_seconds = time.monotonic()-started
    (output/'analysis.json').write_text(json.dumps(analysis, indent=2)+'\n')
    manifests = []
    for index, proposal in enumerate(analysis['proposals']):
        if time.monotonic()-started > 240:
            break
        recipe = ({'kind':'image','start':proposal['timestamp'],'source_resolution':True}
                  if proposal['type'] == 'image' else
                  {'kind':'clip','start':proposal['source_start'],'end':proposal['source_end'],'fps':15})
        directory = output/f'adaptive-{index:02d}'
        begin = time.monotonic()
        try:
            manifest = extract(args.source, directory, recipe)
            manifests.append({'index':index,'recipe':recipe,'manifest':manifest,'seconds':time.monotonic()-begin})
        except ValueError as error:
            manifests.append({'index':index,'recipe':recipe,'error':str(error),'seconds':time.monotonic()-begin})
    baseline = []
    for timestamp in range(0, int(info.duration)+1, 60):
        begin = time.monotonic()
        manifest = extract(args.source, output/f'sparse-{timestamp}',
                           {'kind':'image','start':timestamp,'source_resolution':True})
        baseline.append({'timestamp':timestamp,'manifest':manifest,'seconds':time.monotonic()-begin})
    readability = []
    for sample in reference['readability']:
        for width in (480, 960):
            directory = output/f'readability-{sample["time"]}-{width}'
            manifest = extract(args.source, directory, {'kind':'image','start':sample['time'],'max_width':width})
            ocr = subprocess.run(['tesseract',str(directory/manifest['filename']),'stdout','--psm','11','-l','eng'],
                                 check=True, timeout=30, capture_output=True, text=True).stdout
            (directory/'ocr.txt').write_text(ocr)
            matches = {phrase:normalized(phrase) in normalized(ocr) for phrase in sample['phrases']}
            readability.append({'time':sample['time'],'width':width,'matches':matches,'manifest':manifest})
    # One frame/clip may cover several checkpoints. This is temporal proximity,
    # not a one-to-one event-recall or semantic-importance measurement.
    def checkpoint_matches(checkpoint, proposals):
        timestamp = checkpoint['time']; tolerance = reference['checkpoint_tolerance_seconds']
        return [index for index, item in enumerate(proposals)
                if item['start']-tolerance <= timestamp <= item['end']+tolerance]
    intervals = [{'start':p['source_start'],'end':p['source_end']} for p in analysis['proposals']]
    sparse = [{'start':p['timestamp'],'end':p['timestamp']} for p in baseline]
    checkpoints = [{**point,'adaptive':checkpoint_matches(point,intervals),'sparse':checkpoint_matches(point,sparse)}
                   for point in reference['checkpoints']]
    result = {
        'fixture':reference['fixture'],'source_sha256':sha(args.source),'reference_sha256':sha(args.reference),
        'source':{'duration':info.duration,'width':info.display_width,'height':info.display_height,
                  'fps':info.avg_frame_rate,'bytes':info.size_bytes},
        'source_code_sha256':{str(path):sha(path) for path in [Path(__file__),
              Path(__import__('argon_recordings.visual_runtime',fromlist=['x']).__file__),
              Path(__import__('argon_recordings.visual_analysis',fromlist=['x']).__file__)]},
        'analysis_status':analysis['status'],'analysis_seconds':analysis_seconds,
        'analysis_budgets':analysis['budgets'],'proposal_count':len(analysis['proposals']),
        'rendered_count':sum('manifest' in item for item in manifests),'render_attempts':manifests,
        'rendering_complete':len(manifests)==len(analysis['proposals']) and all('manifest' in item for item in manifests),
        'adaptive_bytes':sum(item.get('manifest',{}).get('bytes',0) for item in manifests),
        'sparse_bytes':sum(item['manifest']['bytes'] for item in baseline),'sparse':baseline,
        'temporal_checkpoints':checkpoints,
        'temporal_coverage':{kind:sum(bool(p[kind]) for p in checkpoints) for kind in ('adaptive','sparse')},
        'readability':readability,'ocr_version':subprocess.check_output(['tesseract','--version'],text=True).splitlines()[0],
        'elapsed_seconds':time.monotonic()-started,'children_peak_rss_kib':resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss,
        'limitations':['One real desktop screencast at 960x720, not representative code/slide/private acceptance',
                        'Agent-labelled coarse checkpoints, not human-adjudicated event recall',
                        'OCR phrase recovery is a readability proxy, not human reading accuracy',
                        'Unequal evidence coverage: byte totals are measured costs, not a storage-savings claim'],
    }
    (output/'result.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps({key:result[key] for key in ('analysis_status','proposal_count','rendered_count',
          'rendering_complete','adaptive_bytes','sparse_bytes','temporal_coverage','analysis_seconds','elapsed_seconds')},indent=2))


if __name__ == '__main__':
    main()
