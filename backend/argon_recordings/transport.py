"""Fixed-upstream, worker-only streaming transport. Never mounts media or accounts.

Coordinator: pinned mutual TLS -> internal API with a RAM-only worker bearer.
Worker host: internal media network -> fixed pinned HTTPS coordinator.
"""
import hashlib
import ipaddress
import json
import logging
import os
from pathlib import Path
import re
import socket
import ssl
import stat
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import httpx

LIMIT = 4 * 1024**3



def tls_context(folder, server=False):
    folder = Path(folder)
    key = (folder / 'self.key').lstat()
    if not stat.S_ISREG(key.st_mode) or key.st_uid != os.geteuid() or key.st_mode & 0o077:
        raise ValueError('TLS key must be a private regular file owned by the transport user')
    peer = (folder / 'peer.crt').read_text()
    if peer.count('-----BEGIN CERTIFICATE-----') != 1:
        raise ValueError('Exactly one peer certificate is required')
    from cryptography import x509
    from cryptography.x509.oid import ExtendedKeyUsageOID
    certificate = x509.load_pem_x509_certificate(peer.encode())
    if certificate.subject != certificate.issuer or certificate.extensions.get_extension_for_class(x509.BasicConstraints).value.ca:
        raise ValueError('Peer must be a dedicated self-signed leaf certificate')
    purpose = ExtendedKeyUsageOID.CLIENT_AUTH if server else ExtendedKeyUsageOID.SERVER_AUTH
    if purpose not in certificate.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value:
        raise ValueError('Peer certificate has the wrong TLS purpose')
    fingerprint = hashlib.sha256(ssl.PEM_cert_to_DER_cert(peer)).digest()
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER) if server else ssl.create_default_context(cafile=str(folder/'peer.crt'))
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.load_cert_chain(str(folder/'self.crt'), str(folder/'self.key'), password=lambda: '')
    if server:
        context.load_verify_locations(cafile=str(folder/'peer.crt'))
        context.verify_mode = ssl.CERT_REQUIRED
    return context, fingerprint


class Relay(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, *, upstream, tls=None, peer_fingerprint=None, subnets=(), credential=None, client_tls=None):
        self.upstream = upstream.rstrip('/')
        self.peer_fingerprint = peer_fingerprint
        self.subnets = [ipaddress.ip_network(value) for value in subnets]
        self.credential = credential
        self.slots = threading.BoundedSemaphore(8)
        self.client = httpx.Client(verify=client_tls or True, trust_env=False,
                                  follow_redirects=False, timeout=httpx.Timeout(60, connect=10),
                                  limits=httpx.Limits(max_connections=8))
        super().__init__(address, Handler)
        if tls:
            # Handshake in the bounded handler thread, not the accept loop.
            self.tls = tls
        else:
            self.tls = None

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(60)
        return connection, address

    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            request.close()
            return
        try:
            super().process_request(request, address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, address):
        try:
            if self.tls:
                request = self.tls.wrap_socket(request, server_side=True)
            super().process_request_thread(request, address)
        except (OSError, ssl.SSLError):
            request.close()
        finally:
            self.slots.release()

    def handle_error(self, *_):
        # Never log request bodies, paths, headers or credentials.
        logging.warning('Worker transport connection failed')

    def server_close(self):
        super().server_close()
        self.client.close()


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *_):
        pass

    def reply(self, code):
        self.close_connection = True
        self.send_response(code)
        self.send_header('Content-Length', '0')
        self.send_header('Connection', 'close')
        self.end_headers()

    def body(self, size, chunked):
        total = 0
        remaining = size
        while chunked or remaining:
            if chunked:
                line = self.rfile.readline(128)
                if not re.fullmatch(rb'[0-9a-fA-F]{1,16}\r\n', line):
                    raise ValueError('Invalid chunk framing')
                remaining = int(line[:-2],16)
                if remaining == 0:
                    if self.rfile.readline(3) != b'\r\n':
                        raise ValueError('Trailers are not accepted')
                    return
            while remaining:
                block = self.rfile.read(min(remaining, 65536))
                if not block:
                    raise ValueError('Truncated body')
                remaining -= len(block)
                total += len(block)
                if total > LIMIT:
                    raise ValueError('Body exceeds transport budget')
                yield block
            if chunked and self.rfile.read(2) != b'\r\n':
                raise ValueError('Invalid chunk delimiter')

    def relay(self):
        self.close_connection = True
        parsed = urlsplit(self.path)
        # No absolute URLs, encoded/traversing paths, queries or browser routes.
        if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment or not re.fullmatch(r'/api/worker/[A-Za-z0-9_./-]+', parsed.path) or any(part in {'.','..'} for part in parsed.path.split('/')):
            return self.reply(404)
        if self.command not in {'GET','POST','PUT'}:
            return self.reply(405)
        server = self.server
        if server.peer_fingerprint:
            cert = self.connection.getpeercert(binary_form=True)
            if not cert or hashlib.sha256(cert).digest() != server.peer_fingerprint:
                return self.reply(403)
        elif not any(ipaddress.ip_address(self.client_address[0]) in net for net in server.subnets):
            return self.reply(403)
        lengths = self.headers.get_all('Content-Length', [])
        encodings = self.headers.get_all('Transfer-Encoding', [])
        if len(lengths)>1 or len(encodings)>1 or lengths and encodings or encodings and encodings[0].lower()!='chunked':
            return self.reply(400)
        if lengths and not re.fullmatch(r'[0-9]{1,12}', lengths[0]):
            return self.reply(400)
        size = int(lengths[0]) if lengths else 0
        if size > LIMIT:
            return self.reply(413)
        # Header allowlist avoids proxy/cookie/identity smuggling in either role.
        headers = {key:value for key,value in self.headers.items()
                   if key.lower() in {'content-type','x-lease-id','range'}}
        headers['Accept-Encoding'] = 'identity'
        if lengths:
            headers['Content-Length'] = str(size)
        if server.credential:
            value = json.loads(Path(server.credential).read_text())
            if value['id'] != 'worker-1' or len(value['token'])<40:
                return self.reply(503)
            headers['Authorization'] = 'Bearer ' + value['token']
        started = False
        try:
            content = self.body(size,bool(encodings)) if size or encodings else None
            timeout = httpx.Timeout(600 if self.command=='POST' and re.fullmatch(r'/api/worker/jobs/[^/]+/prepare',parsed.path) else 60, connect=10)
            with server.client.stream(self.command, server.upstream+self.path, headers=headers, content=content, timeout=timeout) as response:
                # Never follow or expose a redirect to another origin.
                if 300 <= response.status_code < 400:
                    return self.reply(502)
                self.send_response(response.status_code)
                for key,value in response.headers.items():
                    if key.lower() in {'content-type','content-length','content-range','accept-ranges'}:
                        self.send_header(key,value)
                self.send_header('Connection','close')
                self.end_headers()
                started = True
                for block in response.iter_raw():
                    self.wfile.write(block)
        except (OSError, ValueError, httpx.HTTPError):
            if not started:
                self.reply(502)

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = relay


def main():
    mode = os.environ['RECORDINGS_TRANSPORT_MODE']
    context, fingerprint = tls_context('/run/transport-tls', server=mode=='coordinator')
    if mode == 'coordinator':
        server = Relay(('0.0.0.0',8443), upstream='http://api:8001', tls=context,
                       peer_fingerprint=fingerprint, credential='/run/worker-credentials/self/credential.json')
    elif mode == 'worker':
        server = Relay(('0.0.0.0',8001), upstream=os.environ['RECORDINGS_COORDINATOR_ORIGIN'],
                       client_tls=context, subnets=os.environ['RECORDINGS_WORKER_SUBNETS'].split(','))
    else:
        raise ValueError('Unknown transport role')
    Path('/tmp/transport-ready').touch()
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
