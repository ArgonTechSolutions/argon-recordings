"""Published SDK -> stdio adapter -> real disposable account API and SQLite."""
import asyncio
import base64
import hashlib
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time

import httpx
from mcp import Client
from mcp.client.stdio import StdioServerParameters
import uvicorn

from argon_recordings.api import create_app, Settings


def test_stdio_real_backend_grants_and_images(tmp_path):
    app=create_app(Settings(tmp_path/'data',auth_mode='accounts',reserve=0,worker_token='synthetic-sdk-worker'))
    listener=socket.socket();listener.bind(('127.0.0.1',0));port=listener.getsockname()[1]
    server=uvicorn.Server(uvicorn.Config(app,access_log=False,log_level='error'))
    thread=threading.Thread(target=server.run,kwargs={'sockets':[listener]},daemon=True);thread.start()
    try:
        deadline=time.monotonic()+15
        while not server.started:
            assert thread.is_alive() and time.monotonic()<deadline
            time.sleep(.02)
        manager=app.state.accounts
        manager.bootstrap('sdk-fixture@example.com','SDK fixture','synthetic SDK password only')
        origin=f'http://127.0.0.1:{port}'
        with httpx.Client(base_url=origin,trust_env=False) as browser:
            session=browser.get('/api/auth/session').json()
            assert browser.post('/api/auth/login',headers={'X-CSRF-Token':session['csrf']},json={
                'email':'sdk-fixture@example.com','password':'synthetic SDK password only'}).status_code==200
            headers={'X-CSRF-Token':browser.get('/api/auth/session').json()['csrf']}
            pid=browser.post('/api/projects',headers=headers,json={'name':'SDK integration'}).json()['id']
            rid=browser.post(f'/api/projects/{pid}/recordings',headers=headers,json={'title':'SDK evidence','filename':'fixture.mp4'}).json()['id']
            image=tmp_path/'image.jpg'
            subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','color=c=blue:s=64x64',
                '-frames:v','1','-threads','1',str(image)],check=True,timeout=15)
            source=manager.store.root/'sources'/rid
            subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','color=c=blue:s=64x64:r=5:d=10',
                '-c:v','libx264','-threads','1','-f','mp4',str(source)],check=True,timeout=15)
            with manager.store.db() as db:
                db.execute("UPDATE recordings SET sha256=?,bytes=?,duration=10,status='ready' WHERE id=?",(hashlib.sha256(source.read_bytes()).hexdigest(),source.stat().st_size,rid))
                for i in range(3):
                    db.execute('INSERT INTO segments VALUES(?,?,?,?,?,?)',(f'sdk-{i}',rid,i*2,i*2+2,f'Synthetic source passage {i}','[]'))
                db.execute('INSERT INTO artifacts VALUES(?,?,?,?,?)',(rid,'frame-0000.jpg',0,'frame',str(image)))
            issued=browser.post('/api/auth/agent-grants',headers=headers,json={'label':'Disposable SDK','project_ids':[pid],'derive_media':True}).json()
            asyncio.run(exercise(origin,issued['token'],pid,rid,image,browser,headers,issued['grant']['id']))
    finally:
        server.should_exit=True;thread.join(timeout=10);listener.close()
        assert not thread.is_alive()


async def exercise(origin,token,pid,rid,image,browser,headers,grant_id):
    env={'PATH':os.environ.get('PATH',''),'ARGON_RECORDINGS_API_ORIGIN':origin,
         'ARGON_AGENT_TOKEN':token,'ARGON_AGENT_ALLOW_ENV_TOKEN':'1'}
    params=StdioServerParameters(command=sys.executable,args=[str(Path(__file__).with_name('server.py'))],env=env)
    async with Client(params) as client:
        projects=await client.call_tool('list_projects',{})
        assert projects.structured_content['items'][0]['id']==pid
        first=await client.call_tool('get_transcript',{'recording_id':rid,'limit':2})
        assert len(first.structured_content['items'])==2
        second=await client.call_tool('get_transcript',{'recording_id':rid,'cursor':first.structured_content['next_cursor']})
        assert [s['id'] for s in second.structured_content['items']]==['sdk-2']
        result=await client.call_tool('get_image',{'recording_id':rid,'evidence_id':'frame:frame-0000.jpg'})
        block=next(b for b in result.content if b.type=='image')
        assert base64.b64decode(block.data)==image.read_bytes()
        # Decode returned content, rather than merely checking a JPEG prefix.
        decoded=subprocess.run(['ffmpeg','-v','error','-i','pipe:0','-f','null','-'],input=base64.b64decode(block.data),capture_output=True,timeout=15)
        assert decoded.returncode==0
        for name in ('get_speakers','list_marks','list_visual_evidence'):
            assert not (await client.call_tool(name,{'recording_id':rid})).is_error
        job=await client.call_tool('request_visual_evidence',{
            'reference':first.structured_content['reference'],
            'recipe':{'kind':'image','start':.5,'end':.5}})
        assert not job.is_error
        jid=job.structured_content['id']
        from argon_recordings.visual_worker import run
        from types import SimpleNamespace
        def process_visual():
            with httpx.Client(base_url=origin,trust_env=False,headers={'Authorization':'Bearer synthetic-sdk-worker'}) as worker:
                return run(worker,SimpleNamespace(model=None))
        assert await asyncio.to_thread(process_visual)
        status=await client.call_tool('get_job',{'recording_id':rid,'job_id':jid})
        assert status.structured_content['state']=='completed'
        assets=await client.call_tool('list_visual_evidence',{'recording_id':rid})
        extracted=next(item for item in assets.structured_content['items'] if item['id']=='asset:'+jid)
        assert extracted['manifest']['source_origin_time']==0
        rendered=await client.call_tool('get_image',{'recording_id':rid,'evidence_id':'asset:'+jid})
        content=next(b for b in rendered.content if b.type=='image')
        decoded=subprocess.run(['ffmpeg','-v','error','-i','pipe:0','-f','null','-'],input=base64.b64decode(content.data),capture_output=True,timeout=15)
        assert decoded.returncode==0
        denied=await client.call_tool('get_recording',{'recording_id':'f'*32})
        assert denied.is_error
        assert browser.delete('/api/auth/agent-grants/'+grant_id,headers=headers).status_code==200
        revoked=await client.call_tool('get_image',{'recording_id':rid,'evidence_id':'frame:frame-0000.jpg'})
        assert revoked.is_error
