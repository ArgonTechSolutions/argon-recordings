import base64
import json

from fastapi.testclient import TestClient

from test_accounts import team,login,invited
from test_agent_grants import issue


def fixture(team,derive=False):
    app,owner,manager,uid=team;headers=login(owner)
    pid=owner.post('/api/projects',headers=headers,json={'name':'Agent evidence'}).json()['id']
    rid=owner.post(f'/api/projects/{pid}/recordings',headers=headers,json={'title':'Evidence demo','filename':'demo.mp4'}).json()['id']
    source=manager.store.root/'sources'/rid;source.write_bytes(b'synthetic source')
    with manager.store.db() as db:
        db.execute("UPDATE recordings SET sha256=?,bytes=16,duration=31,status='ready' WHERE id=?",('a'*64,rid))
        for i in range(5):
            db.execute('INSERT INTO segments VALUES(?,?,?,?,?,?)',(f'segment-{i}',rid,i*5,i*5+5,'Synthetic evidence passage '+str(i),'[]'))
            db.execute('INSERT INTO segment_search VALUES(?,?,?)',('Synthetic evidence passage '+str(i),f'segment-{i}',rid))
        image=manager.store.root/'results'/'synthetic.jpg';image.write_bytes(b'\xff\xd8\xffsynthetic-image')
        db.execute('INSERT INTO artifacts VALUES(?,?,?,?,?)',(rid,'frame-0000.jpg',0,'frame',str(image)))
    granted=issue(owner,headers,pid,derive_media=derive)
    agent=TestClient(app,base_url='http://localhost:8787',headers={'Authorization':'Bearer '+granted['token']})
    return app,owner,manager,headers,pid,rid,granted,agent


def test_agent_read_images_and_stable_transcript_pagination(team):
    app,owner,manager,headers,pid,rid,granted,agent=fixture(team)
    try:
        assert agent.get('/api/agent/projects').json()['items'][0]['id']==pid
        assert agent.get(f'/api/agent/projects/{pid}/recordings').json()['items'][0]['id']==rid
        recording=agent.get('/api/agent/recordings/'+rid).json()
        assert 'artifacts' not in recording['recording'] and recording['evidence']['source_sha256']=='a'*64
        assert recording['source_link'].startswith('/?project=')
        base='/api/agent/recordings/'+rid
        first=agent.get(base+'/transcript?limit=2').json();assert len(first['items'])==2 and first['next_cursor']
        second=agent.get(base+'/transcript',params={'limit':2,'cursor':first['next_cursor']}).json()
        third=agent.get(base+'/transcript',params={'limit':2,'cursor':second['next_cursor']}).json()
        assert [s['id'] for page in (first,second,third) for s in page['items']]==[f'segment-{i}' for i in range(5)]
        assert third['complete'] and third['next_cursor'] is None
        assert agent.get(base+'/transcript?cursor=not-valid').status_code==422
        assert agent.get(base+'/marks',params={'cursor':first['next_cursor']}).status_code==422
        for endpoint in ('speakers','marks','visual-evidence'):
            response=agent.get(base+'/'+endpoint);assert response.status_code==200,response.text
        visuals=agent.get(base+'/visual-evidence').json()['items']
        assert visuals[0]['id']=='frame:frame-0000.jpg'
        response=agent.get(base+'/images/frame:frame-0000.jpg')
        assert response.content.startswith(b'\xff\xd8\xff') and response.headers['content-type']=='image/jpeg'
        assert response.headers['cache-control']=='no-store'
        assert agent.get(base+'/images/asset:../../secret').status_code in {404,422}
        assert agent.get('/api/agent/search',params={'project_id':pid,'q':'evidence','mode':'keyword'}).json()['items']
        with manager.store.db() as db: db.execute("UPDATE segments SET text='Changed source words' WHERE id='segment-0'")
        assert agent.get(base+'/transcript',params={'cursor':first['next_cursor']}).status_code==409
        # Cookies alone never authorize the dedicated agent namespace.
        assert owner.get(base).status_code==401
    finally: agent.close()


def test_agent_derive_permission_revocation_and_cross_project(team):
    app,owner,manager,headers,pid,rid,granted,agent=fixture(team)
    base='/api/agent/recordings/'+rid
    try:
        state=agent.get(base).json()['evidence']
        body={'reference':{'recording_id':rid,'source_sha256':state['source_sha256'],'versions':state['versions'],'interval':{'start':0,'end':31}},
              'recipe':{'kind':'image','start':3,'end':3}}
        assert agent.post(base+'/visual-jobs',json=body).status_code==403
        other=owner.post('/api/projects',headers=headers,json={'name':'Outside grant'}).json()['id']
        other_rid=owner.post(f'/api/projects/{other}/recordings',headers=headers,json={'title':'Hidden','filename':'secret.wav'}).json()['id']
        assert agent.get(f'/api/agent/projects/{other}/recordings').status_code==404
        for suffix in ('','/transcript','/speakers','/marks','/visual-evidence','/images/frame:frame-0000.jpg','/visual-jobs/unknown'):
            assert agent.get('/api/agent/recordings/'+other_rid+suffix).status_code==404
        derived=issue(owner,headers,pid,derive_media=True)
        agent.headers['Authorization']='Bearer '+derived['token']
        response=agent.post(base+'/visual-jobs',json=body);assert response.status_code==200,response.text
        jid=response.json()['id']
        assert agent.get(base+'/visual-jobs/'+jid).json()['state']=='queued'
        assert agent.post(base+'/visual-jobs/'+jid+'/cancel').json()['state']=='cancelled'
        assert agent.post(base+'/visual-jobs/'+jid+'/cancel').status_code==200
        assert agent.get('/api/recordings/'+rid).status_code==401
        assert agent.post('/api/auth/agent-grants',json={}).status_code==401
        assert owner.delete('/api/auth/agent-grants/'+derived['grant']['id'],headers=headers).status_code==200
        assert agent.get(base).status_code==401
        assert agent.get(base+'/images/frame:frame-0000.jpg').status_code==401
    finally: agent.close()


def test_agent_image_budget_and_cursor_resource_binding(team):
    app,owner,manager,headers,pid,rid,granted,agent=fixture(team)
    try:
        base='/api/agent/recordings/'+rid
        first=agent.get(base+'/transcript?limit=1').json()
        decoded=json.loads(base64.urlsafe_b64decode(first['next_cursor']+'='*(-len(first['next_cursor'])%4)))
        decoded['key']='another-recording'
        cursor=base64.urlsafe_b64encode(json.dumps(decoded).encode()).decode().rstrip('=')
        assert agent.get(base+'/transcript',params={'cursor':cursor}).status_code==422
        assert agent.get(base+'/transcript?limit=10000').status_code==422
        image=manager.store.root/'results'/'synthetic.jpg'
        with image.open('wb') as stream: stream.truncate(4*1024**2+1)
        assert agent.get(base+'/images/frame:frame-0000.jpg').status_code==413
    finally: agent.close()


def test_one_contract_valid_segment_cannot_bypass_agent_context_budget(team):
    from argon_recordings.contracts import Segment
    app,owner,manager,headers,pid,rid,granted,agent=fixture(team)
    try:
        large=Segment(start=0,end=5,text='t'*8192,words=[{'start':0,'end':1,'word':'w'*200,'probability':1} for _ in range(1000)])
        with manager.store.db() as db:
            db.execute('UPDATE segments SET text=?,words=? WHERE id=?',(large.text,json.dumps([w.model_dump() for w in large.words]),'segment-0'))
        response=agent.get(f'/api/agent/recordings/{rid}/transcript?limit=1&include_words=true')
        assert response.status_code==422 and 'without word details' in response.json()['detail']
        assert len(response.content)<300
        page=agent.get(f'/api/agent/recordings/{rid}/transcript?limit=1').json()
        assert len(page['items'][0]['text'])==8192 and page['next_cursor']
    finally: agent.close()
