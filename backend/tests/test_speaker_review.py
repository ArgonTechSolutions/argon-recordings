import json

from test_workflow import client
from test_diarization import setup, worker_claim, result, AUTH


def reviewed(client):
    rid, _ = setup(client)
    with client.app.state.store.db() as db:
        db.execute('UPDATE segments SET words=? WHERE recording_id=?', (json.dumps([
            {'start': 0, 'end': .25, 'word': 'hello', 'probability': .9},
            {'start': .75, 'end': 1, 'word': 'world', 'probability': .9},
        ]),rid))
    snap = client.get(f'/api/recordings/{rid}/evidence').json()
    ref = {'recording_id': rid, 'source_sha256': snap['source_sha256'], 'versions': snap['versions'], 'interval': {'start': 0,'end': 1}}
    payload = result(); payload['turns'] = [{'start': 0,'end': .5,'speaker': 'speaker-7'}, {'start': .5,'end': 1,'speaker': 'speaker-8'}]
    publish(client,rid,ref,payload)
    return rid, ref


def publish(client,rid,ref,payload,options=None):
    body = {'reference': ref, **({'options': options} if options else {})}
    job = client.post(f'/api/recordings/{rid}/diarization',json=body)
    assert job.status_code == 200,job.text
    task = worker_claim(client)
    response = client.post(f'/api/worker/diarization/{task["id"]}/complete',headers={**AUTH,'X-Lease-Id':task['lease_id']},json=payload)
    assert response.status_code == 200,response.text


def mutation(client,rid):
    state = client.get(f'/api/recordings/{rid}/evidence').json()
    return {'expected_revision':state['versions']['speakers'],'expected_source_sha256':state['source_sha256']}


def test_name_and_range_edits_preserve_words_and_survive_cluster_swaps(client):
    rid,ref = reviewed(client)
    before = client.get(f'/api/recordings/{rid}/transcript').json()
    base = f'/api/recordings/{rid}'
    state = client.get(base+'/speakers').json()
    first, second = state['turns'][0]['speaker'],state['turns'][1]['speaker']
    assert state['passages'][0]['words'][0]['speaker_ids'] == [first]
    assert state['passages'][0]['words'][1]['speaker_ids'] == [second]
    stale = mutation(client,rid)
    assert client.put(base+f'/speakers/{first}',json={**stale,'label':'Alice'}).status_code == 200
    assert client.get(base).json()['evidence_versions']['speakers'] == stale['expected_revision']+1
    assert client.put(base+f'/speakers/{first}',json={**stale,'label':'Outdated'}).status_code == 409
    assigned = client.post(base+'/speaker-overrides',json={**mutation(client,rid),'override':{'interval':{'start':.6,'end':1},'speaker':first}})
    assert assigned.status_code == 200,assigned.text
    # Different run/options and arbitrary cluster numbers must not rename people.
    payload = result();payload['turns'] = [{'start':0,'end':.5,'speaker':'speaker-22'},{'start':.5,'end':1,'speaker':'speaker-21'}]
    publish(client,rid,ref,payload,{'min_speakers':2,'max_speakers':2})
    after = client.get(base+'/speakers').json()
    assert after['turns'][0]['speaker'] == first
    assert next(s['label'] for s in after['speakers'] if s['id']==first) == 'Alice'
    assert after['passages'][0]['words'][1]['speaker_ids'] == [first]
    assert after['passages'][0]['words'][1]['origin'] == 'manual'
    assert client.get(base+'/transcript').json() == before
    assert client.post(base+'/speakers/undo',json=mutation(client,rid)).status_code == 200
    assert client.get(base+'/speakers').json()['passages'][0]['words'][1]['speaker_ids'] == [second]


def test_unknown_override_reset_and_undo(client):
    rid,_ = reviewed(client);base=f'/api/recordings/{rid}'
    override=client.post(base+'/speaker-overrides',json={**mutation(client,rid),'override':{'interval':{'start':0,'end':1},'speaker':None}}).json()
    state=client.get(base+'/speakers').json()
    assert state['passages'][0]['speaker_ids']==[]
    assert all(w['origin']=='manual' for w in state['passages'][0]['words'])
    assert client.post(base+f'/speaker-overrides/{override["id"]}/reset',json=mutation(client,rid)).status_code==200
    assert client.get(base+'/speakers').json()['passages'][0]['speaker_ids']
    assert client.post(base+'/speakers/undo',json=mutation(client,rid)).status_code==200
    assert client.get(base+'/speakers').json()['passages'][0]['speaker_ids']==[]


def test_overlap_and_unknown_are_not_forced_to_one_speaker(client):
    rid,ref=reviewed(client);base=f'/api/recordings/{rid}'
    payload=result();payload['turns']=[{'start':0,'end':.3,'speaker':'speaker-0'}, {'start':0,'end':.3,'speaker':'speaker-1'}]
    publish(client,rid,ref,payload,{'min_speakers':2,'max_speakers':3})
    words=client.get(base+'/speakers').json()['passages'][0]['words']
    assert len(words[0]['speaker_ids'])==2
    assert words[1]['speaker_ids']==[] and words[1]['origin']=='unknown'


def test_paginated_speakers_reject_stale_revision(client):
    rid,_=reviewed(client);base=f'/api/recordings/{rid}'
    page=client.get(base+'/speakers?limit=1').json()
    assert len(page['turns'])==1 and page['next_turn_offset']==1
    expected=page['revision'];sid=page['speakers'][0]['id']
    client.put(base+f'/speakers/{sid}',json={**mutation(client,rid),'label':'Named'})
    assert client.get(base+f'/speakers?turn_offset=1&expected_revision={expected}').status_code==409


def test_invalid_ranges_and_wrong_speaker_do_not_modify_history(client):
    rid,_=reviewed(client);base=f'/api/recordings/{rid}'
    for interval in [{'start':0,'end':2},{'start':0,'end':0}]:
        assert client.post(base+'/speaker-overrides',json={**mutation(client,rid),'override':{'interval':interval,'speaker':None}}).status_code==422
    assert client.put(base+'/speakers/speaker-999',json={**mutation(client,rid),'label':'Other'}).status_code==404


def test_partial_unknown_override_visible_without_word_timestamps(client):
    rid,_=reviewed(client);base=f'/api/recordings/{rid}'
    with client.app.state.store.db() as db:
        db.execute("UPDATE segments SET words='[]' WHERE recording_id=?",(rid,))
    # Transcript edit makes raw inference stale, but human source-time overrides remain.
    response=client.post(base+'/speaker-overrides',json={**mutation(client,rid),'override':{'interval':{'start':.1,'end':.2},'speaker':'speaker-0'}})
    assert response.status_code==200
    state=client.get(base+'/speakers').json()
    assert state['stale']
    assert state['passages'][0]['speaker_ids']==['speaker-0']
    assert state['passages'][0]['has_unknown']


def test_merged_cluster_does_not_silently_inherit_a_persons_name(client):
    rid,ref=reviewed(client);base=f'/api/recordings/{rid}'
    first=client.get(base+'/speakers').json()['turns'][0]['speaker']
    client.put(base+f'/speakers/{first}',json={**mutation(client,rid),'label':'Alice'})
    payload=result();payload['turns']=[{'start':0,'end':1,'speaker':'speaker-0'}]
    publish(client,rid,ref,payload,{'min_speakers':1,'max_speakers':1})
    state=client.get(base+'/speakers').json()
    new_id=state['turns'][0]['speaker']
    assert new_id!=first
    assert next(n['label'] for n in state['speakers'] if n['id']==new_id).startswith('Speaker ')
