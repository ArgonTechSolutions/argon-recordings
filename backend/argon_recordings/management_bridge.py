"""Host-runner bridge invoked using docker exec; never accepts shell commands."""
import json
import os
from pathlib import Path
import sqlite3
import sys
import httpx
from .admin_system import HostMessage, exchange


def main():
    body=HostMessage.model_validate(json.load(sys.stdin))
    if sys.argv[1:]==['app']:
        path=Path(os.environ.get('RECORDINGS_DATA','/data'))/'recordings.sqlite'
        with sqlite3.connect(f'file:{path}?mode=rw',uri=True,timeout=30) as db:
            db.row_factory=sqlite3.Row
            value=exchange(db,'app',body)
    elif sys.argv[1:]==['remote-worker']:
        from .host_runner_auth import sign
        from .transport import tls_context
        context,_=tls_context('/run/transport-tls')
        payload=body.model_dump(exclude={'proof'})
        payload['proof']=sign('worker-1',payload,'/run/transport-tls/self.key').model_dump()
        # Bypass the worker-facing relay: only host docker-exec can use this key.
        with httpx.Client(base_url=os.environ['RECORDINGS_COORDINATOR_ORIGIN'],verify=context,timeout=30,trust_env=False) as client:
            response=client.post('/api/worker/management',json=payload);response.raise_for_status();value=response.json()
    else:raise ValueError('Unknown host role')
    print(json.dumps(value))

if __name__=='__main__':main()
