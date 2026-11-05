"""Explicitly opt-in host runner for admin updates; no listener, secrets or SSH.

Run as the installation owner, with the same Docker access as recordings.py.
Uses the existing backup/fast-forward updater, and survives API restarts.
"""
import fcntl
import json
import os
import subprocess
import sys
import time


def revision(installer):
    from scripts.git_updates import git
    result=git(installer.ROOT,'rev-parse','HEAD',check=False)
    return result.stdout.strip() if result.returncode==0 else 'unknown'


def check_update(installer,config,request_id=0):
    from scripts.git_updates import checked_checkout,git
    value={'state':'failed','latest_revision':None,'checked_at':time.time(),'request_id':request_id}
    try:
        root=checked_checkout(installer.ROOT)
        latest=git(root,'ls-remote','--exit-code','origin','refs/heads/main').stdout.split()[0]
        import re
        if not re.fullmatch(r'[a-f0-9]{40,64}',latest):raise ValueError('Invalid release revision')
        value['latest_revision']=latest
        current=revision(installer)
        # A matching checkout alone does not prove the installed image is current.
        if installer.state(config)['source']!=installer.source_id():raise ValueError('Runtime/source mismatch')
        value['state']='up_to_date' if current==latest else 'available'
    except (OSError,ValueError,IndexError,subprocess.SubprocessError):pass
    return value


def bridge(installer,config,body):
    item=installer.inspect(config)
    if not item or not item['State']['Running']:raise ValueError('Installation container is not running')
    remote=config['mode']=='worker'
    # In split worker mode the primary is the transport, which proxies via mTLS.
    role='remote-worker' if remote else 'app'
    result=installer.docker(config,'exec','-i',config['name'],'python','-m',
        'argon_recordings.management_bridge',role,input=json.dumps(body),capture_output=True,text=True,timeout=60)
    return json.loads(result.stdout)


def run(installer,config,once=False):
    folder=config['home']/'.recordings'/'admin-runner'
    folder.mkdir(parents=True,exist_ok=True,mode=0o700)
    # State lives outside replaced containers; never replay a claimed update.
    with (folder/'runner.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        receipt=folder/'receipt.json'
        pending=json.loads(receipt.read_text()) if receipt.exists() else None
        cached=None;requested=0
        while True:
            try:
                if cached is None or time.time()-cached['checked_at']>=300 or requested>cached['request_id']:
                    cached=check_update(installer,config,requested)
                body={'revision':revision(installer),'check':cached}
                if pending:body.update(result_id=pending['id'],result=pending['result'])
                response=bridge(installer,config,body)
                requested=response.get('check_requested',0)
                if pending:receipt.unlink(missing_ok=True);pending=None
                jid=response['update_id']
                if jid:
                    pending={'id':jid,'result':'failed'}
                    temporary=receipt.with_suffix('.tmp');temporary.write_text(json.dumps(pending));temporary.replace(receipt)
                    # Use a separate host process: the containers it replaces may stop.
                    command=[sys.executable,str(installer.ROOT/'recordings.py'),'--config',str(config['config_path']),'update']
                    with (folder/'update.log').open('w') as log:
                        os.chmod(folder/'update.log',0o600)
                        result=subprocess.run(command,stdout=log,stderr=subprocess.STDOUT)
                    cached=None
                    pending['result']='completed' if result.returncode==0 else 'failed'
                    temporary.write_text(json.dumps(pending));temporary.replace(receipt)
                    # Best effort immediately, then retain receipt for the next poll.
                    bridge(installer,config,{'revision':revision(installer),'result_id':jid,'result':pending['result']})
                    receipt.unlink(missing_ok=True);pending=None
                    if result.returncode==0 and not once:
                        # Reload runner code only after acknowledging the durable result.
                        # Python's non-inheritable lock fd closes across exec.
                        os.execv(sys.executable,[sys.executable,str(installer.ROOT/'recordings.py'),
                                 '--config',str(config['config_path']),'admin-runner'])
                if once:return
            except (OSError,ValueError,KeyError,subprocess.SubprocessError) as exc:
                print('Admin runner could not reconcile host state ('+type(exc).__name__+'); retained receipts will be retried.',flush=True)
                if once:raise
            time.sleep(10)
