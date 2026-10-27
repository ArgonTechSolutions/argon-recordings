"""Human speaker labels and time-range overrides, separate from ASR words."""
import json
import bisect
import heapq
from typing import Literal

from fastapi import HTTPException
from pydantic import Field

from .contracts import Strict
from .evidence import MarkMutation, snapshot
from .store import identity
from .timeline import TimelineRange

SCHEMA = '''
CREATE TABLE IF NOT EXISTS speaker_identities(
 recording_id TEXT NOT NULL REFERENCES recordings(id),id TEXT NOT NULL,
 label TEXT NOT NULL DEFAULT '',PRIMARY KEY(recording_id,id));
CREATE TABLE IF NOT EXISTS speaker_overrides(
 id TEXT PRIMARY KEY,recording_id TEXT NOT NULL REFERENCES recordings(id),
 payload TEXT NOT NULL,revision INTEGER NOT NULL,deleted INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS speaker_overrides_recording ON speaker_overrides(recording_id);
CREATE TABLE IF NOT EXISTS speaker_edit_history(
 id TEXT PRIMARY KEY,recording_id TEXT NOT NULL REFERENCES recordings(id),kind TEXT NOT NULL,
 target TEXT NOT NULL,previous TEXT,revision INTEGER NOT NULL,undone INTEGER NOT NULL DEFAULT 0);
'''


class SpeakerName(Strict):
    id: str
    label: str


class RenameSpeaker(MarkMutation):
    label: str = Field(min_length=1, max_length=80, pattern=r'\S')


class OverrideInput(Strict):
    interval: TimelineRange
    speaker: str | None = Field(default=None, pattern=r'^speaker-[0-9]{1,4}$')


class AssignSpeaker(MarkMutation):
    override: OverrideInput


class OverrideView(OverrideInput):
    id: str


class WordSpeaker(TimelineRange):
    word: str
    speaker_ids: list[str]
    origin: Literal['automatic', 'manual', 'unknown']


class PassageSpeakers(Strict):
    segment_id: str
    speaker_ids: list[str]
    words: list[WordSpeaker]
    has_unknown: bool


def merged(intervals):
    result = []
    for start, end in sorted(intervals):
        if result and start <= result[-1][1]:
            result[-1] = (result[-1][0], max(end, result[-1][1]))
        else:
            result.append((start, end))
    return result


def intersection(a, b):
    i = j = 0; total = 0
    while i < len(a) and j < len(b):
        total += max(0, min(a[i][1], b[j][1])-max(a[i][0], b[j][0]))
        if a[i][1] < b[j][1]: i += 1
        else: j += 1
    return total


def stable_turns(db, rid, turns):
    """Reuse a prior identity only for a confident mutual temporal match.

    Cluster numbers have no identity meaning across reruns. Ambiguous matches
    get a fresh generic identity, never silently inherit a person's name.
    """
    previous = db.execute('SELECT turns FROM diarization_results WHERE recording_id=?', (rid,)).fetchone()
    old, new = {}, {}
    for turn in json.loads(previous['turns']) if previous else []:
        old.setdefault(turn['speaker'], []).append((turn['start'], turn['end']))
    for turn in turns:
        new.setdefault(turn['speaker'], []).append((turn['start'], turn['end']))
    old = {s: merged(v) for s, v in old.items()}; new = {s: merged(v) for s, v in new.items()}
    weights = {(a, b): intersection(av, bv) for a, av in old.items() for b, bv in new.items()}
    mapping = {}
    for b, spans in new.items():
        candidates = sorted(old, key=lambda a: weights[a, b], reverse=True)
        if not candidates:
            continue
        a = candidates[0]
        best_new = max(new, key=lambda n: weights[a, n])
        duration = max(sum(e-s for s,e in old[a]), sum(e-s for s,e in spans))
        if best_new == b and duration and weights[a,b] / duration >= .95:
            mapping[b] = a
    used = {r[0] for r in db.execute('SELECT id FROM speaker_identities WHERE recording_id=?', (rid,))} | set(old)
    for b in new:
        if b not in mapping:
            number = next((i for i in range(10000) if f'speaker-{i}' not in used), None)
            if number is None:
                raise HTTPException(409, 'Speaker identity history limit reached')
            mapping[b] = f'speaker-{number}'; used.add(mapping[b])
        db.execute('INSERT OR IGNORE INTO speaker_identities(recording_id,id) VALUES(?,?)', (rid, mapping[b]))
    return [{**t, 'speaker': mapping[t['speaker']]} for t in turns]


def review_projection(db, rid, turns, offset=0, limit=500):
    overrides = [OverrideView(id=r['id'], **json.loads(r['payload'])) for r in db.execute(
        'SELECT id,payload FROM speaker_overrides WHERE recording_id=? AND deleted=0 ORDER BY revision', (rid,))]
    names = [SpeakerName(id=r['id'], label=r['label'] or f'Speaker {int(r["id"].split("-")[1])+1}')
             for r in db.execute('SELECT id,label FROM speaker_identities WHERE recording_id=? ORDER BY id', (rid,))]
    raw = {}
    for turn in turns:
        raw.setdefault(turn['speaker'], []).append((turn['start'], turn['end']))
    raw = {s: merged(spans) for s, spans in raw.items()}
    ends = {s: [e for _,e in spans] for s,spans in raw.items()}
    starts = {}
    for order, override in enumerate(overrides):
        starts.setdefault(override.interval.start, []).append((order,override))
    points = sorted({p for o in overrides for p in (o.interval.start,o.interval.end)})
    active, manual_spans = [], []
    for point, next_point in zip(points, points[1:]):
        for order, override in starts.get(point, []):
            heapq.heappush(active, (-order, override.interval.end, override.speaker))
        while active and active[0][1] <= point:
            heapq.heappop(active)
        if active:
            manual_spans.append((point,next_point,active[0][2]))
    manual_starts = [s for s,_,_ in manual_spans]
    boundary_indexes = [(spans,ends[s]) for s,spans in raw.items()]
    boundary_indexes.append(([(s,e) for s,e,_ in manual_spans],[e for _,e,_ in manual_spans]))
    def assign(start, end):
        center = (start+end)/2
        index = bisect.bisect_right(manual_starts, center)-1
        if index >= 0 and manual_spans[index][0] <= center < manual_spans[index][1]:
            speaker = manual_spans[index][2]
            return ([speaker] if speaker else []), 'manual'
        ids = []
        for speaker, spans in raw.items():
            index = bisect.bisect_right(ends[speaker], start)
            overlap = 0
            while index < len(spans) and spans[index][0] < end:
                overlap += max(0,min(end,spans[index][1])-max(start,spans[index][0])); index += 1
            if overlap >= max(.001,(end-start)*.5): ids.append(speaker)
        ids.sort()
        return ids, 'automatic' if ids else 'unknown'
    passages = []
    words_used = 0
    rows = list(db.execute('SELECT id,start,end,words FROM segments WHERE recording_id=? ORDER BY start,id LIMIT ? OFFSET ?', (rid,limit+1,offset)))
    for segment in rows[:limit]:
        source_words = json.loads(segment['words'])
        if words_used+len(source_words)>5000 and passages: break
        words_used += len(source_words)
        words = []
        for word in source_words:
            ids, origin = assign(word['start'], word['end'])
            words.append(WordSpeaker(start=word['start'], end=word['end'], word=word['word'], speaker_ids=ids, origin=origin))
        unknown = any(not w.speaker_ids for w in words)
        if words:
            ids = sorted({s for w in words for s in w.speaker_ids})
        else:
            # Segments without word timestamps still honor partial range edits.
            boundaries = {segment['start'],segment['end']}
            for spans,span_ends in boundary_indexes:
                index = bisect.bisect_right(span_ends,segment['start'])
                while index<len(spans) and spans[index][0]<segment['end']:
                    start,end=spans[index];index+=1
                    boundaries.update((max(start,segment['start']),min(end,segment['end'])))
            ordered = sorted(boundaries); all_ids = set()
            for start,end in zip(ordered,ordered[1:]):
                part,_ = assign(start,end); all_ids.update(part); unknown |= not part
            ids = sorted(all_ids)
        passages.append(PassageSpeakers(segment_id=segment['id'], speaker_ids=ids, words=words,
                                        has_unknown=unknown if words or segment['end']>segment['start'] else not ids))
    next_offset = offset+len(passages) if len(passages)<len(rows) else None
    return names, overrides, passages, next_offset


def install(app, get_store):
    def check(db, store, rid, body):
        state = snapshot(db, store, rid)
        if state.versions.speakers != body.expected_revision or state.source_sha256 != body.expected_source_sha256:
            raise HTTPException(409, 'Speaker annotations changed; refresh before editing')
        if db.execute('SELECT count(*) FROM speaker_edit_history WHERE recording_id=?', (rid,)).fetchone()[0] >= 5000:
            raise HTTPException(409, 'Speaker edit history limit reached')
        return state

    def history(db, rid, kind, target, previous, revision):
        db.execute('INSERT INTO speaker_edit_history VALUES(?,?,?,?,?,?,0)', (identity(),rid,kind,target,previous,revision))
        db.execute('UPDATE evidence_versions SET speakers=? WHERE recording_id=?', (revision,rid))

    @app.post('/api/recordings/{rid}/speakers', response_model=SpeakerName, status_code=201)
    def create(rid: str, body: RenameSpeaker):
        store = get_store()
        with store.db() as db:
            db.execute('BEGIN IMMEDIATE'); state = check(db,store,rid,body)
            if state.duration is None:
                raise HTTPException(409, 'Recording timeline is not ready')
            used = {r[0] for r in db.execute('SELECT id FROM speaker_identities WHERE recording_id=?', (rid,))}
            number = next((i for i in range(10000) if f'speaker-{i}' not in used), None)
            if number is None:
                raise HTTPException(409, 'Speaker identity history limit reached')
            sid = f'speaker-{number}'
            label = body.label.strip()
            db.execute('INSERT INTO speaker_identities VALUES(?,?,?)', (rid,sid,label))
            history(db,rid,'create',sid,None,state.versions.speakers+1)
        return SpeakerName(id=sid,label=label)

    @app.put('/api/recordings/{rid}/speakers/{sid}')
    def rename(rid: str, sid: str, body: RenameSpeaker):
        store = get_store()
        with store.db() as db:
            db.execute('BEGIN IMMEDIATE'); state = check(db,store,rid,body)
            row = db.execute('SELECT label FROM speaker_identities WHERE recording_id=? AND id=?', (rid,sid)).fetchone()
            if not row: raise HTTPException(404, 'Speaker not found')
            history(db,rid,'rename',sid,row['label'],state.versions.speakers+1)
            db.execute('UPDATE speaker_identities SET label=? WHERE recording_id=? AND id=?', (body.label.strip(),rid,sid))
        return {'ok': True}

    @app.post('/api/recordings/{rid}/speaker-overrides')
    def override(rid: str, body: AssignSpeaker):
        store = get_store()
        with store.db() as db:
            db.execute('BEGIN IMMEDIATE'); state = check(db,store,rid,body)
            if state.duration is None: raise HTTPException(409, 'Recording timeline is not ready')
            try: body.override.interval.within(state.duration)
            except ValueError as exc: raise HTTPException(422, str(exc)) from exc
            if body.override.interval.end <= body.override.interval.start:
                raise HTTPException(422, 'Speaker assignment requires a non-empty range')
            if body.override.speaker and not db.execute('SELECT 1 FROM speaker_identities WHERE recording_id=? AND id=?', (rid,body.override.speaker)).fetchone():
                raise HTTPException(404, 'Speaker not found')
            oid = identity(); revision = state.versions.speakers+1
            db.execute('INSERT INTO speaker_overrides VALUES(?,?,?,?,0)', (oid,rid,body.override.model_dump_json(),revision))
            history(db,rid,'override',oid,None,revision)
        return {'id': oid}

    @app.post('/api/recordings/{rid}/speaker-overrides/{oid}/reset')
    def reset(rid: str, oid: str, body: MarkMutation):
        store = get_store()
        with store.db() as db:
            db.execute('BEGIN IMMEDIATE'); state = check(db,store,rid,body)
            row = db.execute('SELECT id FROM speaker_overrides WHERE recording_id=? AND id=? AND deleted=0', (rid,oid)).fetchone()
            if not row: raise HTTPException(404, 'Speaker override not found')
            history(db,rid,'reset',oid,None,state.versions.speakers+1)
            db.execute('UPDATE speaker_overrides SET deleted=1 WHERE id=?', (oid,))
        return {'ok': True}

    @app.post('/api/recordings/{rid}/speakers/undo')
    def undo(rid: str, body: MarkMutation):
        store = get_store()
        with store.db() as db:
            db.execute('BEGIN IMMEDIATE'); state = check(db,store,rid,body)
            event = db.execute('SELECT * FROM speaker_edit_history WHERE recording_id=? AND undone=0 ORDER BY revision DESC LIMIT 1', (rid,)).fetchone()
            if not event: raise HTTPException(409, 'No speaker edit to undo')
            if event['kind'] == 'rename':
                db.execute('UPDATE speaker_identities SET label=? WHERE recording_id=? AND id=?', (event['previous'],rid,event['target']))
            elif event['kind'] == 'create':
                # A later automatic run may have reused the identity. Never leave
                # published turns or active manual ranges referencing a removed name.
                result = db.execute('SELECT turns FROM diarization_results WHERE recording_id=?', (rid,)).fetchone()
                turns = json.loads(result['turns']) if result else []
                overrides = db.execute('SELECT payload FROM speaker_overrides WHERE recording_id=? AND deleted=0', (rid,))
                if any(t['speaker'] == event['target'] for t in turns) or any(
                        json.loads(o['payload'])['speaker'] == event['target'] for o in overrides):
                    raise HTTPException(409, 'Speaker is in use; undo its range edits first')
                db.execute('DELETE FROM speaker_identities WHERE recording_id=? AND id=?', (rid,event['target']))
            else:
                db.execute('UPDATE speaker_overrides SET deleted=? WHERE id=?', (0 if event['kind']=='reset' else 1,event['target']))
            db.execute('UPDATE speaker_edit_history SET undone=1 WHERE id=?', (event['id'],))
            db.execute('UPDATE evidence_versions SET speakers=? WHERE recording_id=?', (state.versions.speakers+1,rid))
        return {'ok': True}
