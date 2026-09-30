import argparse
import asyncio
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
spec = importlib.util.spec_from_file_location('collect_usage', scripts / 'collect_doubao_usage.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class FakeCDP:
    def __init__(self, denied=False, disconnect=False):
        self.denied = denied
        self.disconnect = disconnect
        self.expressions = []

    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass

    async def send(self, payload):
        self.command = json.loads(payload)
        self.expressions.append(self.command['params']['expression'])

    async def recv(self):
        expression = self.expressions[-1]
        if expression.startswith('// Read one page'):
            value = {'started': True}
        elif 'clearTimeout' in expression:
            value = True
        elif self.disconnect:
            raise RuntimeError('simulated lost connection')
        else:
            value = {'done': True, 'response': {'code': 403 if self.denied else 0,
                     'data': {'entries': [], 'has_more': False}}}
        return json.dumps({'id': self.command['id'], 'result': {'result': {'value': value}}})


class UsageCollectorTests(unittest.TestCase):
    def exercise(self, output, fake):
        args = argparse.Namespace(output=output, port=9222,
                                  target_url='doubaowork://doubaowork-chat/chat',
                                  chunk_key='@flow-web/desktop:stable', api_module=359531)
        targets = [{'type': 'page', 'url': args.target_url,
                    'webSocketDebuggerUrl': 'ws://127.0.0.1:9222/devtools/page/test'}]
        ws_module = type('WS', (), {'connect': staticmethod(lambda *a, **kw: fake)})()
        with patch.dict(sys.modules, {'websockets': ws_module}), patch.object(
                module.urllib.request, 'urlopen', return_value=io.BytesIO(json.dumps(targets).encode())), contextlib.redirect_stdout(io.StringIO()):
            return asyncio.run(module.collect(args))

    def test_access_denial_is_not_successful_zero_usage(self):
        for denied in (False, True):
            with self.subTest(denied=denied), tempfile.TemporaryDirectory() as root:
                output = Path(root) / 'capture'
                self.assertEqual(self.exercise(output, FakeCDP(denied=denied)), not denied)
                manifest = json.loads((output / 'manifest.json').read_text())
                self.assertEqual(manifest['state'], 'query_failed' if denied else 'collected')
                self.assertTrue(manifest['probe_removed'])
                self.assertIsNone(manifest['actual_token_usage'])
                self.assertEqual(output.stat().st_mode & 0o777, 0o700)
                self.assertTrue(all(p.stat().st_mode & 0o777 == 0o600 for p in output.iterdir()))
                self.assertEqual(json.loads((output / 'response.json').read_text())['response']['code'], 403 if denied else 0)
                with self.assertRaises(FileExistsError):
                    self.exercise(output, FakeCDP())

    def test_connection_failure_still_attempts_cleanup(self):
        with tempfile.TemporaryDirectory() as root:
            output = Path(root) / 'capture'
            fake = FakeCDP(disconnect=True)
            with self.assertRaises(RuntimeError):
                self.exercise(output, fake)
            manifest = json.loads((output / 'manifest.json').read_text())
            self.assertEqual(manifest['state'], 'failed')
            self.assertTrue(any('clearTimeout' in e for e in fake.expressions))
            self.assertFalse((output / 'response.json').exists())
