"""Actual API -> isolated worker subprocess -> accepted source-derived media."""
import hashlib
import shutil
import subprocess

import pytest

from test_workflow import client
from test_visual_jobs import setup
from argon_recordings.visual_worker import run

pytestmark = pytest.mark.skipif(not shutil.which('ffmpeg') or not shutil.which('ffprobe'),reason='FFmpeg integration environment required')


@pytest.fixture(autouse=True)
def source_network_guard(tmp_path,monkeypatch):
    from pathlib import Path
    library=tmp_path/'visual_netguard.so'
    subprocess.run(['cc','-shared','-fPIC','-O2',str(Path(__file__).resolve().parents[2]/'scripts/visual_netguard.c'),'-ldl','-o',str(library)],check=True)
    monkeypatch.setenv('RECORDINGS_VISUAL_NET_GUARD',str(library))


def test_actual_visual_worker_publication_and_clip_range(client,tmp_path):
    rid,body=setup(client)
    source=client.app.state.store.root/'sources'/rid
    video=tmp_path/'fixture.mp4'
    subprocess.run(['ffmpeg','-nostdin','-v','error','-f','lavfi','-i',
        'testsrc2=size=320x240:rate=25:duration=4','-c:v','libx264','-threads','2','-pix_fmt','yuv420p',str(video)],
        check=True,timeout=30,capture_output=True)
    data=video.read_bytes(); source.write_bytes(data)
    with client.app.state.store.db() as db:
        db.execute('UPDATE recordings SET sha256=?,bytes=?,duration=4 WHERE id=?',(hashlib.sha256(data).hexdigest(),len(data),rid))
    state=client.get(f'/api/recordings/{rid}/evidence').json()
    body['reference'].update(source_sha256=state['source_sha256'],interval={'start':0,'end':4},versions=state['versions'])
    before=client.get(f'/api/recordings/{rid}/transcript').json()
    client.headers['Authorization']='Bearer synthetic-test-only'
    transcriber=type('Transcriber',(),{'model':object()})()
    for recipe in ({'kind':'image','start':.36,'end':.36},
                   {'kind':'clip','start':.36,'end':2,'fps':15}):
        body['recipe']=recipe
        response=client.post(f'/api/recordings/{rid}/visual-jobs',json=body)
        assert response.status_code==200,response.text
        assert run(client,transcriber,tmp_path)
        assert transcriber.model is None
        jobs=client.get(f'/api/recordings/{rid}/visual-jobs').json()
        assert jobs[0]['state']=='completed',jobs
    assets=client.get(f'/api/recordings/{rid}/visual-evidence').json()
    assert len(assets)==2
    clip=next(a for a in assets if a['manifest']['kind']=='clip')
    assert clip['manifest']['source_fps']==25 and 0<clip['manifest']['fps']<=15
    assert clip['manifest']['output_frame_count']>1
    response=client.get(clip['url'],headers={'Range':'bytes=0-11'})
    assert response.status_code==206 and response.content[4:8]==b'ftyp'
    assert response.headers['content-type']=='video/mp4'
    assert client.get(f'/api/recordings/{rid}/transcript').json()==before
    assert not run(client,transcriber,tmp_path)


def test_large_sparse_recording_screenshot_transfers_only_seek_ranges(client,tmp_path):
    """A multi-GB retained MP4 must not be copied for a single late screenshot."""
    import struct,time
    rid,body=setup(client);source=client.app.state.store.root/'sources'/rid
    video=tmp_path/'short.mp4'
    subprocess.run(['ffmpeg','-nostdin','-v','error','-f','lavfi','-i','testsrc2=size=160x90:rate=2:duration=60','-c:v','libx264','-threads','1','-g','4','-pix_fmt','yuv420p',str(video)],check=True,timeout=30)
    data=video.read_bytes();offset=0
    while data[offset+4:offset+8]!=b'moov':offset+=struct.unpack('>I',data[offset:offset+4])[0]
    padding=2760000000
    with source.open('wb') as stream:
        stream.write(data[:offset]);stream.write(struct.pack('>I4s',padding,b'free'))
        stream.seek(padding-8,1);stream.write(data[offset:])
    digest=hashlib.sha256()
    with source.open('rb') as stream:
        for block in iter(lambda:stream.read(1024**2),b''):digest.update(block)
    size=source.stat().st_size
    with client.app.state.store.db() as db:db.execute('UPDATE recordings SET sha256=?,bytes=?,duration=60 WHERE id=?',(digest.hexdigest(),size,rid))
    state=client.get(f'/api/recordings/{rid}/evidence').json()
    body['reference'].update(source_sha256=state['source_sha256'],interval={'start':0,'end':60},versions=state['versions'])
    body['recipe']={'kind':'image','start':55,'end':55}
    assert client.post(f'/api/recordings/{rid}/visual-jobs',json=body).status_code==200
    client.headers['Authorization']='Bearer synthetic-test-only'
    original=client.stream;ranges=[]
    def measured(method,url,**kwargs):
        if str(url).endswith('/source-verified'):ranges.append(kwargs.get('headers',{}).get('Range'))
        assert not str(url).endswith('/source')
        return original(method,url,**kwargs)
    client.stream=measured
    started=time.monotonic()
    assert run(client,type('Transcriber',(),{'model':None})(),tmp_path)
    elapsed=time.monotonic()-started
    transferred=sum(int(value.split('-')[1])-int(value.split('=')[1].split('-')[0])+1 for value in ranges)
    assert transferred<16*1024**2,(size,transferred,ranges)
    assert elapsed<60,elapsed
    asset=client.get(f'/api/recordings/{rid}/visual-evidence').json()[0]
    assert asset['manifest']['kind']=='image' and 54.9<=asset['manifest']['actual_start']<=55.6
    print({'retained_source_bytes':size,'transferred_range_bytes':transferred,'elapsed_seconds':round(elapsed,2)})
