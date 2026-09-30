import contextlib
import copy
import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import build_backend_corpus_manifest as corpus


class BackendCorpusManifestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bundle = self.root / 'bundle'
        (self.bundle / 'raw').mkdir(parents=True)
        self.secret = 'PRIVATE-BODY-WITH-PASSWORD-and-task-text'
        raw = json.dumps({'body': self.secret}).encode()
        (self.bundle / 'raw/source.json').write_bytes(raw)
        self.trace = json.loads((ROOT / 'docs/api/examples/run-a.request.json').read_text())
        self.trace['run']['query'] = self.secret
        self.trace['run']['title'] = self.secret
        self.trace['sources'][0].update(name='raw/source.json', sha256=corpus.digest(raw))
        self.trace['spans'][-1]['output'] = {'secret': self.secret}
        self.write('one.trace.json', self.trace)
        self.catalog = json.loads((ROOT / 'docs/api/examples/catalog.request.json').read_text())
        self.catalog['cases'][0]['conversation'] = {'mode': 'multi_turn', 'turns': [
            {'id': 'first', 'prompt': self.secret}, {'id': 'second', 'prompt': 'more ' + self.secret}]}
        self.write('catalog.json', self.catalog)

    def write(self, name, value):
        path = self.bundle / name
        path.write_text(json.dumps(value, ensure_ascii=False))
        return path

    def pack(self):
        path = self.root / 'bundle.tar.gz'
        with tarfile.open(path, 'w:gz') as archive:
            for file in sorted(self.bundle.rglob('*')):
                if file.is_file():
                    archive.add(file, arcname='bundle/' + file.relative_to(self.bundle).as_posix())
        return path

    def test_deterministic_directory_and_archive_without_bodies(self):
        directory = corpus.build_manifest(self.bundle)
        archived = corpus.build_manifest(self.pack())
        self.assertEqual(directory, archived)
        self.assertEqual(directory, corpus.build_manifest(self.bundle))
        self.assertEqual(directory['state'], 'ready')
        self.assertNotIn(self.secret, json.dumps(directory))
        self.assertEqual(directory['summary']['declared_multi_turn_run_count'], 1)
        self.assertIsNone(directory['runs'][0]['observed_user_turn_count'])
        self.assertEqual(directory['runs'][0]['declared_user_turn_count'], 2)
        self.assertEqual(directory['runs'][0]['sha256'], corpus.digest((self.bundle / 'one.trace.json').read_bytes()))

    def test_duplicate_same_run_is_counted_once_without_deduplicating_across_ids(self):
        self.write('copy.trace.json', self.trace)
        other = copy.deepcopy(self.trace)
        other['run']['id'] = 'independent-run'
        self.write('other.trace.json', other)
        result = corpus.build_manifest(self.bundle)
        self.assertEqual(result['state'], 'ready')
        self.assertEqual(result['summary']['run_count'], 2)
        self.assertEqual(result['summary']['input_trace_file_count'], 3)
        self.assertEqual(result['summary']['duplicate_file_count'], 1)
        self.assertEqual(result['duplicates'][0]['kind'], 'identical')

    def test_conflicting_run_and_catalog_do_not_leak_validator_or_input_text(self):
        changed = copy.deepcopy(self.trace)
        changed['spans'][-1]['output'] = self.secret + '-changed'
        self.write('conflict.trace.json', changed)
        other_catalog = copy.deepcopy(self.catalog)
        other_catalog['cases'][0]['title'] = 'changed'
        self.write('other.catalog.json', other_catalog)
        result = corpus.build_manifest(self.bundle)
        self.assertEqual(result['state'], 'invalid')
        self.assertEqual({x['code'] for x in result['issues']}, {'conflicting_run_id', 'conflicting_case_definition'})
        self.assertNotIn(self.secret, json.dumps(result))

    def test_missing_and_modified_sources_are_distinct_failures(self):
        path = self.bundle / 'raw/source.json'
        path.write_text('changed')
        modified = corpus.build_manifest(self.bundle)
        self.assertEqual(modified['issues'][0]['code'], 'source_digest_mismatch')
        path.unlink()
        missing = corpus.build_manifest(self.bundle)
        self.assertEqual(missing['issues'][0]['code'], 'missing_source_file')

    def test_missing_time_is_null_and_actual_zero_is_known(self):
        self.trace['spans'] = [s for s in self.trace['spans'] if s['kind'] == 'tool']
        self.trace['links'] = []
        self.trace['coverage']['model_requests'] = 'missing'
        for s in self.trace['spans']:
            s.update(start_ms=None, end_ms=None, duration_ms=None)
        self.write('one.trace.json', self.trace)
        missing = corpus.build_manifest(self.bundle)
        self.assertIsNone(missing['runs'][0]['observed_tool_ms'])
        self.assertEqual(missing['summary']['tool_time_known'], 0)
        self.trace['spans'][0]['duration_ms'] = 0
        self.write('one.trace.json', self.trace)
        zero = corpus.build_manifest(self.bundle)
        self.assertEqual(zero['runs'][0]['observed_tool_ms'], 0)
        self.assertEqual(zero['summary']['tool_time_known'], 1)

    def test_bundle_manifest_hashes_counts_and_missing_members(self):
        path = self.bundle / 'one.trace.json'
        manifest = {'schema_version': 'trace-hunter/1.1', 'state': 'ready', 'conversations': 1,
                    'import_files': [{'path': 'one.trace.json', 'bytes': path.stat().st_size,
                                      'sha256': corpus.digest(path.read_bytes())}]}
        self.write('manifest.json', manifest)
        self.assertEqual(corpus.build_manifest(self.bundle)['state'], 'ready')
        self.trace['run']['query'] = 'different'
        self.write('one.trace.json', self.trace)
        result = corpus.build_manifest(self.bundle)
        self.assertIn('import_digest_mismatch', {x['code'] for x in result['issues']})
        path.unlink()
        result = corpus.build_manifest(self.bundle)
        self.assertTrue({'missing_import_file', 'run_count_mismatch', 'no_standard_traces'} <= {x['code'] for x in result['issues']})

    def test_invalid_json_and_schema_report_only_codes(self):
        (self.bundle / 'bad.trace.json').write_text('{"private":"' + self.secret + '","private":2}')
        invalid = copy.deepcopy(self.trace)
        invalid['run']['status'] = self.secret
        self.write('invalid.trace.json', invalid)
        result = corpus.build_manifest(self.bundle)
        self.assertTrue({'invalid_json', 'invalid_trace'} <= {x['code'] for x in result['issues']})
        self.assertNotIn(self.secret, json.dumps(result))

    def test_archive_traversal_links_and_duplicate_names_fail_without_extraction(self):
        for names, kind in [(['../escape.json'], tarfile.REGTYPE),
                            (['symlink'], tarfile.SYMTYPE),
                            (['same.json', 'same.json'], tarfile.REGTYPE)]:
            with self.subTest(names=names):
                path = self.root / 'unsafe.tar'
                with tarfile.open(path, 'w') as archive:
                    for name in names:
                        member = tarfile.TarInfo(name)
                        member.type = kind
                        member.linkname = '/outside'
                        archive.addfile(member, io.BytesIO())
                with self.assertRaises(ValueError):
                    corpus.build_manifest(path)
        self.assertFalse((self.root / 'escape.json').exists())

    def test_cli_check_is_exact_and_output_must_be_ignored(self):
        output = self.root / 'var/baseline.json'
        with patch.object(corpus, 'ROOT', self.root), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(corpus.main(['--input', str(self.bundle), '--output', str(output)]), 0)
            self.assertEqual(corpus.main(['--input', str(self.bundle), '--check', str(output)]), 0)
            self.assertEqual(corpus.main(['--input', str(self.bundle), '--output', str(self.root / 'tracked.json')]), 2)
            self.trace['run']['query'] = 'new version'
            self.write('one.trace.json', self.trace)
            self.assertEqual(corpus.main(['--input', str(self.bundle), '--check', str(output)]), 2)
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)


if __name__ == '__main__':
    unittest.main()
