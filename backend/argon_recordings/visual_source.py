"""Seekable capability proxy for one authenticated, integrity-verified source.

FFmpeg sees only a random loopback URL; worker credentials stay in this process.
All upstream requests use the existing fixed-origin client and active job lease.
"""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
import re
import secrets
import threading


@contextmanager
def ranged_source(client,base,headers,task,lost,progress):
    size=task['source_bytes'];sha=task['source_sha256']
    endpoint=base+'/source-verified'
    # Verify the canonical source locally on the coordinator before decoding.
    with client.stream('GET',endpoint,follow_redirects=False,timeout=180,headers={**headers,'Range':'bytes=0-0'}) as response:
        if response.status_code!=206 or response.headers.get('x-source-sha256')!=sha or response.headers.get('content-range')!=f'bytes 0-0/{size}':
            raise RuntimeError('Coordinator does not provide verified seekable source')
        if sum(len(block) for block in response.iter_bytes())!=1:raise RuntimeError('Invalid source verification response')
    token='/'+secrets.token_hex(24)
    budget=128*1024**2 if task['recipe']['kind']=='image' else 256*1024**2
    used=0;requests=0;lock=threading.Lock();control=secrets.token_hex(24);guards={}
    class Handler(BaseHTTPRequestHandler):
        protocol_version='HTTP/1.1'
        def log_message(self,*args):pass
        def reply(self,status):
            self.close_connection=True
            self.send_response(status);self.send_header('Content-Length','0');self.send_header('Connection','close');self.end_headers()
        def do_POST(self):
            if self.path!=token+'/r' or self.headers.get('X-Guard-Control')!=control:return self.reply(403)
            try:
                length=int(self.headers.get('Content-Length','0'))
                if not 0<length<=512:raise ValueError()
                value=json.loads(self.rfile.read(length));nonce=value['nonce'];proof=value['proof']
                if set(value)!={'nonce','proof'} or not re.fullmatch('[a-f0-9]{48}',nonce) or not re.fullmatch('[a-f0-9]{48}',proof):raise ValueError()
                with lock:
                    if len(guards)>=64:raise ValueError()
                    guards[nonce]={'proof':proof,'ready':False}
            except (ValueError,KeyError,TypeError):return self.reply(400)
            self.reply(204)
        def authorized(self):
            parts=self.path.split('/')
            if len(parts)!=3 or '/'+parts[1]!=token:return False
            with lock:return bool(guards.get(parts[2],{}).get('ready'))
        def do_HEAD(self):
            if not self.authorized() or lost.is_set():return self.reply(403)
            self.send_response(200);self.send_header('Content-Length',str(size));self.send_header('Accept-Ranges','bytes');self.end_headers()
        def do_GET(self):
            nonlocal used,requests
            self.close_connection=True
            parts=self.path.split('/')
            if len(parts)==5 and '/'+parts[1]==token and parts[2]=='a':
                with lock:
                    guard=guards.get(parts[3])
                    if guard and secrets.compare_digest(guard['proof'],parts[4]):
                        guard['ready']=True;return self.reply(204)
                return self.reply(403)
            if not self.authorized() or lost.is_set():return self.reply(403)
            requested=self.headers.get('Range','bytes=0-')
            match=re.fullmatch(r'bytes=(\d+)-(\d*)',requested)
            if not match:self.send_error(416);return
            start=int(match[1]);end=int(match[2]) if match[2] else size-1
            if not 0<=start<=end<size:self.send_error(416);return
            with lock:
                requests+=1
                if requests>128:lost.set();self.send_error(429);return
            started=False
            try:
                self.send_response(206)
                self.send_header('Content-Length',str(end-start+1));self.send_header('Content-Range',f'bytes {start}-{end}/{size}')
                self.send_header('Accept-Ranges','bytes');self.send_header('Connection','close');self.end_headers();started=True
                position=start
                while position<=end and not lost.is_set():
                    stop=min(end,position+1024**2-1)
                    with client.stream('GET',endpoint,follow_redirects=False,headers={**headers,'Range':f'bytes={position}-{stop}'}) as response:
                        expected=f'bytes {position}-{stop}/{size}'
                        if response.status_code!=206 or response.headers.get('x-source-sha256')!=sha or response.headers.get('content-range')!=expected:
                            raise RuntimeError('Source range verification failed')
                        received=0
                        for block in response.iter_bytes(65536):
                            if lost.is_set():break
                            received+=len(block)
                            if received>stop-position+1:raise RuntimeError('Oversized source response')
                            with lock:
                                used+=len(block)
                                if used>budget:lost.set();raise RuntimeError('Source range budget exceeded')
                                progress['source_bytes']=used
                            self.wfile.write(block)
                        if not lost.is_set() and received!=stop-position+1:raise RuntimeError('Truncated source range')
                    position=stop+1
            except (BrokenPipeError,ConnectionResetError):pass  # A seek closes the previous range.
            except Exception:
                lost.set()
                if not started:self.send_error(502)
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    server.daemon_threads=True
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:yield {'url':f'http://127.0.0.1:{server.server_port}{token}','control':control}
    finally:
        server.shutdown();server.server_close();thread.join(timeout=5)
