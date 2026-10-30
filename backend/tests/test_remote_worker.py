"""Split-role contracts and real local TLS sockets; all credentials synthetic."""
import hashlib
import httpx
import json
import os
from pathlib import Path
import socket
import ssl
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
import recordings
from scripts.remote_stack import tls_bundle,transport_args
from scripts.worker_stack import api_args,worker_args
from argon_recordings.transport import Relay,tls_context


@pytest.fixture
def certificates(tmp_path):
    # Synthetic test-only keys live in a disposable pytest directory, not config.
    for name,purpose in [('server','serverAuth'),('worker','clientAuth'),('other','clientAuth')]:
        subprocess.run(['openssl','req','-x509','-newkey','ed25519','-nodes','-days','1',
                        '-keyout',str(tmp_path/(name+'.key')),'-out',str(tmp_path/(name+'.crt')),
                        '-subj','/CN='+name,'-addext','basicConstraints=critical,CA:FALSE',
                        '-addext','extendedKeyUsage='+purpose,
                        '-addext','subjectAltName=IP:127.0.0.1,DNS:localhost'],
                       check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    for role,name,peer in [('coordinator','server','worker'),('worker','worker','server')]:
        directory=tmp_path/role;directory.mkdir()
        for source,target in [(name+'.crt','self.crt'),(name+'.key','self.key'),(peer+'.crt','peer.crt')]:
            (directory/target).write_bytes((tmp_path/source).read_bytes());(directory/target).chmod(0o600)
    yield tmp_path


class Upstream(BaseHTTPRequestHandler):
    def log_message(self,*_):pass
    def do_POST(self):
        body=bytearray()
        if self.headers.get('Transfer-Encoding')=='chunked':
            while size:=int(self.rfile.readline().strip(),16):
                body.extend(self.rfile.read(size));assert self.rfile.read(2)==b'\r\n'
            assert self.rfile.read(2)==b'\r\n'
        else:
            body.extend(self.rfile.read(int(self.headers.get('Content-Length','0'))))
        value=json.dumps({'path':self.path,'auth':self.headers.get('Authorization'),
                          'cookie':self.headers.get('Cookie'),'lease':self.headers.get('X-Lease-Id'),
                          'bytes':len(body),'sha256':hashlib.sha256(body).hexdigest()}).encode()
        self.send_response(200);self.send_header('Content-Length',str(len(value)));self.end_headers();self.wfile.write(value)
    do_PUT=do_POST
    def do_GET(self):
        value=b'synthetic-audio'*10000
        self.send_response(200);self.send_header('Content-Length',str(len(value)));self.end_headers();self.wfile.write(value)


@pytest.fixture
def tls_relay(certificates):
    upstream=ThreadingHTTPServer(('127.0.0.1',0),Upstream)
    context,pin=tls_context(certificates/'coordinator',server=True)
    credential=certificates/'credential.json';credential.write_text(json.dumps({'id':'worker-1','token':'synthetic-secret-'+'x'*40}))
    relay=Relay(('127.0.0.1',0),upstream=f'http://127.0.0.1:{upstream.server_port}',tls=context,peer_fingerprint=pin,credential=credential)
    threads=[threading.Thread(target=s.serve_forever,daemon=True) for s in [upstream,relay]]
    for t in threads:t.start()
    yield relay,credential
    for s in [relay,upstream]:s.shutdown();s.server_close()
    for t in threads:t.join(timeout=3)


def test_tls_worker_only_routes_and_rotating_bearer(certificates,tls_relay):
    relay,credential=tls_relay
    context,_=tls_context(certificates/'worker')
    with httpx.Client(verify=context,trust_env=False) as client:
        origin=f'https://127.0.0.1:{relay.server_port}'
        result=client.post(origin+'/api/worker/ready',headers={'Authorization':'Bearer spoof','Cookie':'recordings_session=spoof','X-Lease-Id':'synthetic-lease'}).json()
        assert result['auth'].startswith('Bearer synthetic-secret-') and result['cookie'] is None and result['lease']=='synthetic-lease'
        credential.write_text(json.dumps({'id':'worker-1','token':'new-synthetic-'+'z'*40}))
        assert client.post(origin+'/api/worker/ready').json()['auth'].startswith('Bearer new-synthetic-')
        for path in ['/api/admin/users','/api/auth/login','/api/worker/../admin/users','/api/worker/ready?redirect=x','/api/worker/%2e%2e/admin']:
            assert client.post(origin+path).status_code==404
        response=client.get(origin+'/api/worker/jobs/synthetic/source')
        assert response.content==b'synthetic-audio'*10000
    # A trusted but wrong client leaf must not be usable.
    wrong=ssl.create_default_context(cafile=str(certificates/'server.crt'))
    wrong.load_cert_chain(str(certificates/'other.crt'),str(certificates/'other.key'))
    with httpx.Client(verify=wrong,trust_env=False) as client:
        with pytest.raises(httpx.TransportError):client.post(origin+'/api/worker/ready')
    no_cert=ssl.create_default_context(cafile=str(certificates/'server.crt'))
    with httpx.Client(verify=no_cert,trust_env=False) as client:
        with pytest.raises(httpx.TransportError):client.post(origin+'/api/worker/ready')


def test_media_side_relay_only_accepts_internal_peers(certificates,tls_relay):
    remote,_=tls_relay
    context,_=tls_context(certificates/'worker')
    relay=Relay(('127.0.0.1',0),upstream=f'https://127.0.0.1:{remote.server_port}',client_tls=context,subnets=['127.0.0.1/32'])
    thread=threading.Thread(target=relay.serve_forever,daemon=True);thread.start()
    try:
        with httpx.Client(trust_env=False) as client:
            result=client.post(f'http://127.0.0.1:{relay.server_port}/api/worker/ready')
            assert result.status_code==200
            relay.subnets=[]
            assert client.post(f'http://127.0.0.1:{relay.server_port}/api/worker/ready').status_code==403
    finally:relay.shutdown();relay.server_close();thread.join(timeout=3)


def config(tmp_path,role,extra=''):
    path=tmp_path/'recordings.toml'
    path.write_text(f'[deployment]\nmode="{role}"\n[transport]\ncertificate="self.crt"\nprivate_key="self.key"\npeer_certificate="peer.crt"\n'+extra)
    return recordings.load_config(path)


def test_role_config_resources_and_installation_identity(tmp_path):
    worker=config(tmp_path,'worker','origin="https://vesta.example:8788"\n')
    assert worker['workers']==1 and worker['cpus']==2.5
    relay=transport_args(dict(worker,_worker_subnets='172.20.0.0/24'),{'image':'test'})
    assert '-p' not in relay and not any(':/data' in value for value in relay)
    coordinator=config(tmp_path,'coordinator')
    assert worker['identity']==coordinator['identity']
    assert worker['turbo']==coordinator['turbo']
    assert '127.0.0.1:8788:8443' in transport_args(coordinator,{'image':'test'},'172.20.0.2')


@pytest.mark.parametrize('role,extra',[
    ('unknown',''),('worker','origin="http://vesta"'),('worker','origin="https://user:pass@vesta"'),
    ('worker','origin="https://vesta/api"'),('worker','origin="https://vesta"\nport=8788'),
    ('coordinator','bind_address="0.0.0.0"'),('coordinator','bind_address="8.8.8.8"'),
    ('coordinator','port=true'),('coordinator','\n[workers]\ncount=2'),
])
def test_invalid_role_settings_fail_before_docker(tmp_path,role,extra):
    with pytest.raises(ValueError):config(tmp_path,role,extra)


def test_private_key_permissions_and_symlinks_rejected(certificates):
    cfg=config(certificates/'coordinator','coordinator')
    assert set(tls_bundle(cfg))=={'self.crt','self.key','peer.crt'}
    key=certificates/'coordinator'/'self.key';key.chmod(0o644)
    with pytest.raises(ValueError,match='mode0600'):tls_bundle(cfg)
    key.unlink();key.symlink_to(certificates/'server.key')
    with pytest.raises(ValueError,match='symlinks'):tls_bundle(cfg)


def test_streaming_upload_and_prepare_timeout(certificates,tls_relay):
    from unittest.mock import patch
    relay,_=tls_relay
    context,_=tls_context(certificates/'worker')
    payload=b'a'*2*1024**2
    with httpx.Client(verify=context,trust_env=False) as client:
        origin=f'https://127.0.0.1:{relay.server_port}'
        for content in [payload,iter([payload[:100],payload[100:]])]:
            result=client.put(origin+'/api/worker/jobs/synthetic/artifacts/audio.wav',content=content)
            # The synthetic upstream serves the same body collector for PUT.
            assert result.status_code==200,result.text
            assert result.json()['bytes']==len(payload)
            assert result.json()['sha256']==hashlib.sha256(payload).hexdigest()
        with patch.object(relay.client,'stream',wraps=relay.client.stream) as stream:
            assert client.post(origin+'/api/worker/jobs/synthetic/prepare',json={}).status_code==200
            assert stream.call_args.kwargs['timeout'].read==600
    with socket.create_connection(('127.0.0.1',relay.server_port),timeout=3) as raw:
        with context.wrap_socket(raw,server_hostname='127.0.0.1') as connection:
            connection.sendall(b'POST /api/worker/ready HTTP/1.1\r\nHost: localhost\r\nContent-Length: 1\r\nContent-Length: 2\r\n\r\nx')
            assert b'400' in connection.recv(1024).split(b'\r\n')[0]
    with socket.create_connection(('127.0.0.1',relay.server_port),timeout=3) as raw:
        with pytest.raises(ssl.SSLCertVerificationError):
            context.wrap_socket(raw,server_hostname='wrong-host.invalid')


def test_reboot_transport_mounts_host_tls_only_and_api_uses_remote_inference(tmp_path):
    cfg=config(tmp_path,'coordinator')
    args=transport_args(cfg,{'image':'test'},'172.20.0.2')
    assert args[args.index('--user')+1]==str(os.geteuid())+':10001'
    for name in ('self.crt','self.key','peer.crt'):
        assert any('dst=/run/transport-tls/'+name+',readonly' in arg for arg in args)
    assert not any('credentials-transport' in arg for arg in args)
    api=api_args(dict(cfg,_worker_subnets='172.20.0.0/24'),{'image':'test'})
    assert 'RECORDINGS_REMOTE_EMBEDDINGS=true' in api
    assert not any(':/models/embeddings' in arg for arg in api)


def test_transport_runtime_rejects_readable_key_after_restart(certificates):
    key=certificates/'worker'/'self.key'
    key.chmod(0o644)
    with pytest.raises(ValueError,match='private regular file'):
        tls_context(certificates/'worker')


def test_ram_bearer_permissions_are_group_readable_only_for_split_coordinator(tmp_path,monkeypatch):
    from argon_recordings import isolated
    monkeypatch.setattr(isolated,'Path',lambda _:tmp_path)
    folder=tmp_path/'1';folder.mkdir(mode=0o700)
    monkeypatch.delenv('RECORDINGS_REMOTE_EMBEDDINGS',raising=False)
    monkeypatch.delenv('RECORDINGS_TRANSPORT_GROUP',raising=False)
    isolated.write_credentials(1,{'id':'synthetic'})
    assert folder.stat().st_mode & 0o777 == 0o700
    assert (folder/'credential.json').stat().st_mode & 0o777 == 0o600
    monkeypatch.setenv('RECORDINGS_TRANSPORT_GROUP','true')
    isolated.write_credentials(1,{'id':'synthetic-new'})
    assert folder.stat().st_mode & 0o777 == 0o750
    assert (folder/'credential.json').stat().st_mode & 0o777 == 0o640
    assert json.loads((folder/'credential.json').read_text())['id']=='synthetic-new'
