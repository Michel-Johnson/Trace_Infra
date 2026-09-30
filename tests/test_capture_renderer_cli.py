import argparse
import asyncio
import base64
import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

scripts = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(scripts))
spec = importlib.util.spec_from_file_location('capture_renderer', scripts / 'capture_doubao_renderer.py')
capture_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(capture_module)


class FakeCDP:
    def __init__(self, fail=False):
        self.fail=fail;self.expressions=[];self.drains=0
        self.body='data: {"text":"中文"}\n\n'.encode()
    async def __aenter__(self): return self
    async def __aexit__(self,*args): pass
    async def send(self, data):
        self.command=json.loads(data);self.expressions.append(self.command['params']['expression'])
    async def recv(self):
        e=self.expressions[-1]
        if e.endswith('.drain()'):
            self.drains+=1
            if self.fail: raise RuntimeError('synthetic disconnection')
            events=[] if self.drains>1 else [
                {'type':'fetch_start','request':1},
                {'type':'stream_chunk','request':1,'bytes_b64':base64.b64encode(self.body).decode(),'length':len(self.body)},
                {'type':'stream_end','request':1}]
            value={'events':[json.dumps(r) for r in events],'status':{'enabled':True,'counts':{'target_fetches':1,'stream_chunks':1,'dropped':0}}}
        elif e.endswith('.stop()'):
            value={'enabled':False,'restored':{'fetch':True,'read':True,'getReader':True}}
        elif e.startswith('delete '): value=True
        else: value={'enabled':True}
        return json.dumps({'id':self.command['id'],'result':{'result':{'value':value}}})


class RendererCollectorTests(unittest.TestCase):
    def exercise(self, folder, fake):
        args=argparse.Namespace(output=folder,port=9222,target_url='doubaowork://doubaowork-chat/chat',seconds=20,
                                query_id='synthetic-hook',env_id='synthetic-env',stop_after_streams=1)
        targets=[{'type':'page','url':args.target_url,'webSocketDebuggerUrl':'ws://127.0.0.1:9222/devtools/page/synthetic'}]
        fake_module=type('WS',(),{'connect':staticmethod(lambda *a,**kw:fake)})()
        with patch.dict(sys.modules, {'websockets':fake_module}), patch.object(capture_module.urllib.request,'urlopen',return_value=io.BytesIO(json.dumps(targets).encode())),contextlib.redirect_stdout(io.StringIO()):
            asyncio.run(capture_module.capture(args))

    def test_private_files_raw_bytes_and_cleanup(self):
        with tempfile.TemporaryDirectory() as root:
            folder=Path(root)/'capture';fake=FakeCDP();self.exercise(folder,fake)
            m=json.loads((folder/'manifest.json').read_text())
            self.assertEqual((folder/'response-1.sse').read_bytes(),fake.body)
            self.assertEqual(m['events'],3);self.assertEqual(m['issues'],[])
            self.assertIsNone(m['semantic_trace_complete'])
            self.assertEqual(m['ended_reader_requests'],[1])
            self.assertTrue(all(m['cleanup']['restored'].values()))
            self.assertEqual(folder.stat().st_mode & 0o777,0o700)
            self.assertTrue(all((f.stat().st_mode & 0o777)==0o600 for f in folder.iterdir()))

    def test_failure_attempts_restore_and_records_unknown_status(self):
        with tempfile.TemporaryDirectory() as root:
            folder=Path(root)/'capture';fake=FakeCDP(fail=True)
            with self.assertRaises(RuntimeError):self.exercise(folder,fake)
            m=json.loads((folder/'manifest.json').read_text())
            self.assertTrue(any(e.endswith('.stop()') for e in fake.expressions))
            self.assertTrue(all(m['cleanup']['restored'].values()))
            self.assertIn('RuntimeError',m['issues']);self.assertIn('hook_status_unavailable',m['issues'])
