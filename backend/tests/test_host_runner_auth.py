"""Ordinary inference credentials cannot impersonate a host update runner."""
from datetime import datetime,timedelta,timezone

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes,serialization
from cryptography.hazmat.primitives.asymmetric import ec,rsa,ed25519
from cryptography.x509.oid import NameOID,ExtendedKeyUsageOID
from fastapi import HTTPException
from argon_recordings import host_runner_auth as auth


def credentials(root,key=None):
    key=key or ec.generate_private_key(ec.SECP256R1())
    name=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'synthetic-host')])
    now=datetime.now(timezone.utc)
    cert=(x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
          .serial_number(x509.random_serial_number()).not_valid_before(now-timedelta(days=1)).not_valid_after(now+timedelta(days=1))
          .add_extension(x509.BasicConstraints(ca=False,path_length=None),critical=True)
          .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]),critical=False)
          .sign(key,None if isinstance(key,ed25519.Ed25519PrivateKey) else hashes.SHA256()))
    private=root/'host.key';public=root/'host.crt'
    private.write_bytes(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
    private.chmod(0o600);public.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return private,public


@pytest.fixture
def runner_credentials(tmp_path,monkeypatch):
    private,public=credentials(tmp_path)
    monkeypatch.setattr(auth,'PEER_CERTIFICATE',public)
    return private


@pytest.mark.parametrize('kind',['rsa','ec','ed25519'])
def test_host_transport_key_signatures_are_verifiable(tmp_path,monkeypatch,kind):
    key=rsa.generate_private_key(public_exponent=65537,key_size=2048) if kind=='rsa' else ed25519.Ed25519PrivateKey.generate() if kind=='ed25519' else ec.generate_private_key(ec.SECP256R1())
    private,public=credentials(tmp_path,key);monkeypatch.setattr(auth,'PEER_CERTIFICATE',public)
    body={'revision':'a'*40,'result_id':None,'result':None}
    proof=auth.sign('worker-1',body,private)
    assert auth.verify('worker-1',body,proof)==proof.nonce
    with pytest.raises(HTTPException):auth.verify('worker-2',body,proof)
    with pytest.raises(HTTPException):auth.verify('worker-1',{**body,'revision':'b'*40},proof)
    with pytest.raises(HTTPException):auth.verify('worker-1',{**body,'result_id':'c'*32,'result':'completed'},proof)


def test_missing_expired_and_wrong_certificate_proofs_are_rejected(runner_credentials,tmp_path,monkeypatch):
    body={'revision':'a'*40,'result_id':None,'result':None}
    with pytest.raises(HTTPException):auth.verify('worker-1',body,None)
    clock=auth.time.time()
    with monkeypatch.context() as scoped:
        scoped.setattr(auth.time,'time',lambda:clock-300)
        old=auth.sign('worker-1',body,runner_credentials)
    with pytest.raises(HTTPException):auth.verify('worker-1',body,old)
    proof=auth.sign('worker-1',body,runner_credentials)
    other=tmp_path/'other';other.mkdir();_,public=credentials(other)
    monkeypatch.setattr(auth,'PEER_CERTIFICATE',public)
    with pytest.raises(HTTPException):auth.verify('worker-1',body,proof)
