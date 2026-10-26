"""Bounded evidence API for the stdio adapter; never browser/worker authority."""
import base64
import hashlib
import json
import re
from pathlib import Path
from typing import Literal
from urllib.parse import urlencode

from fastapi import HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import Field, ValidationError

from .agent_grants import authorize_project,authorize_recording
from .contracts import Strict
from .evidence import snapshot,marks
from .timeline import SourceReference,TimelineRange,EvidenceVersions
from .visual_jobs import VisualRequest,job_view


class Cursor(Strict):
    kind: str = Field(max_length=24)
    key: str = Field(max_length=128)
    offset: int = Field(default=0,ge=0,le=1000000)
    passage_offset: int = Field(default=0,ge=0,le=1000000)
    source_sha256: str | None = Field(default=None,pattern=r'^[a-f0-9]{64}$')
    versions: EvidenceVersions | None = None


def encode(cursor):
    return base64.urlsafe_b64encode(cursor.model_dump_json().encode()).decode().rstrip('=')


def decode(value,kind,key,state=None,version_keys=()):
    if value:
        try:
            if len(value)>2048 or not re.fullmatch(r'[A-Za-z0-9_-]+',value): raise ValueError()
            result=Cursor.model_validate_json(base64.urlsafe_b64decode(value+'='*(-len(value)%4)))
        except (ValueError,ValidationError): raise HTTPException(422,'Invalid evidence cursor') from None
        if result.kind!=kind or result.key!=key: raise HTTPException(422,'Cursor belongs to another resource')
        if state and (result.source_sha256!=state.source_sha256 or result.versions is None or
                      any(getattr(result.versions,k)!=getattr(state.versions,k) for k in version_keys)):
            raise HTTPException(409,'Evidence changed; restart pagination')
        return result
    return Cursor(kind=kind,key=key,source_sha256=state.source_sha256 if state else None,
                  versions=state.versions if state else None)


def source_reference(state):
    if state.duration is None or not state.source_sha256:
        raise HTTPException(409,'Recording evidence is not ready')
    return SourceReference(recording_id=state.recording_id,source_sha256=state.source_sha256,
                           interval=TimelineRange(start=0,end=state.duration),versions=state.versions)


def source_link(row,at=0):
    return '/?'+urlencode({'project':row['project_id'],'recording':row['id'],'at':at})


def install(app,get_accounts,recording_view,search,speaker_readers,visual_actions):
    def principal(request):
        manager=get_accounts()
        value=getattr(request.state,'agent',None)
        if not manager.enabled or not value: raise HTTPException(401,'An agent grant is required')
        return manager,value

    def recording(request,rid,derive=False):
        manager,who=principal(request)
        row=authorize_recording(manager,who,rid,derive)
        # Content is excluded from the audit trail.
        with manager.store.db() as db: manager.audit(db,who.user_id,'agent-evidence-request',rid)
        return manager,who,row

    @app.get('/api/agent/projects')
    def projects(request: Request,cursor: str | None = Query(default=None,max_length=2048),limit: int = Query(default=20,ge=1,le=40)):
        manager,who=principal(request)
        page=decode(cursor,'projects',who.grant_id)
        items=[]
        for pid in who.projects:
            try: role=authorize_project(manager,who,pid)
            except HTTPException as exc:
                if exc.status_code==404: continue
                raise
            row=manager.store.project(pid)
            if row: items.append(dict(row,role=role))
        items.sort(key=lambda p:p['id'])
        selected=items[page.offset:page.offset+limit]
        page.offset+=len(selected)
        return {'items':selected,'next_cursor':encode(page) if page.offset<len(items) else None}

    @app.get('/api/agent/projects/{pid}/recordings')
    def recordings(pid: str,request: Request,cursor: str | None = Query(default=None,max_length=2048),limit: int = Query(default=20,ge=1,le=40)):
        manager,who=principal(request);authorize_project(manager,who,pid)
        page=decode(cursor,'recordings',pid)
        with manager.store.db() as db:
            rows=list(db.execute('SELECT id,title,language,status,duration,created FROM recordings WHERE project_id=? ORDER BY created,id LIMIT ? OFFSET ?',(pid,limit+1,page.offset)))
        page.offset+=min(limit,len(rows))
        return {'items':[dict(row,project_id=pid,source_link=source_link({'id':row['id'],'project_id':pid})) for row in rows[:limit]],
                'next_cursor':encode(page) if len(rows)>limit else None}

    @app.get('/api/agent/search')
    def search_recordings(project_id: str,request: Request,q: str = Query(min_length=1,max_length=300),
                          mode: Literal['hybrid','keyword','semantic']='hybrid',
                          cursor: str | None = Query(default=None,max_length=2048),limit: int = Query(default=10,ge=1,le=40)):
        manager,who=principal(request);authorize_project(manager,who,project_id)
        key=hashlib.sha256(json.dumps([project_id,q,mode]).encode()).hexdigest()
        page=decode(cursor,'search',key)
        hits=search(project_id,q,mode)
        selected=hits[page.offset:page.offset+limit];page.offset+=len(selected)
        items=[]
        for hit in selected:
            hit=dict(hit);text=hit.pop('text')
            items.append(dict(hit,text_excerpt=text[:1000],excerpt_truncated=len(text)>1000,
                              source_link=source_link({'project_id':project_id,'id':hit['recording_id']},hit['start'])))
        return {'items':items,'next_cursor':encode(page) if page.offset<len(hits) else None}

    @app.get('/api/agent/recordings/{rid}')
    def get_recording(rid: str,request: Request):
        manager,_,row=recording(request,rid)
        with manager.store.db() as db: state=snapshot(db,manager.store,rid)
        value=dict(recording_view(row))
        # Browser media URLs are not credentials for the agent. Use image IDs.
        value.pop('artifacts',None)
        return {'recording':value,'evidence':state,'source_link':source_link(row)}

    @app.get('/api/agent/recordings/{rid}/transcript')
    def transcript(rid: str,request: Request,cursor: str | None = Query(default=None,max_length=2048),
                   limit: int = Query(default=20,ge=1,le=40),include_words: bool=False):
        manager,_,row=recording(request,rid)
        with manager.store.db() as db:
            db.execute('BEGIN IMMEDIATE');state=snapshot(db,manager.store,rid)
            ref=source_reference(state);page=decode(cursor,'transcript',rid,state,('transcript',))
            rows=list(db.execute('SELECT id,start,end,text,words FROM segments WHERE recording_id=? ORDER BY start,id LIMIT ? OFFSET ?',(rid,limit+1,page.offset)))
            selected=[];characters=0;word_count=0
            for item in rows[:limit]:
                words=json.loads(item['words']) if include_words else []
                cost=len(item['text'])+sum(len(w['word'])+100 for w in words)
                if not selected and (cost>32000 or len(words)>1000):
                    raise HTTPException(422,'Word details exceed the page budget; request this transcript without word details')
                if selected and (characters+cost>32000 or word_count+len(words)>1000): break
                selected.append({k:item[k] for k in ('id','start','end','text')})
                if include_words: selected[-1]['words']=words
                characters+=cost;word_count+=len(words)
            page.offset+=len(selected);more=len(rows)>len(selected)
            return {'items':selected,'next_cursor':encode(page) if more else None,'reference':ref,
                    'source_link':source_link(row),'complete':not more}

    @app.get('/api/agent/recordings/{rid}/speakers')
    def speakers(rid: str,request: Request,cursor: str | None = Query(default=None,max_length=2048),limit: int = Query(default=20,ge=1,le=40)):
        manager,_,row=recording(request,rid)
        with manager.store.db() as db:
            state=snapshot(db,manager.store,rid);ref=source_reference(state)
        page=decode(cursor,'speakers',rid,state,('transcript','speakers'))
        # Shared review logic includes manual precedence and uncertainty. Strip
        # word text from the agent speaker page; transcript pages carry speech.
        data=speaker_readers['speakers'](rid,page.passage_offset,page.offset,limit,
                                        state.versions.speakers,state.versions.transcript)
        items={'speakers':data.speakers[page.offset:page.offset+limit],
               'overrides':data.overrides[page.offset:page.offset+limit],
               'turns':data.turns,
               'passages':[{'segment_id':p.segment_id,'speaker_ids':p.speaker_ids,'has_unknown':p.has_unknown} for p in data.passages]}
        next_offset=page.offset+limit
        more=bool(data.next_passage_offset is not None or data.next_turn_offset is not None or
                  next_offset<len(data.speakers) or next_offset<len(data.overrides))
        page.offset=next_offset
        page.passage_offset=data.next_passage_offset if data.next_passage_offset is not None else page.passage_offset+len(data.passages)
        return {'items':items,'next_cursor':encode(page) if more else None,'reference':ref,
                'model':data.model,'stale':data.stale,'source_link':source_link(row)}

    @app.get('/api/agent/recordings/{rid}/marks')
    def list_marks(rid: str,request: Request,cursor: str | None = Query(default=None,max_length=2048),limit: int = Query(default=20,ge=1,le=40)):
        manager,_,row=recording(request,rid)
        with manager.store.db() as db:
            db.execute('BEGIN IMMEDIATE');state=snapshot(db,manager.store,rid)
            page=decode(cursor,'marks',rid,state,('annotations',));values=marks(db,rid).marks
        items=values[page.offset:page.offset+limit];page.offset+=len(items)
        return {'items':items,'next_cursor':encode(page) if page.offset<len(values) else None,
                'reference':source_reference(state),'source_link':source_link(row)}

    @app.get('/api/agent/recordings/{rid}/visual-evidence')
    def visual_evidence(rid: str,request: Request,cursor: str | None = Query(default=None,max_length=2048),limit: int = Query(default=20,ge=1,le=50)):
        manager,_,row=recording(request,rid)
        with manager.store.db() as db:
            db.execute('BEGIN IMMEDIATE');state=snapshot(db,manager.store,rid)
            page=decode(cursor,'visual-evidence',rid,state,('extraction',))
            legacy=[dict(id='frame:'+r['name'],kind='image',actual_start=r['time'],actual_end=r['time'],
                         source_sha256=state.source_sha256,legacy=True) for r in db.execute("SELECT name,time FROM artifacts WHERE recording_id=? AND kind='frame' ORDER BY time,name",(rid,))]
            derived=[dict(id='asset:'+r['id'],manifest=json.loads(r['manifest']),recipe=json.loads(r['recipe']),
                          source_sha256=r['source_sha256'],legacy=False) for r in db.execute('SELECT * FROM visual_assets WHERE recording_id=?',(rid,))]
        for item in derived:
            item.update(kind=item['manifest']['kind'],actual_start=item['manifest']['actual_start'],actual_end=item['manifest']['actual_end'])
        values=sorted(legacy+derived,key=lambda a:(a['actual_start'],a['id']))
        items=values[page.offset:page.offset+limit];page.offset+=len(items)
        return {'items':items,'next_cursor':encode(page) if page.offset<len(values) else None,
                'reference':source_reference(state),'source_link':source_link(row)}

    @app.get('/api/agent/recordings/{rid}/images/{evidence_id}')
    def image(rid: str,evidence_id: str,request: Request):
        manager,_,_=recording(request,rid)
        with manager.store.db() as db:
            if re.fullmatch(r'frame:frame-[0-9]{4}\.jpg',evidence_id):
                row=db.execute("SELECT path FROM artifacts WHERE recording_id=? AND name=? AND kind='frame'",(rid,evidence_id[6:])).fetchone()
            elif re.fullmatch(r'asset:[a-f0-9]{32}',evidence_id):
                row=db.execute('SELECT path,manifest FROM visual_assets WHERE recording_id=? AND id=?',(rid,evidence_id[6:])).fetchone()
                if row and json.loads(row['manifest'])['kind']!='image': raise HTTPException(422,'Request a screenshot for this clip time')
            else: raise HTTPException(422,'Choose a registered image ID')
        if not row: raise HTTPException(404,'Image not found')
        path=Path(row['path'])
        if not path.is_file(): raise HTTPException(410,'Image unavailable; request extraction again')
        if path.stat().st_size>4*1024**2: raise HTTPException(413,'Image exceeds agent budget; request a smaller screenshot')
        return FileResponse(path,media_type='image/jpeg',headers={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'})

    @app.post('/api/agent/recordings/{rid}/visual-jobs')
    def request_visual(rid: str,body: VisualRequest,request: Request):
        recording(request,rid,derive=True)
        return visual_actions['schedule'](rid,body,request)

    @app.get('/api/agent/recordings/{rid}/visual-jobs/{jid}')
    def get_job(rid: str,jid: str,request: Request):
        manager,_,_=recording(request,rid)
        with manager.store.db() as db:
            row=db.execute('SELECT * FROM visual_jobs WHERE recording_id=? AND id=?',(rid,jid)).fetchone()
            if not row: raise HTTPException(404,'Visual task not found')
            return job_view(row)

    @app.post('/api/agent/recordings/{rid}/visual-jobs/{jid}/cancel')
    def cancel(rid: str,jid: str,request: Request):
        recording(request,rid,derive=True)
        return visual_actions['cancel'](rid,jid)
