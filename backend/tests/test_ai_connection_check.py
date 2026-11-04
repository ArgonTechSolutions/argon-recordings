"""The deploy checker uses private scratch space, never the live database or 64 MiB tmpfs."""
import asyncio
import json
from pathlib import Path
import shutil

import httpx
import pytest
from fastapi import HTTPException

from scripts.check_ai_connection import check
from test_report_generation import reading_response
from argon_recordings.report_reading import READER_MODEL


@pytest.mark.parametrize('provider_status',[200,401])
def test_checker_uses_data_filesystem_and_cleans_only_scratch(tmp_path,monkeypatch,capsys,provider_status):
    data=tmp_path/'data';data.mkdir()
    production=data/'recordings.sqlite';production.write_bytes(b'untouched-production-database-sentinel')
    monkeypatch.setenv('RECORDINGS_DATA',str(data))
    monkeypatch.setenv('OPENAI_API_KEY','synthetic-checker-only')
    monkeypatch.delenv('RECORDINGS_OPENAI_KEY_FILE',raising=False)
    monkeypatch.setenv('RECORDINGS_AI_BUDGET_USD','10')
    original_client=httpx.AsyncClient;original_usage=shutil.disk_usage
    roots=[]
    def usage(path):
        path=Path(path);roots.append(path)
        if path.is_relative_to(data): return original_usage(path)
        # Reproduce the installed runtime's less-than-64-MiB free /tmp.
        return type(original_usage(data))(64*1024**2,1024**2,63*1024**2)
    monkeypatch.setattr(shutil,'disk_usage',usage)
    def respond(request):
        payload=json.loads(request.content)
        if provider_status!=200: return httpx.Response(provider_status,json={'error':'synthetic'})
        if payload['model']==READER_MODEL: return reading_response(payload)
        first=json.loads(payload['input'][0]['content'])
        segment=first['evidence'][0]
        report={'reference':first['reference'],'scope':first['scope'],'language':'en','coverage':'complete','limitations':[],
                'citations':[{'id':'source','kind':'transcript','evidence_id':segment['id'],'interval':{'start':segment['start'],'end':segment['end']}}],
                'items':[{'id':'summary','kind':'summary','basis':'explicit','text':'Review the mobile layout.','citation_ids':['source']}]}
        output=[{'type':'message','role':'assistant','content':[{'type':'output_text','text':json.dumps(report)}]}]
        return httpx.Response(200,json={'status':'completed','output':output,'usage':{'input_tokens':100,'output_tokens':100}})
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kwargs: original_client(**kwargs,transport=httpx.MockTransport(respond)))
    if provider_status==200:
        asyncio.run(check())
        assert json.loads(capsys.readouterr().out)['synthetic_openai_check']=='passed'
        assert roots and all(path.parent==data for path in roots)
    else:
        with pytest.raises(HTTPException):asyncio.run(check())
    assert production.read_bytes()==b'untouched-production-database-sentinel'
    assert list(data.iterdir())==[production]
