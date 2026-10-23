"""PCM planning and bounded timestamp reconciliation shared by worker and API."""
from collections import deque
import math
import re
import struct
import wave

from .contracts import Segment


def plan(audio, seconds=180, overlap=2):
    """Move nominal cuts to quiet 200 ms windows within five seconds."""
    with wave.open(str(audio), 'rb') as stream:
        if (stream.getnchannels(), stream.getsampwidth(), stream.getframerate()) != (1, 2, 16000):
            raise ValueError('Expected 16 kHz mono PCM')
        duration = stream.getnframes() / 16000
        cuts = [0.0]
        target = seconds
        while target < duration - 5:
            quiet = []
            for offset in range(-25, 26):
                position = max(cuts[-1] + 1, target + offset * .2)
                if position >= duration:
                    continue
                stream.setpos(round(position * 16000))
                data = stream.readframes(3200)
                samples = struct.unpack('<' + 'h' * (len(data) // 2), data)
                energy = sum(v * v for v in samples) / max(1, len(samples))
                quiet.append((energy, abs(position - target), position))
            cuts.append(min(quiet)[2])
            target = cuts[-1] + seconds
        cuts.append(duration)
    tasks = [dict(start=max(0, a-overlap), end=min(duration, b+overlap), core_start=a, core_end=b, boundary=False)
             for a, b in zip(cuts, cuts[1:])]
    # Conservative context retries own only the overlap strips.
    tasks += [dict(start=max(0, cut-max(12, overlap*3)), end=min(duration, cut+max(12, overlap*3)),
                   core_start=max(0, cut-overlap), core_end=min(duration, cut+overlap), boundary=True) for cut in cuts[1:-1]]
    return duration, tasks


def validate_words(segments, start, end):
    """Bound both inference plausibility and work before accepting durable JSON.

    Neighboring Whisper words may overlap by 200 ms. A sliding density window
    bounds even zero-duration timestamps; interval duration alone cannot do so.
    """
    count, previous = 0, None
    recent = deque()
    for segment in segments:
        for word in segment['words']:
            count += 1
            if count > math.ceil((end-start)*20)+128:
                raise ValueError('Chunk word count exceeds bounded speech density')
            if not start-.01 <= word['start'] <= word['end'] <= end+.01 or word['end']-word['start'] > 30:
                raise ValueError('Word timestamp outside plausible chunk interval')
            if previous and (word['start'] < previous['start'] or word['start'] < previous['end']-.2):
                raise ValueError('Word timeline overlaps excessively or is unordered')
            while recent and recent[0] < word['start']-1:
                recent.popleft()
            recent.append(word['start'])
            if len(recent) > 50:
                raise ValueError('Word timeline exceeds bounded speech density')
            previous = word


def token(word):
    return re.sub(r'\W+', '', word['word'].casefold())


def reliable_boundaries(rows):
    boundaries = sorted((r for r in rows if r['boundary']),key=lambda r:r['core_start'])
    core_words = [w for r in rows if not r['boundary'] for s in r['segments'] for w in s['words']
                  if r['core_start'] <= (w['start']+w['end'])/2 < r['core_end']]
    # Use wider context only where it covers the core-owned words.
    # Sorted intervals and a monotone cursor avoid originals x replacements.
    accepted = []
    core_words.sort(key=lambda w:(w['start']+w['end'])/2)
    cursor=0
    for boundary in boundaries:
        a,b=boundary['core_start'],boundary['core_end']
        replacements=sorted((w for s in boundary['segments'] for w in s['words'] if a <= (w['start']+w['end'])/2 < b),key=lambda w:w['start'])
        while cursor < len(core_words) and (core_words[cursor]['start']+core_words[cursor]['end'])/2 < a:
            cursor+=1
        reliable=True
        position=0
        scan=cursor
        while scan < len(core_words):
            original=core_words[scan]; midpoint=(original['start']+original['end'])/2
            if midpoint >= b:
                break
            if midpoint >= a:
                if not replacements:
                    reliable=False
                    scan+=1
                    continue
                while position < len(replacements) and replacements[position]['end'] < original['start']-.7:
                    position+=1
                covered=False
                for replacement_index in range(position,len(replacements)):
                    replacement=replacements[replacement_index]
                    if replacement['start'] > original['end']+.7:
                        break
                    if min(original['end'],replacement['end']) > max(original['start'],replacement['start']) or abs(midpoint-(replacement['start']+replacement['end'])/2) <= .35:
                        covered=True
                        break
                if not covered:
                    reliable=False
            scan+=1
        cursor=scan
        if reliable:
            accepted.append(boundary)
    return accepted


def reconcile(rows):
    """Sort once; compare only concurrent words belonging to different chunks."""
    for row in rows:
        validate_words(row['segments'],row['start'],row['end'])
        if any(s['text'].strip() and not s['words'] for s in row['segments']):
            raise ValueError('Chunk transcript needs word timestamps for safe boundary reconciliation')
    boundaries = reliable_boundaries(rows)
    accepted = {id(row) for row in boundaries}
    duration = max(r['end'] for r in rows)
    candidates, groups = [], {}
    for index,row in enumerate(rows):
        if row['boundary'] and id(row) not in accepted:
            continue
        owned = []
        for number,segment in enumerate(row['segments']):
            if not segment['words']:
                if segment['text'].strip():
                    raise ValueError('Chunk transcript needs word timestamps for safe boundary reconciliation')
                continue
            key = index,number
            groups[key] = []
            for word in segment['words']:
                midpoint = (word['start']+word['end'])/2
                if not (row['core_start'] <= midpoint < row['core_end'] or midpoint == row['core_end'] == duration):
                    continue
                owned.append((word,key))
        if row['boundary']:
            for word,key in owned:
                candidates.append((word,row,key))
        else:
            # Core and boundary ranges are ordered, so one moving cursor suffices.
            strips = iter(boundaries)
            strip = next(strips,None)
            for word,key in sorted(owned,key=lambda item:(item[0]['start']+item[0]['end'])/2):
                midpoint=(word['start']+word['end'])/2
                while strip and strip['core_end'] <= midpoint:
                    strip=next(strips,None)
                if not strip or not strip['core_start'] <= midpoint < strip['core_end']:
                    candidates.append((word,row,key))
    active = []
    for word,row,key in sorted(candidates,key=lambda item:(item[0]['start'],item[0]['end'])):
        # Validated non-overlap/density bounds keep this active set small. No
        # per-second indexing and no same-chunk token comparisons are needed.
        active = [(other,source) for other,source in active if other['end'] > word['start']]
        duplicate = False
        for other,source in active:
            if source is row:
                continue
            midpoint = (word['start']+word['end'])/2
            shared_start,shared_end=max(source['start'],row['start']),min(source['end'],row['end'])
            shortest=min(other['end']-other['start'],word['end']-word['start'])
            intersection=min(other['end'],word['end'])-max(other['start'],word['start'])
            if shared_start <= midpoint <= shared_end and shortest > 0 and intersection/shortest > .5 and token(other)==token(word):
                duplicate=True
                break
        if not duplicate:
            groups[key].append(word)
            active.append((word,row))
    output=[]
    for words in groups.values():
        if words:
            output.append(Segment(start=min(w['start'] for w in words),end=max(w['end'] for w in words),
                                  text=''.join(w['word'] for w in words).strip(),words=words))
    return sorted(output,key=lambda s:(s.start,s.end))
