"""Host-owned update proofs, separate from ordinary inference credentials.

Reuse the existing transport private key only inside the host-exec bridge.
The coordinator needs only the pinned public peer certificate, never that key.
"""
import base64
from datetime import datetime, timezone
import json
from pathlib import Path
import secrets
import time

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa, ec, ed25519, ed448, padding
from cryptography.x509.oid import ExtendedKeyUsageOID
from fastapi import HTTPException
from pydantic import Field
from .contracts import Strict

PEER_CERTIFICATE=Path('/run/host-runner/peer.crt')

class HostProof(Strict):
    issued_at: int = Field(ge=0)
    nonce: str = Field(pattern=r'^[a-f0-9]{32}$')
    signature: str = Field(pattern=r'^[A-Za-z0-9_-]{40,4096}$')


def signing_bytes(target,body,issued_at,nonce):
    prefix=f'argon-recordings-host-runner-v1\nPOST /api/worker/management\n{target}\n{issued_at}\n{nonce}\n'
    return prefix.encode()+json.dumps(body,sort_keys=True,separators=(',',':')).encode()


def sign(target,body,key_path):
    issued=int(time.time());nonce=secrets.token_hex(16)
    key=serialization.load_pem_private_key(Path(key_path).read_bytes(),password=None)
    payload=signing_bytes(target,body,issued,nonce)
    if isinstance(key,rsa.RSAPrivateKey):signature=key.sign(payload,padding.PKCS1v15(),hashes.SHA256())
    elif isinstance(key,ec.EllipticCurvePrivateKey):signature=key.sign(payload,ec.ECDSA(hashes.SHA256()))
    elif isinstance(key,(ed25519.Ed25519PrivateKey,ed448.Ed448PrivateKey)):signature=key.sign(payload)
    else:raise ValueError('Unsupported host transport signing key')
    return HostProof(issued_at=issued,nonce=nonce,signature=base64.urlsafe_b64encode(signature).decode().rstrip('='))


def verify(target,body,proof):
    try:
        if proof is None or abs(time.time()-proof.issued_at)>120:raise ValueError('Expired proof')
        certificate=x509.load_pem_x509_certificate(PEER_CERTIFICATE.read_bytes())
        if certificate.subject!=certificate.issuer or certificate.extensions.get_extension_for_class(x509.BasicConstraints).value.ca:
            raise ValueError('Expected pinned leaf certificate')
        if ExtendedKeyUsageOID.CLIENT_AUTH not in certificate.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value:
            raise ValueError('Expected worker certificate')
        now=datetime.now(timezone.utc)
        if not certificate.not_valid_before_utc<=now<=certificate.not_valid_after_utc:raise ValueError('Expired certificate')
        signature=base64.urlsafe_b64decode(proof.signature+'='*(-len(proof.signature)%4))
        payload=signing_bytes(target,body,proof.issued_at,proof.nonce);key=certificate.public_key()
        if isinstance(key,rsa.RSAPublicKey):key.verify(signature,payload,padding.PKCS1v15(),hashes.SHA256())
        elif isinstance(key,ec.EllipticCurvePublicKey):key.verify(signature,payload,ec.ECDSA(hashes.SHA256()))
        elif isinstance(key,(ed25519.Ed25519PublicKey,ed448.Ed448PublicKey)):key.verify(signature,payload)
        else:raise ValueError('Unsupported signing key')
    except (OSError,ValueError,TypeError,InvalidSignature,x509.ExtensionNotFound):
        raise HTTPException(403,'A fresh proof from the host update runner is required') from None
    return proof.nonce
