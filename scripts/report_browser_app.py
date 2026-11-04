"""Synthetic provider for the bounded reports browser check. Never a runtime entrypoint."""
import asyncio
import json
import os

import httpx

from argon_recordings.api import create_app

_original_client = httpx.AsyncClient


async def synthetic_response(request):
    await asyncio.sleep(.5)
    payload=json.loads(request.content)
    first=json.loads(payload['input'][0]['content'])
    outputs={item['call_id']:item['output'] for item in payload['input'] if item.get('type')=='function_call_output'}
    if 'transcript' not in outputs:
        output=[{'type':'function_call','call_id':'transcript','name':'read_transcript','arguments':'{"offset":0}'}]
    elif first['kind']=='report' and 'screen' not in outputs:
        output=[{'type':'function_call','call_id':'screen','name':'inspect_image','arguments':'{"image_id":"frame:frame-0000.jpg"}'}]
    else:
        segments=json.loads(outputs['transcript'])['items']
        reference=json.loads(payload['instructions'].split('\nCurrent reference: ')[1].split('\nScope: ')[0])
        citations=[{'id':'source','kind':'transcript','evidence_id':segments[0]['id'],
                    'interval':{'start':segments[0]['start'],'end':segments[0]['end']}}]
        if 'screen' in outputs:
            citations.append({'id':'screen','kind':'image','evidence_id':'frame:frame-0000.jpg','interval':{'start':0,'end':0}})
        report={'template_version':'argon-summary-v1','reference':reference,'scope':first['scope'],
            'language':first['language'],'coverage':'complete','limitations':[], 'citations':citations,
            'items':[{'id':'main','kind':'action' if first['kind']=='actions' else 'summary','basis':'explicit',
                'text':'Review the mobile workflow. Source: '+segments[0]['text'],
                'citation_ids':[c['id'] for c in citations],'owner':None,'due':None,'visual_observation':False}]}
        output=[{'type':'message','role':'assistant','content':[{'type':'output_text','text':json.dumps(report)}]}]
    return httpx.Response(200,json={'status':'completed','output':output,'usage':{'input_tokens':100,'output_tokens':100}})


def create_fixture_app():
    if os.environ.get('OPENAI_API_KEY')!='synthetic-browser-only':
        raise RuntimeError('This fixture requires the synthetic browser key')
    httpx.AsyncClient=lambda **kwargs: _original_client(**kwargs,transport=httpx.MockTransport(synthetic_response))
    return create_app()
