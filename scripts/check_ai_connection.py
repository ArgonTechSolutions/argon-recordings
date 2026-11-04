"""One bounded live API smoke check inside the coordinator; synthetic data only.

Run through the coordinator's private runtime. Uses its configured key without
printing it. Creates no production recording/report and removes its scratch DB.
"""
import asyncio
import hashlib
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace

from fastapi import HTTPException
from argon_recordings.api import Settings, create_app
from argon_recordings.embeddings import Embeddings
from argon_recordings.report_generation import Generation, GenerateReport, RESERVATION
from argon_recordings.reports import current_reference, install as install_reports
from argon_recordings.store import Store, identity


async def check():
    with tempfile.TemporaryDirectory(prefix='recordings-ai-smoke-') as directory:
        settings=Settings(data=directory,reserve=0,auth_mode='local',embeddings=Embeddings())
        app=create_app(settings)
        store=Store(Path(directory))
        app.state.store=store
        app.state.accounts=SimpleNamespace(enabled=False)
        persist=install_reports(app,lambda:store,0)
        engine=Generation(app,settings,persist,{})
        if not engine.key or engine.budget<RESERVATION:
            raise HTTPException(503,'Configure a private API key and an allowance of at least two USD before checking')
        pid,rid,sid,jid=(identity() for _ in range(4));now=time.time()
        text='Synthetic deployment check: the team decided to review the mobile layout tomorrow. No owner was assigned.'
        with store.db() as db:
            db.execute('INSERT INTO projects VALUES(?,?,?)',(pid,'Synthetic deployment check',now))
            db.execute("INSERT INTO recordings(id,project_id,title,filename,language,status,sha256,duration,created) VALUES(?,?,?,?,?,'ready',?,1,?)",
                       (rid,pid,'Synthetic API check','synthetic.wav','en',hashlib.sha256(text.encode()).hexdigest(),now))
            db.execute('INSERT INTO segments VALUES(?,?,0,1,?,?)',(sid,rid,text,'[]'))
            reference=current_reference(db,store,rid)
            request=GenerateReport(kind='summary',visuals=False,end=1)
            db.execute("INSERT INTO report_generation(id,recording_id,request,reference,state,stage,created,updated,period,reserved) VALUES(?,?,?,?,'running','Synthetic connection check',?,?,?,?)",
                       (jid,rid,request.model_dump_json(),reference.model_dump_json(),now,now,engine.period(),RESERVATION))
        async with asyncio.timeout(360):
            await engine.run(jid)
        with store.db() as db:
            job=db.execute('SELECT * FROM report_generation WHERE id=?',(jid,)).fetchone()
            report=db.execute('SELECT status,payload FROM reports WHERE id=?',(job['report_id'],)).fetchone()
            if job['state']!='completed' or not report or report['status']!='draft' or not json.loads(report['payload'])['citations']:
                raise HTTPException(502,'Synthetic cited draft was not completed')
            print(json.dumps({'synthetic_openai_check':'passed','model':'gpt-6.1-sol','review_state':'draft','estimated_cost_usd':round(job['cost'],6),'production_data_used':False}))


if __name__=='__main__':
    try:
        asyncio.run(check())
    except HTTPException as exc:
        print(json.dumps({'synthetic_openai_check':'failed','detail':exc.detail}))
        raise SystemExit(1) from None
    except Exception as exc:
        print(json.dumps({'synthetic_openai_check':'failed','error_type':type(exc).__name__}))
        raise SystemExit(1) from None
