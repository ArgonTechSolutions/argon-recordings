"""Disposable old-source -> current-source SQLite compatibility check; never installs services."""
from __future__ import annotations

import argparse
import hashlib
import io
from importlib.metadata import version
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]
BASELINE = '3d4eda7'


def checked(response, status=200):
    assert response.status_code == status, (response.status_code, response.text)
    return response.json()


def snapshot(data):
    """Hash every original column/row, including hashed credentials and FTS data."""
    with sqlite3.connect(data / 'recordings.sqlite') as db:
        tables = {}
        for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
            quoted = '"' + name.replace('"', '""') + '"'
            columns = [r[1] for r in db.execute(f'PRAGMA table_info({quoted})')]
            rows = [json.dumps(list(row), default=lambda blob: {'hex': blob.hex()}, sort_keys=True)
                    for row in db.execute(f'SELECT * FROM {quoted}')]
            tables[name] = {'columns': columns, 'rows': sorted(rows)}
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert not db.execute('PRAGMA foreign_key_check').fetchall()
    return tables


def files(data):
    return {str(p.relative_to(data)): hashlib.sha256(p.read_bytes()).hexdigest()
            for directory in ('sources', 'results') for p in (data / directory).rglob('*') if p.is_file()}


def dump(path, value):
    path.write_text(json.dumps(value, indent=2) + '\n')
    path.chmod(0o600)  # All credentials here are disposable synthetic fixtures.


def cookies(client):
    return [{'name':c.name,'value':c.value,'domain':c.domain,'path':c.path} for c in client.cookies.jar]


def restore_cookies(client, values):
    for value in values:
        client.cookies.set(**value)


def installer_snapshot(work):
    import recordings
    from scripts.worker_stack import worker_args
    config_path = work/'installation/recordings.toml'
    before = hashlib.sha256(config_path.read_bytes()).hexdigest()
    config = recordings.load_config(config_path)
    assert config['workers'] == 1 and config['embedding'] == 'large'
    assert config['turbo'] == (work/'retained-turbo').resolve()
    api = recordings.start_args(config, {'image':'disposable-not-built'})
    worker = worker_args(config, {'image':'disposable-not-built'}, 1)
    assert worker[worker.index('--cpus')+1] == '2.0'
    assert '--add-host' not in worker
    assert worker[worker.index('--dns')+1] == '127.0.0.1'
    assert 'RECORDINGS_WORKER_TOKEN' not in ' '.join(api+worker)
    assert hashlib.sha256(config_path.read_bytes()).hexdigest() == before
    return {'config_sha256':before,'identity':config['identity'],'home':str(config['home']),
            'turbo':str(config['turbo']),'api_args':api,'worker_args':worker}


def phase(source, work, mode):
    # Separate interpreters ensure no module from the other source tree is reused.
    sys.path[:0] = [str(source), str(source / 'backend'), str(source / 'backend/tests')]
    from fastapi.testclient import TestClient
    from argon_recordings.api import Settings, create_app
    from test_accounts import login, invited, session, PASSWORD
    from test_workflow import wav, completion, AUTH
    from passkey_fixture import Authenticator, enroll, headers as admin_headers
    from cryptography.hazmat.primitives.asymmetric import ec

    data = work / 'data'
    app = create_app(Settings(data, auth_mode='accounts', worker_token='synthetic-test-only', reserve=0))
    base = 'http://localhost:8787'
    def authenticator():
        key = Authenticator()
        key.key = ec.derive_private_key(42, ec.SECP256R1())
        key.id = b'disposable-upgrade-authenticator!'
        return key

    if mode == 'seed':
        (work/'installation').mkdir()
        (work/'installation/recordings.toml').write_text(
            '[app]\nname="argon-upgrade-fixture"\nport=8877\n'
            '[models]\nturbo_path="../retained-turbo"\n'
            '[resources]\ncpus=2.5\n[auth]\nmode="accounts"\n')
        installation = installer_snapshot(work)
        with TestClient(app, base_url=base) as owner:
            manager = app.state.accounts
            uid = manager.bootstrap('owner@example.com', 'Upgrade owner', PASSWORD)
            oh = login(owner)
            checked(owner.post('/api/auth/login', json={'email':'owner@example.com', 'password':PASSWORD,
                         'audience':'admin'}, headers=session(owner, 'admin')))
            enroll(owner, manager, authenticator=authenticator())
            pid = checked(owner.post('/api/projects', json={'name':'Historical project'}, headers=oh), 201)['id']
            sessions = {'owner': cookies(owner)}
            for role in ('editor', 'viewer', 'outsider'):
                member, _ = invited(app, manager, uid, role+'@example.com')
                try:
                    if role != 'outsider':
                        checked(owner.put(f'/api/projects/{pid}/members', json={
                            'email':role+'@example.com', 'role':role}, headers=oh))
                    sessions[role] = cookies(member)
                finally:
                    member.close()
            manager.invite(uid, 'pending@example.com')
            rid = checked(owner.post(f'/api/projects/{pid}/recordings', json={
                'title':'Historical completed recording', 'filename':'fixture.wav', 'language':'es'}, headers=oh), 201)['id']
            checked(owner.put(f'/api/recordings/{rid}/source', content=wav(), headers=oh))
            task = checked(owner.post('/api/worker/claim', json={'worker':'fixture'}, headers=AUTH))
            wh, body = completion(owner, task)
            checked(owner.post(f'/api/worker/jobs/{task["job_id"]}/complete', json=body, headers=wh))
            pending = checked(owner.post(f'/api/projects/{pid}/recordings', json={
                'title':'Historical queued recording', 'filename':'queued.wav'}, headers=oh), 201)['id']
            checked(owner.put(f'/api/recordings/{pending}/source', content=wav(), headers=oh))
            transcript = checked(owner.get(f'/api/recordings/{rid}/transcript'))
            assert owner.get(f'/api/recordings/{rid}/evidence').status_code == 404
            state = {'project':pid, 'recording':rid, 'pending':pending, 'sessions':sessions,
                     'transcript':transcript, 'source_files':files(data),'installation':installation}
        state['legacy_tables'] = snapshot(data)
        assert 'reports' not in state['legacy_tables'] and 'agent_grants' not in state['legacy_tables']
        assert state['legacy_tables']['passkeys']['rows']
        dump(work / 'fixture.json', state)
        return {'phase':'seed', 'legacy_tables':len(state['legacy_tables']), 'source_files':len(state['source_files'])}

    state = json.loads((work / 'fixture.json').read_text())
    assert installer_snapshot(work) == state['installation'], 'Installation identities or mounts changed'
    with TestClient(app, base_url=base) as owner:
        current = snapshot(data)
        expected = state['legacy_tables'] if mode == 'upgrade' else state['upgraded_tables']
        for name, table in expected.items():
            assert current[name] == table, 'Startup changed historical rows: '+name
        assert files(data) == state['source_files'], 'Startup changed retained source/result files'
        assert {'reports','report_versions','diarization_jobs','visual_jobs','analysis_jobs','agent_grants'} <= current.keys()
        restore_cookies(owner, state['sessions']['owner'])
        oh = session(owner)
        rid, pid = state['recording'], state['project']
        route = f'/api/recordings/{rid}'
        assert checked(owner.get(route+'/transcript')) == state['transcript']
        assert checked(owner.get(f'/api/recordings/{state["pending"]}'))['status'] == 'queued'
        assert checked(owner.get('/api/search', params={'project_id':pid,'q':'PostgreSQL'}))
        assert owner.get(route+'/media/audio.wav').content == wav()
        for role in ('editor', 'viewer', 'outsider'):
            with TestClient(app, base_url=base) as member:
                restore_cookies(member, state['sessions'][role])
                assert member.get(route).status_code == (404 if role == 'outsider' else 200)
                if role == 'viewer':
                    assert member.post(route+'/marks', json={}, headers=session(member)).status_code == 403
        if mode == 'upgrade':
            # The historical password and public passkey verify against the new source.
            login(owner)
            checked(owner.post('/api/auth/login', json={'email':'owner@example.com', 'password':PASSWORD,
                         'audience':'admin'}, headers=session(owner, 'admin')))
            options = checked(owner.post('/api/auth/mfa/authenticate/options', headers=admin_headers(owner)))
            checked(owner.post('/api/auth/mfa/authenticate/verify', json={
                'credential':authenticator().authentication(options)}, headers=admin_headers(owner)))
            assert owner.get('/api/admin/users', headers=admin_headers(owner)).status_code == 200
            oh = session(owner)
            evidence = checked(owner.get(route+'/evidence'))
            checked(owner.post(route+'/marks', json={
                'expected_revision':0, 'expected_source_sha256':evidence['source_sha256'],
                'mark':{'interval':{'start':0,'end':1}, 'label':'Upgrade mark','pinned':True}}, headers=oh), 201)
            checked(owner.post(route+'/speaker-overrides', json={
                'expected_revision':0, 'expected_source_sha256':evidence['source_sha256'],
                'override':{'interval':{'start':0,'end':1},'speaker':None}}, headers=oh))
            grant = checked(owner.post('/api/auth/agent-grants', json={
                'label':'Disposable upgrade check','project_ids':[pid],'derive_media':False,'expires_in_days':1}, headers=oh))
            bearer = {'Authorization':'Bearer '+grant['token']}
            assert owner.get('/api/agent/projects', headers=bearer).status_code == 200
            checked(owner.delete('/api/auth/agent-grants/'+grant['grant']['id'], headers=oh))
            assert owner.get('/api/agent/projects', headers=bearer).status_code == 401
            evidence = checked(owner.get(route+'/evidence'))
            sid = state['transcript']['segments'][0]['id']
            reference = {'recording_id':rid,'source_sha256':evidence['source_sha256'],
                         'interval':{'start':0,'end':1},'versions':evidence['versions']}
            draft = {'expected_revision':0,'title':'Upgrade report',
                     'generator':{'name':'Disposable compatibility fixture','version':'1'},'inspected_images':[],
                     'report':{'template_version':'argon-summary-v1','reference':reference,
                               'scope':{'start':0,'end':1},'language':'en','coverage':'complete','limitations':[],
                               'citations':[{'id':'speech','kind':'transcript','evidence_id':sid,
                                             'interval':{'start':0,'end':1}}],
                               'items':[{'id':'summary','kind':'summary','basis':'explicit',
                                         'text':'PostgreSQL deployment is ready.','citation_ids':['speech']}]}}
            report = checked(owner.post(route+'/reports', json=draft, headers=oh), 201)
            state['report'] = report['id']
            checked(owner.post(route+'/reports/'+report['id']+'/review', json={'expected_revision':1}, headers=oh))
            assert checked(owner.get(route+'/transcript')) == state['transcript']
            state['sessions']['owner'] = cookies(owner)
        else:
            assert checked(owner.get(route+'/marks'))['marks'][0]['label'] == 'Upgrade mark'
            assert checked(owner.get(route+'/speakers'))['overrides']
            assert checked(owner.get(route+'/reports/'+state['report']))['status'] == 'reviewed'
        archive = owner.get(route+'/reports/'+state['report']+'/export.zip')
        assert archive.status_code == 200
        import zipfile
        with zipfile.ZipFile(io.BytesIO(archive.content)) as bundle:
            manifest = json.loads(bundle.read('manifest.json'))
            for item in manifest['files']:
                payload = bundle.read(item['path'])
                assert len(payload) == item['bytes'] and hashlib.sha256(payload).hexdigest() == item['sha256']
        assert files(data) == state['source_files']
    if mode == 'upgrade':
        state['upgraded_tables'] = snapshot(data)
        dump(work / 'fixture.json', state)
    return {'phase':mode,'historical_rows_preserved':True,'files_preserved':True,
            'sessions_roles_transcript_search_media':True,'new_features_persisted':True,'export_checksums':True,
            'installer_identity_model_mounts_single_worker':True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', default=BASELINE)
    parser.add_argument('--evidence', type=Path)
    parser.add_argument('--phase', choices=('seed','upgrade','restart'))
    parser.add_argument('--source', type=Path)
    parser.add_argument('--work', type=Path)
    args = parser.parse_args()
    if args.phase:
        print(json.dumps(phase(args.source, args.work, args.phase)))
        return
    baseline = subprocess.check_output(['git','rev-parse','--verify','--end-of-options',args.baseline+'^{commit}'], cwd=ROOT, text=True).strip()
    archive = subprocess.check_output(['git','archive',baseline,'backend','recordings.py','scripts/worker_stack.py'], cwd=ROOT)
    with tempfile.TemporaryDirectory(prefix='argon-recordings-upgrade-') as directory:
        work = Path(directory); old = work/'old'; old.mkdir()
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            tar.extractall(old, filter='data')
        results = []
        for mode, source in [('seed',old),('upgrade',ROOT),('restart',ROOT)]:
            process = subprocess.run([sys.executable,str(Path(__file__).resolve()),'--phase',mode,
                         '--source',str(source),'--work',str(work)], capture_output=True,
                         text=True, timeout=90, env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1'})
            if process.returncode:
                raise RuntimeError(mode+' phase failed:\n'+process.stderr[-6000:])
            results.append(json.loads(process.stdout))
        result = {'baseline':baseline,'candidate':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
                  'baseline_archive_sha256':hashlib.sha256(archive).hexdigest(),
                  'source_sha256':{str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest()
                      for p in sorted([*ROOT.glob('backend/argon_recordings/*.py'),ROOT/'recordings.py',
                                       ROOT/'scripts/worker_stack.py',Path(__file__).resolve()])},
                  'runtime':{'python':sys.version,**{name:version(name) for name in ('fastapi','pydantic','httpx')}},
                  'checks':results,'passed':True,'installed_host':False,'real_inference':False,'deployment':False}
        if args.evidence:
            args.evidence.mkdir(parents=True,exist_ok=True)
            dump(args.evidence/'result.json',result)
        print(json.dumps({key:value for key,value in result.items() if key != 'source_sha256'},indent=2))


if __name__ == '__main__':
    main()
