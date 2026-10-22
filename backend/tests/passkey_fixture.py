"""Synthetic ES256 authenticator producing actual WebAuthn signed responses."""
import hashlib
import json
import secrets
import struct

import cbor2
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url


class Authenticator:
    def __init__(self):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.id = secrets.token_bytes(32)
        self.count = 0

    def client(self, options, kind, origin):
        return json.dumps({'type': kind, 'challenge': options['challenge'], 'origin': origin, 'crossOrigin': False}).encode()

    def registration(self, options, origin='http://localhost:8787', uv=True):
        public = self.key.public_key().public_numbers()
        cose = cbor2.dumps({1: 2, 3: -7, -1: 1, -2: public.x.to_bytes(32, 'big'), -3: public.y.to_bytes(32, 'big')})
        data = hashlib.sha256(options['rp']['id'].encode()).digest() + bytes([0x45 if uv else 0x41]) + struct.pack('>I', 0)
        data += b'\0' * 16 + struct.pack('>H', len(self.id)) + self.id + cose
        return {'id': bytes_to_base64url(self.id), 'rawId': bytes_to_base64url(self.id), 'type': 'public-key',
                'response': {'clientDataJSON': bytes_to_base64url(self.client(options, 'webauthn.create', origin)),
                             'attestationObject': bytes_to_base64url(cbor2.dumps({'fmt': 'none', 'attStmt': {}, 'authData': data}))}}

    def authentication(self, options, origin='http://localhost:8787', uv=True):
        self.count += 1
        client = self.client(options, 'webauthn.get', origin)
        data = hashlib.sha256(options['rpId'].encode()).digest() + bytes([5 if uv else 1]) + struct.pack('>I', self.count)
        signature = self.key.sign(data + hashlib.sha256(client).digest(), ec.ECDSA(hashes.SHA256()))
        return {'id': bytes_to_base64url(self.id), 'rawId': bytes_to_base64url(self.id), 'type': 'public-key',
                'response': {'clientDataJSON': bytes_to_base64url(client), 'authenticatorData': bytes_to_base64url(data),
                             'signature': bytes_to_base64url(signature), 'userHandle': None}}


def headers(client):
    return {'X-CSRF-Token': client.get('/api/auth/session?audience=admin').json()['csrf'], 'X-Session-Audience': 'admin'}


def enroll(client, manager, address='owner@example.com', authenticator=None):
    from argon_recordings.passkeys import enrollment_ticket
    key = authenticator or Authenticator()
    code = enrollment_ticket(manager, address)
    options = client.post('/api/auth/mfa/register/options', json={'code': code}, headers=headers(client))
    assert options.status_code == 200, options.text
    origin = str(client.base_url).rstrip('/')
    result = client.post('/api/auth/mfa/register/verify', json={'credential': key.registration(options.json(), origin)}, headers=headers(client))
    assert result.status_code == 200, result.text
    return key, result.json()['recovery_codes']
