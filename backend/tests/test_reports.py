"""Report permissions, immutable snapshots and offline export integrity."""
import hashlib
import asyncio
import io
import json
from pathlib import PurePosixPath
import zipfile

import pytest

from test_workflow import client
from test_visual_jobs import setup
from test_accounts import team,login,invited


JPEG=b'\xff\xd8\xffdisposable-report-image'


def test_export_disconnect_releases_resources_even_when_send_fails():
    from argon_recordings.reports import ExportResponse
    from starlette.requests import ClientDisconnect
    released=[]
    async def exercise():
        async def chunks(): yield b'export chunk'
        async def receive(): return {'type':'http.disconnect'}
        async def send(message):
            if message['type']=='http.response.body': raise OSError('synthetic disconnect')
        response=ExportResponse(chunks(),cleanup=lambda:released.append(True))
        with pytest.raises(ClientDisconnect):
            await response({'type':'http','asgi':{'spec_version':'2.4'}},receive,send)
    asyncio.run(exercise())
    assert released==[True]


def draft(client,tmp_path):
    rid,_=setup(client)
    store=client.app.state.store
    image=tmp_path/'private-file.jpg';image.write_bytes(JPEG)
    with store.db() as db:
        db.execute("UPDATE artifacts SET path=? WHERE recording_id=? AND name='frame-0000.jpg'",(str(image),rid))
        sid=db.execute('SELECT id FROM segments WHERE recording_id=?',(rid,)).fetchone()[0]
        db.execute('UPDATE segments SET text=? WHERE id=?',('Alice: I will send the patch by Friday. <script>window.pwned=1</script>',sid))
    state=client.get(f'/api/recordings/{rid}/evidence').json()
    body={'expected_revision':0,'title':'../<img src="https://evil.invalid" onerror="window.pwned=1">',
          'generator':{'name':'Fixture <script>window.pwned=1</script>','version':'1.0','model':'fixture'},
          'report':{'template_version':'argon-summary-v1','reference':{
              'recording_id':rid,'source_sha256':state['source_sha256'],'interval':{'start':0,'end':1},'versions':state['versions']},
              'scope':{'start':0,'end':1},'language':'en','coverage':'complete','limitations':[],
              'citations':[{'id':'speech','kind':'transcript','evidence_id':sid,'interval':{'start':0,'end':1}},
                           {'id':'screen','kind':'image','evidence_id':'frame:frame-0000.jpg','interval':{'start':0,'end':0}}],
              'items':[{'id':'action','kind':'action','basis':'explicit','text':'Send patch. [unsafe](https://evil.invalid) ![x](file:///etc/passwd)',
                        'citation_ids':['speech','screen'],'owner':{'text':'Alice','quote':'Alice: I will send the patch by Friday.','citation_ids':['speech']}}]},
          'inspected_images':[{'id':'frame:frame-0000.jpg','sha256':hashlib.sha256(JPEG).hexdigest(),'inspected':True}]}
    return rid,body,image


def save(client,rid,body):
    response=client.post(f'/api/recordings/{rid}/reports',json=body)
    assert response.status_code==201,response.text
    return response.json()


def unpack(client,rid,report_id):
    response=client.get(f'/api/recordings/{rid}/reports/{report_id}/export.zip')
    assert response.status_code==200,response.text
    assert response.headers['cache-control']=='no-store'
    archive=zipfile.ZipFile(io.BytesIO(response.content))
    manifest=json.loads(archive.read('manifest.json'))
    for entry in manifest['files']:
        data=archive.read(entry['path'])
        assert len(data)==entry['bytes'] and hashlib.sha256(data).hexdigest()==entry['sha256']
    assert {e['path'] for e in manifest['files']}==set(archive.namelist())-{'manifest.json'}
    assert all(not PurePosixPath(n).is_absolute() and '..' not in PurePosixPath(n).parts for n in archive.namelist())
    return archive,manifest


def test_snapshot_review_and_portable_selected_bundle(client,tmp_path):
    rid,body,_=draft(client,tmp_path);saved=save(client,rid,body)
    assert saved['status']=='draft' and not saved['stale']
    root=f'/api/recordings/{rid}/reports'
    assert client.post(root,json=body).status_code==409
    assert client.get(root).json()['revision']==1
    review=client.post(root+'/'+saved['id']+'/review',json={'expected_revision':1})
    assert review.status_code==200 and review.json()['status']=='reviewed'
    assert client.get(f'/api/projects/{client.app.state.store.recording(rid)["project_id"]}/recordings').json()[0]['report_revision']==2
    archive,manifest=unpack(client,rid,saved['id'])
    assert manifest['report']['status']=='reviewed' and manifest['reference']==body['report']['reference']
    assert manifest['generator']['version']=='1.0' and manifest['images'][0]['recipe_version']=='legacy-sparse-v1'
    assert manifest['coverage_is_importer_assertion'] and manifest['inspection_is_importer_assertion']
    assert archive.read('images/frame-0000.jpg')==JPEG
    assert len(json.loads(archive.read('transcript.json')))==1
    assert 'labels_are_user_assertions' in json.loads(archive.read('speakers.json'))
    assert not any(name.endswith(('.mp4','.wav','.sqlite')) for name in archive.namelist())
    html=archive.read('report.html').decode();transcript=archive.read('transcript.html').decode()
    assert '<script>' not in html+transcript and '&lt;script&gt;' in transcript
    assert 'src="https://evil.invalid"' not in html and 'Content-Security-Policy' in html
    assert 'href="transcript.html#s0"' in html
    assert '[unsafe](https://evil.invalid)' not in archive.read('report.md').decode()
    assert str(tmp_path).encode() not in b''.join(archive.read(n) for n in archive.namelist())


def test_stale_report_keeps_snapshot_but_cannot_be_reviewed_or_resaved(client,tmp_path):
    rid,body,_=draft(client,tmp_path);saved=save(client,rid,body)
    with client.app.state.store.db() as db: db.execute('UPDATE segments SET text=? WHERE recording_id=?',('Changed source transcript',rid))
    root=f'/api/recordings/{rid}/reports'
    assert client.get(root).json()['reports'][0]['stale']
    assert client.post(root+'/'+saved['id']+'/review',json={'expected_revision':1}).status_code==409
    body['expected_revision']=1
    assert client.post(root,json=body).status_code==409
    archive,manifest=unpack(client,rid,saved['id'])
    assert manifest['report']['stale']
    assert manifest['checked_reference']['versions']['transcript']>manifest['reference']['versions']['transcript']
    assert b'Changed source transcript' not in archive.read('transcript.json')
    assert b'Alice' in archive.read('transcript.json')
    (client.app.state.store.root/'sources'/rid).unlink()
    unpack(client,rid,saved['id'])


def test_media_integrity_missing_source_and_foreign_evidence(client,tmp_path):
    rid,body,image=draft(client,tmp_path)
    body['inspected_images'][0]['sha256']='0'*64
    assert client.post(f'/api/recordings/{rid}/reports',json=body).status_code==409
    body['inspected_images'][0]['sha256']=hashlib.sha256(JPEG).hexdigest()
    saved=save(client,rid,body)
    image.write_bytes(JPEG+b'changed')
    assert client.get(f'/api/recordings/{rid}/reports/{saved["id"]}/export.zip').status_code==409
    image.unlink()
    assert client.get(f'/api/recordings/{rid}/reports/{saved["id"]}/export.zip').status_code==410
    image.write_bytes(JPEG)
    body['expected_revision']=1
    body['report']['citations'][0]['evidence_id']='another-recording-segment'
    assert client.post(f'/api/recordings/{rid}/reports',json=body).status_code==422
    body['inspected_images'][0]['id']='frame:../../etc/passwd'
    assert client.post(f'/api/recordings/{rid}/reports',json=body).status_code==422


def test_snapshot_export_and_request_caps_release_resources(client,tmp_path,monkeypatch):
    import argon_recordings.reports as reports
    import argon_recordings.report_bundle as bundle
    rid,body,_=draft(client,tmp_path);root=f'/api/recordings/{rid}/reports'
    monkeypatch.setattr(reports,'MAX_SNAPSHOT_BYTES',16)
    assert client.post(root,json=body).status_code==422
    assert client.get(root).json()['revision']==0
    monkeypatch.undo();saved=save(client,rid,body)
    monkeypatch.setattr(bundle,'MAX_BUNDLE_BYTES',128)
    for _ in range(3):
        assert client.get(root+'/'+saved['id']+'/export.zip').status_code==422
    monkeypatch.undo();unpack(client,rid,saved['id'])
    assert client.post(root,content=b'x'*(1024**2+1),headers={'Content-Type':'application/json'}).status_code==413


def test_report_roles_revocation_and_explicit_review(team,tmp_path):
    app,owner,manager,uid=team;headers=login(owner)
    pid=owner.post('/api/projects',headers=headers,json={'name':'Report team'}).json()['id']
    rid=owner.post(f'/api/projects/{pid}/recordings',headers=headers,json={'title':'Private','filename':'private.mp4'}).json()['id']
    source=manager.store.root/'sources'/rid;source.write_bytes(b'source')
    with manager.store.db() as db:
        db.execute("UPDATE recordings SET sha256=?,bytes=6,duration=1,status='ready' WHERE id=?",(hashlib.sha256(b'source').hexdigest(),rid))
        db.execute('INSERT INTO segments VALUES(?,?,?,?,?,?)',('selected',rid,0,1,'A source statement','[]'))
    state=owner.get(f'/api/recordings/{rid}/evidence').json()
    body={'expected_revision':0,'title':'Review','generator':{'name':'Synthetic','version':'1'},'report':{
        'reference':{'recording_id':rid,'source_sha256':state['source_sha256'],'interval':{'start':0,'end':1},'versions':state['versions']},
        'scope':{'start':0,'end':1},'language':'en','coverage':'complete','citations':[{'id':'c','kind':'transcript','evidence_id':'selected','interval':{'start':0,'end':1}}],
        'items':[{'id':'item','kind':'summary','basis':'explicit','text':'A source statement','citation_ids':['c']}]}}
    root=f'/api/recordings/{rid}/reports'
    assert owner.post(root,json=body).status_code==403
    saved=owner.post(root,headers=headers,json=body);assert saved.status_code==201,saved.text
    viewer,vh=invited(app,manager,uid,'report-viewer@example.com')
    try:
        assert owner.put(f'/api/projects/{pid}/members',headers=headers,json={'email':'report-viewer@example.com','role':'viewer'}).status_code==200
        assert viewer.get(root).status_code==200
        assert viewer.post(root,headers=vh,json=body).status_code==403
        report_id=saved.json()['id'];path=root+'/'+report_id
        assert viewer.post(path+'/review',headers=vh,json={'expected_revision':1}).status_code==403
        assert viewer.patch(path,headers=vh,json={'title':'Hijacked','expected_revision':1}).status_code==403
        assert owner.patch(path,json={'title':'Missing CSRF','expected_revision':1}).status_code==403
        assert viewer.get(path+'/export.html').status_code==200
        assert viewer.get(path+'/export.zip').status_code==200
        assert owner.request('DELETE',f'/api/projects/{pid}/members',headers=headers,json={'email':'report-viewer@example.com'}).status_code==200
        assert viewer.get(path).status_code==404 and viewer.get(path+'/export.zip').status_code==404
    finally: viewer.close()


def test_rename_and_embedded_export_keep_evidence_and_check_state(client,tmp_path):
    rid,body,_=draft(client,tmp_path)
    body['report']['template_version']='argon-summary-v3'
    body['report']['blocks']=[{'id':'figure','kind':'figure','title':'Design changes','item_ids':['action']}]
    saved=save(client,rid,body);root=f'/api/recordings/{rid}/reports/{saved["id"]}'
    assert client.patch(root,json={'title':'New name','expected_revision':0}).status_code==409
    assert client.patch(root,json={'title':'   ','expected_revision':1}).status_code==422
    renamed=client.patch(root,json={'title':'  Project deliverables  ','expected_revision':1})
    assert renamed.status_code==200 and renamed.json()['title']=='Project deliverables'
    assert renamed.json()['status']=='draft'
    assert client.get(root).json()['report']==saved['report']
    page=client.get(root+'/export.html')
    assert page.status_code==200 and page.headers['content-type'].startswith('text/html')
    assert '.html' in page.headers['content-disposition']
    content=page.text
    assert 'data:image/jpeg;base64,' in content and 'src="images/' not in content
    assert '<script>' not in content and '&lt;script&gt;' in content
    assert 'href="#s0"' in content and 'id="s0"' in content
    assert content.index('<img src=') < content.index('Report provenance')
    assert '<h2>Design changes</h2>' in content


@pytest.mark.parametrize('blocks',[
    [{'id':'a','kind':'narrative','title':'Example','item_ids':['missing']}],
    [{'id':'a','kind':'actions','title':'Example','item_ids':['action']},
     {'id':'b','kind':'calendar','title':'Example','item_ids':['action']}],
])
def test_blocks_reject_unknown_or_repeated_items(client,tmp_path,blocks):
    rid,body,_=draft(client,tmp_path)
    body['report']['template_version']='argon-summary-v3';body['report']['blocks']=blocks
    assert client.post(f'/api/recordings/{rid}/reports',json=body).status_code==422


def test_schedule_requires_actions_and_figure_requires_inspected_image(client,tmp_path):
    rid,body,_=draft(client,tmp_path)
    body['report']['template_version']='argon-summary-v3'
    body['report']['blocks']=[{'id':'a','kind':'calendar','title':'Deliverables','item_ids':['action']}]
    body['report']['items'][0].update(kind='summary',owner=None)
    assert client.post(f'/api/recordings/{rid}/reports',json=body).status_code==422
    body['report']['blocks'][0]['kind']='figure'
    body['report']['items'][0]['citation_ids']=['speech']
    body['report']['citations']=body['report']['citations'][:1];body['inspected_images']=[]
    assert client.post(f'/api/recordings/{rid}/reports',json=body).status_code==422


def test_embedded_export_bounds_repeated_images_before_base64_allocation(client,tmp_path,monkeypatch):
    from argon_recordings import report_bundle
    rid,body,image=draft(client,tmp_path)
    data=JPEG+b'x'*10000;image.write_bytes(data)
    body['inspected_images'][0]['sha256']=hashlib.sha256(data).hexdigest()
    item=body['report']['items'][0]
    body['report']['items']=[{**item,'id':f'item-{i}'} for i in range(10)]
    saved=save(client,rid,body)
    monkeypatch.setattr(report_bundle,'MAX_BUNDLE_BYTES',35000)
    def forbidden_allocation(_): raise AssertionError('Expansion must be bounded before encoding')
    monkeypatch.setattr(report_bundle.base64,'b64encode',forbidden_allocation)
    response=client.get(f'/api/recordings/{rid}/reports/{saved["id"]}/export.html')
    assert response.status_code==422 and 'Embedded export exceeds' in response.text
