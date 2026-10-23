"""Actual API -> isolated worker subprocess -> accepted source-derived media."""
import hashlib
import shutil
import subprocess

import pytest

from test_workflow import client
from test_visual_jobs import setup
from argon_recordings.visual_worker import run

pytestmark = pytest.mark.skipif(not shutil.which('ffmpeg') or not shutil.which('ffprobe'),reason='FFmpeg integration environment required')


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
