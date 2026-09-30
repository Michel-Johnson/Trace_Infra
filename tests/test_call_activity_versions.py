"""Published renderer versions must keep verifiable, independent packages."""
import hashlib
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class CallActivityVersionsTests(unittest.TestCase):
    def test_both_versions_match_their_shipped_files(self):
        for directory in ('call-activity', 'call-activity-1.1.0'):
            folder = ROOT / 'plugins/extensions' / directory
            manifest = json.loads((folder / 'manifest.json').read_text())
            files = json.loads((folder / 'package.json').read_text())['files']
            for name, digest in files.items():
                with self.subTest(version=manifest['version'], file=name):
                    self.assertEqual(hashlib.sha256((ROOT / name).read_bytes()).hexdigest(), digest)
            payload = json.dumps(files, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
            self.assertEqual(hashlib.sha256(payload).hexdigest(), manifest['package_digest'])
            if directory == 'call-activity':
                self.assertEqual(manifest['package_digest'], 'a40b4b1d987751f7823f047dec3869d4ec42f14f8ec60d8a625a3e19212d14bd')

