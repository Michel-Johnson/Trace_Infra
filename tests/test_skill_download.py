"""The downloadable bundle is self-contained and declares its remote service."""
import io
import json
import sys
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'apps/api')]

from trace_hunter_api.app import build_skill_archive, build_skill_manifest


class SkillDownloadTests(unittest.TestCase):
    def bundle(self):
        return build_skill_archive(ROOT / 'skills', ROOT / 'scripts' / 'trace_hunter_cli.py')

    def test_bundle_contains_skills_cli_and_service_manifest(self):
        with zipfile.ZipFile(io.BytesIO(self.bundle())) as archive:
            names = set(archive.namelist())
            manifest = json.loads(archive.read('trace-hunter-client/manifest.json'))
            cli = archive.getinfo('trace-hunter-client/skills/trace-hunter-cli/scripts/trace_hunter_cli.py')
        expected = {f'trace-hunter-client/skills/{path.parent.name}/SKILL.md'
                    for path in (ROOT / 'skills').glob('*/SKILL.md')}
        self.assertTrue(expected.issubset(names))
        self.assertFalse(any(name.endswith('/VERSION.json') for name in names))
        self.assertEqual(manifest['format'], 'trace-hunter-skill-bundle/1')
        self.assertEqual(manifest['bundle_version'],
                         json.loads((ROOT / 'skills' / 'bundle.json').read_text())['bundle_version'])
        self.assertEqual(manifest['check_url'], 'http://10.37.24.3:8766/api/skills/manifest')
        self.assertEqual(manifest['download_url'], 'http://10.37.24.3:8766/api/skills/archive')
        self.assertTrue(all(isinstance(name, str) for name in manifest['skills']))
        self.assertEqual({item['name'] for item in manifest['skill_versions']}, set(manifest['skills']))
        self.assertEqual(manifest['service']['url'], 'http://10.37.24.3:8766')
        self.assertEqual(manifest['service']['default_project'], 'benchmark-a')
        self.assertEqual(manifest['service']['authentication'], 'none')
        self.assertEqual((cli.external_attr >> 16) & 0o777, 0o755)

    def test_each_skill_digest_matches_public_manifest(self):
        manifest = build_skill_manifest(ROOT / 'skills', ROOT / 'scripts' / 'trace_hunter_cli.py')
        self.assertEqual({item['name'] for item in manifest['skills']},
                         {path.parent.name for path in (ROOT / 'skills').glob('*/SKILL.md')})
        with zipfile.ZipFile(io.BytesIO(self.bundle())) as archive:
            self.assertEqual(json.loads(archive.read('trace-hunter-client/manifest.json'))['skill_versions'],
                             manifest['skills'])

    def test_every_skill_declares_version_preflight(self):
        for skill in (ROOT / 'skills').glob('*/SKILL.md'):
            self.assertIn('## 版本预检', skill.read_text(), skill.as_posix())

    def test_bundle_is_deterministic(self):
        self.assertEqual(self.bundle(), self.bundle())


if __name__ == '__main__':
    unittest.main()
