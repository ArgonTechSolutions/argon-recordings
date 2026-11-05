"""Static offline report rendering. Source strings are text, never Markdown/HTML."""
import base64
import hashlib
import html
import json
import re
import zipfile

from fastapi import HTTPException

from .reports import MAX_BUNDLE_BYTES, IMAGE_ID, checked_image

BUNDLE_VERSION='argon-evidence-bundle-v1'


def encoded(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,indent=2).encode()


def text(value):
    return html.escape(str(value),quote=True)


def markdown(value):
    # Escape source syntax and URL autolinks. Only renderer-owned links are active.
    escaped=re.sub(r'([\\`*_{}\[\]()#+.!|>\-])',r'\\\1',text(value))
    return escaped.replace(':','&#58;')


def stamp(value):
    return f'{int(value//60):02}:{value%60:06.3f}'


def image_filename(image_id):
    # IDs have already passed the report contract; filenames never use source labels.
    if not re.fullmatch(IMAGE_ID,image_id): raise HTTPException(422,'Invalid selected image ID')
    if image_id.startswith('asset:'): return 'images/asset-'+image_id[6:]+'.jpg'
    return 'images/'+image_id[6:]


def render(report,captured,metadata,inline_images=None):
    segments={s['id']:(index,s) for index,s in enumerate(captured['segments'])}
    links={}
    for citation in report.citations:
        if citation.kind=='transcript':
            links[citation.id]=f'transcript.html#s{segments[citation.evidence_id][0]}'
        else: links[citation.id]=image_filename(citation.evidence_id)
    state=('Checked by an editor' if metadata['status']=='reviewed' else 'Draft · Not checked by an editor')+(' · Source changed' if metadata['stale'] else '')
    body=[f'<h1>{text(metadata["title"])}</h1>',f'<p>{text(state)}</p>',
          f'<p>Scope: {stamp(report.scope.start)}–{stamp(report.scope.end)} · {text(report.coverage)}</p>']
    md=[f'# {markdown(metadata["title"])}',markdown(state),
        'Coverage and image inspection are importer assertions. Review status is an explicit editor action.',
        f'Scope: {stamp(report.scope.start)}–{stamp(report.scope.end)} · {report.coverage}']
    if report.limitations:
        body.append('<h2>Limitations</h2><ul>'+''.join(f'<li>{text(item)}</li>' for item in report.limitations)+'</ul>')
        md.append('## Limitations\n\n'+'\n'.join('- '+markdown(item) for item in report.limitations))
    citations={c.id:c for c in report.citations}
    rendered_items={}
    for item in report.items:
        begin=len(body)
        heading=f'{item.kind} · {item.basis}'
        body.append(f'<section><h2>{text(heading)}</h2><p>{text(item.text)}</p>')
        md.append(f'## {heading}\n\n{markdown(item.text)}')
        for label,value in (('Owner',item.owner),('Due',item.due)):
            if value:
                body.append(f'<p>{label}: {text(value.text)}<br>Source quote: {text(value.quote)}</p>')
                md.append(f'{label}: {markdown(value.text)}\n\nSource quote: {markdown(value.quote)}')
        for cid in item.citation_ids:
            citation=citations[cid];label=f'{stamp(citation.interval.start)}–{stamp(citation.interval.end)} · {citation.evidence_id}'
            body.append(f'<a href="{links[cid]}">{text(label)}</a> ')
            md.append(f'[{markdown(label)}]({links[cid]})')
        for video in report.video_references:
            if video.item_id == item.id:
                label=f'Video reference: {stamp(video.interval.start)}–{stamp(video.interval.end)}. {video.caption} (playback available in the app; video is not included in this export).'
                body.append(f'<p>{text(label)}</p>')
                md.append(markdown(label))
        for cid in item.citation_ids:
            citation=citations[cid]
            if citation.kind=='image':
                filename=image_filename(citation.evidence_id)
                md.append(f'![Supporting screenshot]({filename})')
                body.append(f'<figure><img src="{filename}" alt="Supporting screenshot"><figcaption>{stamp(citation.interval.start)} · Source screenshot</figcaption></figure>')
        body.append('</section>')
        rendered_items[item.id]=''.join(body[begin:])
        del body[begin:]
    item_map={item.id:item for item in report.items}
    covered=set()
    for block in report.blocks:
        body.append(f'<section class="widget {block.kind}"><h2>{text(block.title)}</h2>')
        if block.kind in {'actions','calendar'}:
            body.append('<table><thead><tr><th>Deliverable</th><th>Owner</th><th>Deadline as stated</th></tr></thead><tbody>')
            for iid in block.item_ids:
                item=item_map[iid]
                body.append(f'<tr><td><a href="#item-{iid}">{text(item.text)}</a></td><td>{text(item.owner.text if item.owner else "Not specified")}</td><td>{text(item.due.text if item.due else "Date to confirm")}</td></tr>')
            body.append('</tbody></table><details><summary>Supporting evidence</summary>')
        for iid in block.item_ids:
            body.append(f'<div id="item-{iid}">{rendered_items[iid]}</div>')
        if block.kind in {'actions','calendar'}: body.append('</details>')
        body.append('</section>')
        covered.update(block.item_ids)
    body.extend(rendered_items[item.id] for item in report.items if item.id not in covered)
    cited_images={c.evidence_id for c in report.citations if c.kind=='image'}
    for image in captured['images']:
        if image['id'] in cited_images: continue
        filename=image_filename(image['id']);label=f'{image["id"]} · {stamp(image["timestamp"])}'
        body.append(f'<figure><img src="{filename}" alt="{text(label)}"><figcaption>{text(label)}</figcaption></figure>')
        md.append(f'![{markdown(label)}]({filename})')
    provenance=[('Recording',report.reference.recording_id),('Project',captured['project_id']),
        ('Source SHA-256',report.reference.source_sha256),
        ('Evidence revisions',json.dumps(report.reference.versions.model_dump(),sort_keys=True)),
        ('Generator',metadata['generator']['name']+' '+metadata['generator']['version']),
        ('Generator model',metadata['generator'].get('model') or 'unspecified'),
        ('Template',report.template_version),('Speaker model',captured['speakers']['model'] or 'not available')]
    body.append('<details><summary>Report provenance</summary><h2>Provenance</h2>'+''.join(f'<p>{label}: {text(value)}</p>' for label,value in provenance))
    body.append('<p>Generator metadata and image inspection are importer assertions. Speaker labels are user assertions.</p><p><a href="manifest.json">Provenance manifest</a></p></details>')
    md.append('## Provenance\n\n'+'\n\n'.join(label+': '+markdown(value) for label,value in provenance))
    md.append('[Provenance manifest](manifest.json)')
    transcript=['<h1>Selected transcript passages</h1><p>This snapshot contains cited passages, not necessarily the whole transcript.</p>']
    for index,segment in enumerate(captured['segments']):
        transcript.append(f'<section id="s{index}"><h2>{stamp(segment["start"])}–{stamp(segment["end"])}</h2><p>{text(segment["text"])}</p></section>')

    if inline_images is not None:
        body=[part.replace('transcript.html#', '#').replace('<p><a href="manifest.json">Provenance manifest</a></p>', '') for part in body]
        expanded=sum(len(part.encode()) for part in body)
        for filename,data in inline_images.items():
            target='"'+filename+'"'
            count=sum(part.count(target) for part in body)
            encoded_size=4*((len(data)+2)//3)+len('data:image/jpeg;base64,')
            expanded+=count*(encoded_size-len(filename))
            # One image can be cited many times. Bound expansion before allocation.
            if expanded>MAX_BUNDLE_BYTES: raise HTTPException(422,'Embedded export exceeds 64 MiB')
            uri='data:image/jpeg;base64,'+base64.b64encode(data).decode('ascii')
            body=[part.replace(target, '"'+uri+'"') for part in body]
        body.append('<details class="sources"><summary>Source passages</summary>'+''.join(transcript)+'</details>')

    def page(content):
        return ('<!doctype html><html lang="'+text(report.language)+'"><head><meta charset="utf-8">'
            '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; img-src \'self\' data:; style-src \'unsafe-inline\'; base-uri \'none\'; form-action \'none\'">'
            '<meta name="viewport" content="width=device-width, initial-scale=1"><title>Recording report</title>'
            '<style>body{max-width:70ch;margin:2rem auto;padding:0 1rem;font:16px system-ui;line-height:1.5;overflow-wrap:anywhere}img{max-width:100%;height:auto}p{white-space:pre-wrap}section,figure{margin:2rem 0}h1{font-size:2.2rem;line-height:1.15}h2{text-transform:capitalize}table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:.75rem;border-bottom:1px solid #ddd;vertical-align:top}a{color:#6232a8}summary{cursor:pointer}figcaption{font-size:.85rem;color:#555}.widget{border-top:1px solid #ddd;padding-top:1rem}@media(max-width:600px){table{font-size:.85rem}th,td{padding:.4rem}}@media print{body{max-width:none;margin:0}figure,tr{break-inside:avoid}details:not([open]){display:none}a{color:inherit;text-decoration:none}}</style>'
            '</head><body>'+''.join(content)+'</body></html>').encode()
    return {'report.html':page(body),'report.md':('\n\n'.join(md)+'\n').encode(),
            'report.json':encoded(report.model_dump()),'transcript.html':page(transcript),
            'transcript.json':encoded(captured['segments']),'speakers.json':encoded(captured['speakers'])}


def write_bundle(output,report,captured,metadata,paths):
    files=render(report,captured,metadata);entries=[];total=0
    with zipfile.ZipFile(output,'w',compression=zipfile.ZIP_DEFLATED) as archive:
        def add(name,data):
            nonlocal total
            total+=len(data)
            if total>MAX_BUNDLE_BYTES: raise HTTPException(422,'Export exceeds 64 MiB; select fewer images or a smaller report')
            archive.writestr(name,data)
            entries.append({'path':name,'sha256':hashlib.sha256(data).hexdigest(),'bytes':len(data)})
        for name,data in files.items(): add(name,data)
        for image in captured['images']:
            data,digest=checked_image(paths[image['id']])
            if digest!=image['sha256']: raise HTTPException(409,'Selected image bytes changed; import a new draft')
            add(image_filename(image['id']),data)
        manifest={'format':BUNDLE_VERSION,'reference':report.reference.model_dump(),
            'project_id':captured['project_id'],
            'generator':metadata['generator'],'template_version':report.template_version,
            'report':{k:metadata[k] for k in ('id','status','revision','stale','created','reviewed')},
            'checked_reference':metadata['checked_reference'],'checked_at':metadata['checked_at'],
            'scope':report.scope.model_dump(),'coverage':report.coverage,
            'coverage_is_importer_assertion':True,'inspection_is_importer_assertion':True,
            'selection':'cited transcript passages, scope-filtered speaker snapshot, and explicitly selected images; no source/audio/video',
            'images':captured['images'],'files':entries.copy()}
        add('manifest.json',encoded(manifest))
    if output.tell()>MAX_BUNDLE_BYTES: raise HTTPException(422,'Encoded export exceeds 64 MiB')


def standalone(report,captured,metadata,paths):
    images={};total=0
    for image in captured['images']:
        data,digest=checked_image(paths[image['id']])
        if digest!=image['sha256']: raise HTTPException(409,'Selected image bytes changed; import a new draft')
        total+=len(data)
        if total>MAX_BUNDLE_BYTES*3//4: raise HTTPException(422,'Embedded export exceeds media limit')
        images[image_filename(image['id'])]=data
    result=render(report,captured,metadata,images)['report.html']
    if len(result)>MAX_BUNDLE_BYTES: raise HTTPException(422,'Embedded export exceeds 64 MiB')
    return result
