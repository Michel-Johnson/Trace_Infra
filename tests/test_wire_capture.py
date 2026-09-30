import hashlib
import asyncio
import importlib.util
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from trace_hunter.wire_capture import WireCapture, _private_file


class WireCaptureTests(unittest.TestCase):
    def make(self, path, **kw):
        return WireCapture(path, query_id='synthetic-query', env_id='loopback', **kw)

    def test_split_utf8_binary_order_and_private_permissions(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'record'
            capture = self.make(path)
            body = 'data: 中文\n\n'.encode() + bytes(range(256))
            for fragment in (body[:7], body[7:9], body[9:]):
                capture.observe('down', fragment)
            capture.observe('up', b'POST /synthetic')
            result = capture.close(transport_complete=True)
            self.assertEqual((path / 'down.bin').read_bytes(), body)
            self.assertEqual(result['captured_sha256']['down'], hashlib.sha256(body).hexdigest())
            self.assertTrue(result['capture_complete'])
            self.assertIsNone(result['semantic_trace_complete'])
            rows = [json.loads(s) for s in (path / 'chunks.jsonl').read_text().splitlines()]
            self.assertEqual([r['sequence'] for r in rows], [0, 1, 2, 3])
            self.assertEqual([r['observed_mono_ns'] for r in rows],
                             sorted(r['observed_mono_ns'] for r in rows))
            self.assertEqual(path.stat().st_mode & 0o777, 0o700)
            for file in path.iterdir():
                self.assertEqual(file.stat().st_mode & 0o777, 0o600)

    def test_quota_reports_loss_without_raising_in_transport_path(self):
        with tempfile.TemporaryDirectory() as root:
            capture = self.make(Path(root) / 'record', max_bytes=3)
            capture.observe('down', b'abc')
            capture.observe('down', b'def')
            result = capture.close(transport_complete=True)
            self.assertFalse(result['capture_complete'])
            self.assertIn('byte_limit', result['issues'])
            self.assertEqual(result['observed_bytes']['down'], 6)
            self.assertEqual(result['captured_bytes']['down'], 3)

    def test_disk_failure_is_explicit_and_close_does_not_hang(self):
        with tempfile.TemporaryDirectory() as root:
            def failing_file(path):
                if path.name == 'up.bin':
                    raise OSError('simulated disk failure')
                return _private_file(path)
            with patch('trace_hunter.wire_capture._private_file', side_effect=failing_file):
                capture = self.make(Path(root) / 'record', queue_chunks=1)
                capture.observe('up', b'request')
                result = capture.close(transport_complete=True)
            self.assertFalse(result['capture_complete'])
            self.assertIn('writer_OSError', result['issues'])

    def test_queue_overflow_remains_visible(self):
        gate = threading.Event()
        original_writer = WireCapture._writer
        def paused_writer(capture):
            gate.wait(timeout=2)
            original_writer(capture)
        with tempfile.TemporaryDirectory() as root:
            with patch.object(WireCapture, '_writer', paused_writer):
                capture = self.make(Path(root) / 'record', queue_chunks=1)
                capture.observe('up', b'a')
                capture.observe('up', b'b')
                gate.set()
                result = capture.close(transport_complete=True)
            self.assertIn('queue_full', result['issues'])
            self.assertEqual(result['dropped_observations'], 1)

    def test_in_progress_manifest_never_claims_complete(self):
        with tempfile.TemporaryDirectory() as root:
            capture = self.make(Path(root) / 'record')
            manifest = json.loads((capture.path / 'manifest.json').read_text())
            self.assertEqual(manifest['state'], 'recording')
            self.assertFalse(manifest['capture_complete'])
            result = capture.close(transport_complete=False)
            self.assertEqual(result['started_at'], manifest['started_at'])
            self.assertIn('transport_incomplete', result['issues'])

    def test_existing_capture_cannot_be_overwritten(self):
        with tempfile.TemporaryDirectory() as root:
            capture = self.make(Path(root) / 'record')
            capture.close(transport_complete=True)
            with self.assertRaises(FileExistsError):
                self.make(Path(root) / 'record')

    def test_probe_socket_setup_failure_closes_incomplete_manifest(self):
        script = Path(__file__).resolve().parents[1] / 'scripts/probe_wire_capture.py'
        spec = importlib.util.spec_from_file_location('wire_probe_test', script)
        probe = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(probe)
        with tempfile.TemporaryDirectory() as root:
            folder = Path(root) / 'denied'
            with patch('asyncio.start_server', side_effect=PermissionError('denied')):
                with self.assertRaises(PermissionError):
                    asyncio.run(probe.exercise(folder, 'normal'))
            manifest = json.loads((folder / 'manifest.json').read_text())
            self.assertEqual(manifest['state'], 'closed')
            self.assertFalse(manifest['capture_complete'])
