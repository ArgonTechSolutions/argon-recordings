"""Generation runs real API/storage/report code with only OpenAI transport substituted."""
import copy
import json

import httpx
import pytest
from fastapi import HTTPException

from test_workflow import client
from test_reports import draft, save, unpack
from test_accounts import team, login, invited
from argon_recordings.report_generation import Generation, GenerateReport, strict_schema
from argon_recordings.report_reading import READER_MODEL, WRITER_MODEL
from argon_recordings.report_contract import Report


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY','synthetic-test-only')
    monkeypatch.delenv('RECORDINGS_OPENAI_KEY_FILE',raising=False)
    monkeypatch.setenv('RECORDINGS_AI_BUDGET_USD','10')
    monkeypatch.setattr(Generation,'start',lambda self: None)


def queue(client,rid,**body):
    r=client.post(f'/api/recordings/{rid}/report-generation',json=body)
    assert r.status_code==202,r.text
    jid=r.json()['id']
    with client.app.state.store.db() as db:
        db.execute("UPDATE report_generation SET state='running' WHERE id=?",(jid,))
    return jid


def reading_response(payload):
    source=json.loads(payload['input'][0]['content'])
    items=source.get('passages',source.get('notes',[]))
    ids=list(dict.fromkeys(sid for item in items for sid in item.get('segment_ids',[item.get('id')])))[:4]
    value={'notes':[{'text':'Internal source summary: '+items[0].get('text','Source notes'),'segment_ids':ids}]}
    # Keep the structured response within its real field bounds.
    value['notes'][0]['text']=value['notes'][0]['text'][:600]
    return httpx.Response(200,json={'status':'completed','output':[{'type':'message','role':'assistant','content':[{'type':'output_text','text':json.dumps(value)}]}],
                                    'usage':{'input_tokens':100,'output_tokens':80}})


def transport(monkeypatch,client,body,*,images=True,alter=None,hook=None):
    original=httpx.AsyncClient; sent=[]
    def respond(request):
        payload=json.loads(request.content); sent.append(payload)
        assert payload['store'] is False and payload['model'] in {READER_MODEL,WRITER_MODEL}
        if payload['model']==READER_MODEL: return reading_response(payload)
        index=sum(p['model']==WRITER_MODEL for p in sent)
        if images and index==1:
            output=[{'type':'function_call','call_id':'read','name':'read_transcript','arguments':'{"offset":0}'}]
        elif images and index==2:
            output=[{'type':'function_call','call_id':'pixels','name':'inspect_image','arguments':'{"image_id":"frame:frame-0000.jpg"}'}]
        else:
            report=copy.deepcopy(body['report'])
            if not images:
                report['citations']=[c for c in report['citations'] if c['kind']=='transcript']
                report['items'][0]['citation_ids']=['speech']
            if alter: alter(report)
            if hook: hook()
            output=[{'type':'message','role':'assistant','content':[{'type':'output_text','text':json.dumps(report)}]}]
        return httpx.Response(200,json={'status':'completed','output':output,'usage':{'input_tokens':100,'output_tokens':80}})
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kwargs: original(**kwargs,transport=httpx.MockTransport(respond)))
    return sent


def test_generation_uses_real_pixels_and_saves_unreviewed_cited_draft(client,tmp_path,monkeypatch):
    rid,body,_=draft(client,tmp_path); jid=queue(client,rid,visuals=True)
    sent=transport(monkeypatch,client,body)
    client.portal.call(client.app.state.generation.run,jid)
    state=client.get(f'/api/recordings/{rid}/report-generation').json()
    job=state['jobs'][0]
    assert job['state']=='completed' and job['input_tokens']==400 and job['estimated_cost_usd']>0
    assert state['committed_usd']>0 and state['reservation_usd']==0
    report=client.get(f'/api/recordings/{rid}/reports/{job["report_id"]}').json()
    assert report['status']=='draft' and report['selected_image_count']==1
    assert report['generator']['model']==WRITER_MODEL
    image=sent[-1]['input'][-1]['output'][1]
    assert image['type']=='input_image' and image['image_url'].startswith('data:image/jpeg;base64,')
    with client.app.state.store.db() as db:
        trace=json.loads(db.execute('SELECT trace FROM report_generation WHERE id=?',(jid,)).fetchone()[0])
    assert [x['tool'] for x in trace]==['read_transcript','inspect_image']
    unpack(client,rid,job['report_id'])


def test_unsupplied_image_and_unread_citations_are_rejected(client,tmp_path,monkeypatch):
    rid,body,_=draft(client,tmp_path); jid=queue(client,rid,visuals=False)
    transport(monkeypatch,client,body,images=False,alter=lambda report: report['citations'].append(body['report']['citations'][1]))
    with pytest.raises(HTTPException):
        client.portal.call(client.app.state.generation.run,jid)
    assert client.get(f'/api/recordings/{rid}/reports').json()['reports']==[]


def test_source_change_and_cancel_during_paid_call_prevent_publication(client,tmp_path,monkeypatch):
    rid,body,_=draft(client,tmp_path); jid=queue(client,rid,visuals=False)
    def change():
        with client.app.state.store.db() as db: db.execute("UPDATE segments SET text='Changed' WHERE recording_id=?",(rid,))
    transport(monkeypatch,client,body,images=False,hook=change)
    with pytest.raises(HTTPException) as e: client.portal.call(client.app.state.generation.run,jid)
    assert e.value.status_code==409
    assert client.get(f'/api/recordings/{rid}/reports').json()['reports']==[]
    with client.app.state.store.db() as db: db.execute("UPDATE report_generation SET state='cancelled',reserved=0 WHERE id=?",(jid,))
    # Independently check cancellation inside the final save transaction.
    from argon_recordings.reports import SaveReport
    with pytest.raises(HTTPException) as e: client.app.state.generation.save_report(rid,SaveReport(**body),generation_id=jid)
    assert e.value.status_code==409


def test_advisory_estimate_queue_cancel_and_range(client,tmp_path):
    rid,_,_=draft(client,tmp_path); engine=client.app.state.generation
    engine.budget=.001
    base=f'/api/recordings/{rid}/report-generation'
    estimate=client.post(base+'/estimate',json={})
    assert estimate.status_code==200 and estimate.json()['allowance_warning']
    r=client.post(base,json={'start':.2,'end':.8}); assert r.status_code==202
    assert client.post(base,json={}).status_code in {409,429}
    assert client.get(base).json()['budget_mode']=='advisory'
    assert client.get(base).json()['committed_usd']>engine.budget
    jid=r.json()['id']
    with engine.store.db() as db:
        body=json.loads(db.execute('SELECT request FROM report_generation WHERE id=?',(jid,)).fetchone()[0])
    assert body['start']==0 and body['end']==1
    assert client.post(base+'/'+jid+'/cancel').json()['state']=='cancelled'
    assert client.get(base).json()['committed_usd']==0
    assert client.post(base,json={'end':50}).status_code==422
    engine.key=''
    assert client.post(base,json={}).status_code==503


def test_provider_failures_are_redacted_and_not_retried(client,tmp_path,monkeypatch):
    rid,body,_=draft(client,tmp_path); jid=queue(client,rid)
    original=httpx.AsyncClient; called=[]
    def fail(request):
        called.append(True)
        return httpx.Response(401,json={'error':'synthetic-private-response-not-for-browser'})
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kwargs: original(**kwargs,transport=httpx.MockTransport(fail)))
    with pytest.raises(HTTPException) as exc: client.portal.call(client.app.state.generation.run,jid)
    assert 'synthetic-private' not in str(exc.value.detail) and len(called)==1
    with client.app.state.store.db() as db:
        assert db.execute('SELECT cost FROM report_generation WHERE id=?',(jid,)).fetchone()[0]==0


def test_membership_removed_before_tool_prevents_provider_call(team,tmp_path,monkeypatch):
    app,owner,manager,uid=team; headers=login(owner)
    pid=owner.post('/api/projects',json={'name':'Private'},headers=headers).json()['id']
    with manager.store.db() as db:
        rid='a'*32
        db.execute("INSERT INTO recordings(id,project_id,title,filename,language,status,sha256,duration,created) VALUES(?,?,?,?,?,'ready',?,1,0)",(rid,pid,'Demo','x.wav','en','b'*64))
        db.execute("INSERT INTO segments(id,recording_id,start,end,text,words) VALUES(?,?,0,1,'Hello','[]')",('segment',rid))
    viewer,vh=invited(app,manager,uid,'viewer-ai@example.com')
    try:
        with manager.store.db() as db:
            viewer_id=db.execute('SELECT id FROM users WHERE email=?',('viewer-ai@example.com',)).fetchone()[0]
            db.execute("INSERT INTO memberships VALUES(?,?,'viewer')",(pid,viewer_id))
        base=f'/api/recordings/{rid}/report-generation'
        assert viewer.get(base).status_code==200
        assert viewer.post(base,json={},headers=vh).status_code==403
        queued=owner.post(base,json={},headers=headers); assert queued.status_code==202,queued.text
        jid=queued.json()['id']
        with manager.store.db() as db:
            db.execute("UPDATE report_generation SET state='running' WHERE id=?",(jid,))
            db.execute('DELETE FROM memberships WHERE project_id=? AND user_id=?',(pid,uid))
        with pytest.raises(HTTPException) as exc: owner.portal.call(app.state.generation.run,jid)
        assert exc.value.status_code==403
        assert owner.get(base).status_code==404
    finally: viewer.close()


def test_strict_schema_has_no_optional_object_keys():
    def check(value):
        if isinstance(value,dict):
            if value.get('type')=='object':
                assert set(value['required'])==set(value['properties']) and value['additionalProperties'] is False
            for v in value.values(): check(v)
        elif isinstance(value,list):
            for v in value: check(v)
    check(strict_schema(Report.model_json_schema()))


def test_clip_references_are_source_bound_playable_and_export_as_references(client,tmp_path):
    import hashlib
    rid,body,_=draft(client,tmp_path); asset_id='c'*32
    media=tmp_path/'clip.mp4';media.write_bytes(b'\x00\x00\x00\x18ftypisom-synthetic-video')
    sha=hashlib.sha256(media.read_bytes()).hexdigest()
    manifest={'kind':'clip','actual_start':0,'actual_end':1,'sha256':sha}
    with client.app.state.store.db() as db:
        db.execute("INSERT INTO visual_jobs(id,recording_id,source_sha256,recipe,cache_key,state,created) VALUES(?,?,?,'{}',?,'completed',0)",(asset_id,rid,body['report']['reference']['source_sha256'],asset_id))
        db.execute('INSERT INTO visual_assets VALUES(?,?,?,?,?,?)',(asset_id,rid,body['report']['reference']['source_sha256'],'{}',json.dumps(manifest),str(media)))
    body['report']['template_version']='argon-summary-v2'
    body['report']['video_references']=[{'item_id':'action','asset_id':asset_id,'sha256':sha,'interval':{'start':0,'end':1},'caption':'Demonstration'}]
    saved=save(client,rid,body)
    url=f'/api/recordings/{rid}/reports/{saved["id"]}/clips/{asset_id}'
    assert client.get(url).content==media.read_bytes()
    archive,_=unpack(client,rid,saved['id'])
    assert b'video is not included' in archive.read('report.html')
    assert not any(n.endswith('.mp4') for n in archive.namelist())
    media.write_bytes(b'changed')
    assert client.get(url).status_code==409
    assert client.get(url.replace(asset_id,'d'*32)).status_code==404
    body['expected_revision']=1; body['report']['video_references'][0]['interval']['end']=.5
    assert client.post(f'/api/recordings/{rid}/reports',json=body).status_code==422


def test_refinement_preserves_old_report_and_creates_a_new_draft(client,tmp_path,monkeypatch):
    rid,body,_=draft(client,tmp_path); previous=save(client,rid,body)
    jid=queue(client,rid,previous_report_id=previous['id'],kind='custom',instruction='Make this concise',visuals=False)
    sent=transport(monkeypatch,client,body,images=False)
    client.portal.call(client.app.state.generation.run,jid)
    assert json.loads(next(p for p in sent if p['model']==WRITER_MODEL)['input'][0]['content'])['previous_report']['items']==previous['report']['items']
    rows=client.get(f'/api/recordings/{rid}/reports').json()['reports']
    assert len(rows)==2 and rows[0]['title']=='Refined report' and rows[0]['status']=='draft'
    assert client.get(f'/api/recordings/{rid}/reports/{previous["id"]}').json()['report']==previous['report']


def test_stale_and_foreign_refinement_rejected_before_paid_call(client,tmp_path):
    rid,body,_=draft(client,tmp_path); previous=save(client,rid,body)
    with client.app.state.store.db() as db: db.execute("UPDATE segments SET text='Changed' WHERE recording_id=?",(rid,))
    jid=queue(client,rid,previous_report_id=previous['id'])
    with pytest.raises(HTTPException) as exc: client.portal.call(client.app.state.generation.run,jid)
    assert exc.value.status_code==409
    with client.app.state.store.db() as db: db.execute('UPDATE report_generation SET request=? WHERE id=?',(json.dumps({'previous_report_id':'f'*32}),jid))
    with pytest.raises(HTTPException) as exc: client.portal.call(client.app.state.generation.run,jid)
    assert exc.value.status_code==404


def test_literal_assignment_correction_is_bounded_and_preserves_validation(client,tmp_path,monkeypatch):
    rid,body,_=draft(client,tmp_path); jid=queue(client,rid,visuals=False)
    def invalidate(report):
        report['items'][0]['owner']['text']='Alice (inferred owner)'
    sent=transport(monkeypatch,client,body,images=False,alter=invalidate)
    with pytest.raises(HTTPException) as exc: client.portal.call(client.app.state.generation.run,jid)
    assert exc.value.status_code==422 and len(sent)==4
    assert 'Correct the report' in sent[-1]['input'][-1]['content']
    assert client.get(f'/api/recordings/{rid}/reports').json()['reports']==[]


def test_visual_request_reuses_real_worker_queue_and_inspects_registered_result(client,tmp_path,monkeypatch):
    from test_visual_jobs import manifest
    from test_reports import JPEG
    from test_workflow import AUTH
    rid,body,_=draft(client,tmp_path); jid=queue(client,rid,visuals=True)
    g=client.app.state.generation; original_schedule=g.visual_actions['schedule']; original=httpx.AsyncClient; requests=[]
    import asyncio
    tasks=[]
    async def publish():
        # Use the real ASGI worker routes while generation awaits its durable visual job.
        async with original(transport=httpx.ASGITransport(app=client.app),base_url='http://testserver') as worker:
            claim=(await worker.post('/api/worker/visual/claim',headers=AUTH)).json()
            headers={**AUTH,'X-Lease-Id':claim['lease_id']}
            assert (await worker.put(f'/api/worker/visual/{claim["id"]}/media',headers=headers,content=JPEG)).status_code==200
            m=manifest(JPEG);m.update(requested_start=0,requested_end=0,actual_start=0,actual_end=0)
            response=await worker.post(f'/api/worker/visual/{claim["id"]}/complete',headers=headers,json=m)
            assert response.status_code==200,response.text
    def schedule(rid,request,principal):
        job=original_schedule(rid,request,principal)
        tasks.append(asyncio.create_task(publish()))
        return job
    monkeypatch.setitem(g.visual_actions,'schedule',schedule)
    def respond(request):
        payload=json.loads(request.content);requests.append(payload)
        if payload['model']==READER_MODEL: return reading_response(payload)
        count=sum(p['model']==WRITER_MODEL for p in requests)
        if count==1: output=[{'type':'function_call','call_id':'read','name':'read_transcript','arguments':'{"offset":0}'}]
        elif count==2: output=[{'type':'function_call','call_id':'extract','name':'request_visual','arguments':'{"kind":"image","start":0,"end":0}'}]
        elif count==3:
            data=json.loads(payload['input'][-1]['output'])
            output=[{'type':'function_call','call_id':'inspect','name':'inspect_image','arguments':json.dumps({'image_id':data['image_id']})}]
        else:
            report=copy.deepcopy(body['report'])
            report['reference']=json.loads(payload['instructions'].split('\nCurrent reference: ')[1].split('\nScope: ')[0])
            image=json.loads(payload['input'][-1]['output'][0]['text'])
            report['citations'][1]['evidence_id']=image['id']
            output=[{'type':'message','role':'assistant','content':[{'type':'output_text','text':json.dumps(report)}]}]
        return httpx.Response(200,json={'status':'completed','output':output,'usage':{'input_tokens':100,'output_tokens':100}})
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kwargs: original(**kwargs,transport=httpx.MockTransport(respond)))
    client.portal.call(g.run,jid)
    jobs=client.get(f'/api/recordings/{rid}/report-generation').json()['jobs']
    assert jobs[0]['state']=='completed' and len(requests)==5
    for task in tasks:
        assert task.done()
        task.result()
    with g.store.db() as db:
        trace=json.loads(db.execute('SELECT trace FROM report_generation WHERE id=?',(jid,)).fetchone()[0])
    assert trace[-1]['image']['sha256']==__import__('hashlib').sha256(JPEG).hexdigest()


def test_private_key_file_contract_and_rejected_file_types(client,tmp_path,monkeypatch):
    key=tmp_path/'api.env';key.write_text('OPENAI_API_KEY="synthetic-secret-file"\n');key.chmod(0o600)
    monkeypatch.setenv('RECORDINGS_OPENAI_KEY_FILE',str(key))
    engine=Generation(client.app,None,None,None)
    assert engine.key=='synthetic-secret-file'
    key.chmod(0o644)
    with pytest.raises(ValueError): Generation(client.app,None,None,None)
    key.chmod(0o600);link=tmp_path/'link';link.symlink_to(key)
    monkeypatch.setenv('RECORDINGS_OPENAI_KEY_FILE',str(link))
    with pytest.raises(ValueError): Generation(client.app,None,None,None)
    monkeypatch.setenv('RECORDINGS_OPENAI_KEY_FILE',str(key));key.write_text('x'*4097)
    with pytest.raises(ValueError): Generation(client.app,None,None,None)


def test_custom_request_requires_a_task_and_focus_is_separate(client,tmp_path,monkeypatch):
    rid,body,_=draft(client,tmp_path)
    base=f'/api/recordings/{rid}/report-generation'
    for instruction in ['', '   ']:
        response=client.post(base,json={'kind':'custom','instruction':instruction,'focus':'Deployment details'})
        assert response.status_code==422 and response.json()['detail']=='Describe what the custom report should do'
    assert client.get(base).json()['jobs']==[]
    jid=queue(client,rid,kind='custom',instruction='Write an implementation guide',focus='Mobile accessibility',visuals=False)
    sent=transport(monkeypatch,client,body,images=False)
    client.portal.call(client.app.state.generation.run,jid)
    payload=json.loads(next(p for p in sent if p['model']==WRITER_MODEL)['input'][0]['content'])
    assert payload['request']=='Write an implementation guide'
    assert payload['focus']=='Mobile accessibility'
    assert client.get(base).json()['jobs'][0]['state']=='completed'


def test_scope_expansion_preserves_requested_fractional_video_bounds(client,tmp_path):
    rid,_,_=draft(client,tmp_path)
    with client.app.state.store.db() as db:
        db.execute('UPDATE recordings SET duration=1.037 WHERE id=?',(rid,))
        db.execute('UPDATE segments SET start=.137,end=.9 WHERE recording_id=?',(rid,))
    response=client.post(f'/api/recordings/{rid}/report-generation',json={'start':.03,'end':1.037})
    assert response.status_code==202,response.text
    with client.app.state.store.db() as db:
        request=json.loads(db.execute('SELECT request FROM report_generation WHERE id=?',(response.json()['id'],)).fetchone()[0])
    assert request['start']==.03 and request['end']==1.037


def test_hour_long_reading_is_batched_cached_and_never_pages_with_the_writer(client,tmp_path,monkeypatch):
    from argon_recordings.report_reading import price
    rid,body,_=draft(client,tmp_path)
    with client.app.state.store.db() as db:
        db.execute('UPDATE recordings SET duration=3600 WHERE id=?',(rid,))
        for index in range(1,1200):
            db.execute('INSERT INTO segments(id,recording_id,start,end,text,words) VALUES(?,?,?,?,?,?)',
                       (f'source-{index}',rid,index*3,index*3+1,'Synthetic meeting context about decisions and follow-up actions. '*8,'[]'))
    body['report']['scope']['end']=3600
    # Source checksum remains unchanged, transcript revision is computed from all passages.
    with client.app.state.store.db() as db:
        body['report']['reference']=client.app.state.generation.source(db,rid,GenerateReport())[0].model_dump()
    jid=queue(client,rid,visuals=False)
    sent=transport(monkeypatch,client,body,images=False)
    client.portal.call(client.app.state.generation.run,jid)
    readers=[p for p in sent if p['model']==READER_MODEL]
    writers=[p for p in sent if p['model']==WRITER_MODEL]
    observed=[s['id'] for p in readers for s in json.loads(p['input'][0]['content']).get('passages',[])]
    assert len(readers)>12 and len(writers)==1 and writers[0]['tools']==[]
    assert len(observed)==1200 and len(set(observed))==1200
    assert all(len(json.dumps(p['input']).encode())<35000 for p in readers)
    state=client.get(f'/api/recordings/{rid}/report-generation').json()
    assert state['jobs'][0]['state']=='completed'
    assert state['jobs'][0]['estimated_cost_usd']==pytest.approx(len(readers)*price(READER_MODEL,100,80)+price(WRITER_MODEL,100,80))
    cached_estimate=client.post(f'/api/recordings/{rid}/report-generation/estimate',json={}).json()
    assert cached_estimate['cached_batches']==sum('passages' in json.loads(p['input'][0]['content']) for p in readers)
    next_job=queue(client,rid,visuals=False)
    sent.clear()
    client.portal.call(client.app.state.generation.run,next_job)
    assert [p['model'] for p in sent]==[WRITER_MODEL]
    with client.app.state.store.db() as db: db.execute("UPDATE segments SET text='Changed source content' WHERE recording_id=? AND start=0",(rid,))
    refreshed=client.post(f'/api/recordings/{rid}/report-generation/estimate',json={}).json()
    assert refreshed['cached_batches']==0


def test_reader_cannot_invent_canonical_source_ids(client,tmp_path,monkeypatch):
    rid,_,_=draft(client,tmp_path);jid=queue(client,rid)
    original=httpx.AsyncClient
    def respond(request):
        response=reading_response(json.loads(request.content));data=response.json()
        value=json.loads(data['output'][0]['content'][0]['text']);value['notes'][0]['segment_ids']=['unknown-source']
        data['output'][0]['content'][0]['text']=json.dumps(value)
        return httpx.Response(200,json=data)
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kwargs: original(**kwargs,transport=httpx.MockTransport(respond)))
    with pytest.raises(HTTPException) as error:client.portal.call(client.app.state.generation.run,jid)
    assert 'unknown transcript passages' in error.value.detail
    with client.app.state.store.db() as db:assert not db.execute('SELECT 1 FROM report_readings').fetchone()


def test_optional_tool_rounds_finalize_instead_of_failing_on_step_limit(client,tmp_path,monkeypatch):
    from argon_recordings.report_generation import VISUAL_ROUNDS
    rid,body,_=draft(client,tmp_path);jid=queue(client,rid,visuals=True)
    original=httpx.AsyncClient;sent=[]
    def respond(request):
        payload=json.loads(request.content);sent.append(payload)
        if payload['model']==READER_MODEL:return reading_response(payload)
        if payload['tools']:
            output=[{'type':'function_call','call_id':f'read-{len(sent)}','name':'read_transcript','arguments':'{"offset":0}'}]
        else:
            report=copy.deepcopy(body['report']);report['citations']=report['citations'][:1]
            report['items'][0]['citation_ids']=['speech']
            output=[{'type':'message','role':'assistant','content':[{'type':'output_text','text':json.dumps(report)}]}]
        return httpx.Response(200,json={'status':'completed','output':output,'usage':{'input_tokens':100,'output_tokens':100}})
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kwargs: original(**kwargs,transport=httpx.MockTransport(respond)))
    client.portal.call(client.app.state.generation.run,jid)
    assert len(sent)==VISUAL_ROUNDS+2 and sent[-1]['tools']==[]
    assert client.get(f'/api/recordings/{rid}/report-generation').json()['jobs'][0]['state']=='completed'
