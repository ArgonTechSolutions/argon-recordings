"""Run with pytest or directly inside the installed runtime image (no paid API)."""
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

import httpx

from argon_recordings.visual_runtime import network_guard
from argon_recordings.visual_source import ranged_source


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg required')
class SourceFenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory=tempfile.TemporaryDirectory()
        cls.root=Path(cls.directory.name)
        cls.guard=Path(os.environ.get('RECORDINGS_VISUAL_NET_GUARD','/app/lib/visual_netguard.so'))
        if not cls.guard.is_file():
            if not shutil.which('cc'):raise unittest.SkipTest('Packaged guard or compiler required')
            cls.guard=cls.root/'guard.so'
            subprocess.run(['cc','-shared','-fPIC','-O2',str(Path(__file__).resolve().parents[2]/'scripts/visual_netguard.c'),'-ldl','-o',str(cls.guard)],check=True)
        video=cls.root/'fixture.mp4'
        subprocess.run(['ffmpeg','-nostdin','-v','error','-f','lavfi','-i','color=red:size=64x64:duration=1','-c:v','libx264','-threads','1',str(video)],check=True,timeout=20)
        cls.data=video.read_bytes()

    @classmethod
    def tearDownClass(cls):cls.directory.cleanup()

    def setUp(self):
        self.ranges=[]
        self.sha=hashlib.sha256(self.data).hexdigest()
        def upstream(request):
            self.assertEqual(str(request.url),'https://coordinator/api/worker/visual/job/source-verified')
            self.assertEqual(request.headers['X-Lease-Id'],'synthetic-lease')
            match=re.fullmatch(r'bytes=(\d+)-(\d+)',request.headers['range'])
            start,end=map(int,match.groups());self.ranges.append((start,end))
            return httpx.Response(206,content=self.data[start:end+1],headers={
                'Content-Range':f'bytes {start}-{end}/{len(self.data)}','X-Source-Sha256':self.sha})
        self.client=httpx.Client(transport=httpx.MockTransport(upstream))
        self.addCleanup(self.client.close)
        self.task={'source_bytes':len(self.data),'source_sha256':self.sha,'recipe':{'kind':'image'}}

    def source(self):
        return ranged_source(self.client,'https://coordinator/api/worker/visual/job',{'X-Lease-Id':'synthetic-lease'},self.task,threading.Event(),{})

    def command(self,source,guard=None):
        with patch.dict(os.environ,{'RECORDINGS_VISUAL_SOURCE_CONTROL':source['control'],'RECORDINGS_VISUAL_NET_GUARD':str(guard or self.guard)}):
            args,env=network_guard(['ffprobe','-v','error','-protocol_whitelist','file,pipe,http,tcp','-show_format',source['url']])
        self.assertNotIn('RECORDINGS_VISUAL_SOURCE_CONTROL',env)
        return args,env

    def execute(self,args,env):
        return subprocess.run(args,env=env,stdin=subprocess.DEVNULL,capture_output=True,timeout=10)

    def test_packaged_ffprobe_can_read_attested_source(self):
        with self.source() as source:
            args,env=self.command(source)
            result=self.execute(args,env)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertGreater(len(self.ranges),1)

    def test_unloaded_guard_releases_no_media(self):
        invalid=self.root/'invalid.so';invalid.write_text('not a shared library')
        with self.source() as source:
            args,env=self.command(source,invalid)
            result=self.execute(args,env)
        self.assertNotEqual(result.returncode,0)
        self.assertEqual(self.ranges,[(0,0)])

    def test_guard_blocks_other_loopback_port_and_hostname(self):
        from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
        hits=[]
        class Trap(BaseHTTPRequestHandler):
            def do_GET(self):
                hits.append(self.path);self.send_response(200);self.end_headers()
            def log_message(self,*args):pass
        trap=ThreadingHTTPServer(('127.0.0.1',0),Trap)
        thread=threading.Thread(target=trap.serve_forever,daemon=True);thread.start()
        try:
            with self.source() as source:
                for host in ('127.0.0.1','localhost'):
                    args,env=self.command(source)
                    args[-1]=f'http://{host}:{trap.server_port}/private'
                    result=self.execute(args,env)
                    self.assertNotEqual(result.returncode,0)
            self.assertEqual(hits,[])
            self.assertEqual(self.ranges,[(0,0)])
        finally:trap.shutdown();trap.server_close();thread.join(timeout=2)


if __name__=='__main__':unittest.main()
