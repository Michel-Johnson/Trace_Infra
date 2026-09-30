"""Backend image entrypoint contract; no external database or container required."""
import contextlib
import io
import json
import os
from pathlib import Path
import shlex
import sys
import tempfile
import unittest
from unittest.mock import patch

from sqlalchemy.engine import make_url

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from deploy.containers import backend_entrypoint as entry


class ContainerEntrypointTests(unittest.TestCase):
    def setUp(self):
        folder=tempfile.TemporaryDirectory();self.addCleanup(folder.cleanup)
        self.secret=Path(folder.name)/'db-password';self.password=' p@ss:/?#%[] 空格 '
        self.secret.write_text(self.password+'\n',encoding='utf-8')
        self.parts={'DB_HOST':'postgres','DB_NAME':'trace_hunter','DB_USER':'trace:user@tenant',
                    'DB_PASSWORD_FILE':str(self.secret)}

    def test_secret_file_url_roundtrips_reserved_characters_and_spaces(self):
        value=make_url(entry.database_url(self.parts))
        self.assertEqual(value.drivername,'postgresql+psycopg')
        self.assertEqual((value.username,value.password,value.host,value.port,value.database),
                         ('trace:user@tenant',self.password,'postgres',5432,'trace_hunter'))
        self.assertEqual(make_url(entry.database_url({**self.parts,'DB_PORT':'5440'})).port,5440)

    def test_explicit_postgres_url_preserves_options_and_wins_over_parts(self):
        raw='postgresql://agent:p%40ss@db:5433/trace?sslmode=require&application_name=trace-hunter'
        value=make_url(entry.database_url({**self.parts,'DATABASE_URL':raw,'DB_PASSWORD_FILE':'/does-not-exist'}))
        self.assertEqual((value.password,value.host,value.port),('p@ss','db',5433))
        self.assertEqual(value.query,{'sslmode':'require','application_name':'trace-hunter'})

    def test_missing_database_and_non_postgres_urls_never_fall_back_to_sqlite(self):
        for config in ({},{'DATABASE_URL':'sqlite:///tmp/trace.db'},{'DATABASE_URL':'not-a-url-secret'},
                       {'DATABASE_URL':'postgresql://user:secret@db:bad/trace'}):
            with self.subTest(config_fields=list(config)):
                with self.assertRaises(entry.ConfigurationError) as caught:entry.database_url(config)
                self.assertNotIn('secret',str(caught.exception))

    def test_unreadable_empty_and_invalid_utf8_secrets_are_configuration_errors(self):
        for contents in (b'',b'\n',b'\xff'):
            self.secret.write_bytes(contents)
            with self.assertRaises(entry.ConfigurationError):entry.database_url(self.parts)
        self.secret.unlink()
        with self.assertRaises(entry.ConfigurationError):entry.database_url(self.parts)
        self.assertFalse(self.secret.exists())

    def test_invalid_database_and_api_ports_fail_before_execution_without_echo(self):
        for variable in ('DB_PORT','API_PORT'):
            with patch.dict(os.environ,{**self.parts,variable:'sensitive-port-value'},clear=True), \
                 patch.object(entry.runpy,'run_path') as run, contextlib.redirect_stderr(io.StringIO()) as stderr:
                self.assertEqual(entry.main(['api']),2);run.assert_not_called()
                self.assertIn(variable,stderr.getvalue());self.assertNotIn('sensitive-port-value',stderr.getvalue())

    def test_commands_reuse_existing_entrypoints_in_same_process(self):
        for command,script in entry.COMMANDS.items():
            with self.subTest(command=command), patch.dict(os.environ,{**self.parts,'TRACE_HUNTER_WORKER_PROJECT':'fixture'} if command=='invocation-worker' else self.parts,clear=True), \
                 patch.object(entry.os,'chdir') as chdir, patch.object(entry.runpy,'run_path') as run, \
                 patch.object(sys,'argv',['container',command]):
                self.assertEqual(entry.main([command]),0)
                run.assert_called_once_with(str(script),run_name='__main__');chdir.assert_called_once_with(ROOT)
                self.assertEqual(sys.argv,[str(script)]+(['run','--project','fixture'] if command=='invocation-worker' else []))
                self.assertEqual(make_url(os.environ['DATABASE_URL']).password,self.password)
                if command=='api':
                    self.assertEqual((os.environ['API_HOST'],os.environ['API_PORT']),('0.0.0.0','8767'))
                else:self.assertNotIn('API_HOST',os.environ)

    def test_command_failure_exits_nonzero_and_does_not_log_connection_secrets(self):
        with patch.dict(os.environ,self.parts,clear=True), patch.object(entry.os,'chdir'), \
             patch.object(entry.runpy,'run_path',side_effect=RuntimeError('postgresql://user:secret@db/trace')), \
             patch.object(sys,'argv',[]), contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(entry.main(['migrate']),1)
            self.assertIn('RuntimeError',stderr.getvalue());self.assertNotIn('secret',stderr.getvalue())

    def test_native_worker_requires_an_explicit_project(self):
        with patch.dict(os.environ,self.parts,clear=True), patch.object(entry.os,'chdir'), \
             patch.object(entry.runpy,'run_path') as run, contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(entry.main(['invocation-worker']),2);run.assert_not_called()
            self.assertIn('TRACE_HUNTER_WORKER_PROJECT',stderr.getvalue())

    def test_invalid_command_does_not_start_an_entrypoint(self):
        with patch.object(entry.runpy,'run_path') as run, contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(entry.main(['not-a-command']),2);run.assert_not_called()
            self.assertIn('api, worker, invocation-worker, migrate or rebuild-indexes',stderr.getvalue())

    def test_backend_image_copies_every_published_package_file_to_expected_path(self):
        # Bootstrap hashes refer to repository-relative files, including Web source.
        dockerfile=(ROOT/'deploy/containers/backend.Dockerfile').read_text()
        backend=dockerfile.split('FROM backend',1)[0]
        copied=set()
        for line in backend.splitlines():
            if not line.startswith('COPY '):continue
            *sources,destination=shlex.split(line)[1:]
            target=Path(destination).relative_to('/app')
            for source in sources:
                path=ROOT/source
                if path.is_dir():
                    copied.update(str(target/file.relative_to(path)) for file in path.rglob('*') if file.is_file())
                else:copied.add(str(target/path.name if destination.endswith('/') else target))
        for package in (ROOT/'plugins/extensions').glob('*/package.json'):
            self.assertIn(str(package.relative_to(ROOT)),copied)
            for filename in json.loads(package.read_text())['files']:self.assertIn(filename,copied)
        for package in (ROOT/'plugins/worker').glob('*/package.json'):
            self.assertIn(str(package.relative_to(ROOT)),copied)
            for filename in json.loads(package.read_text())['files']:
                self.assertIn(str((package.parent/filename).relative_to(ROOT)),copied)
        for filename in ('plugins/worker/registry.json','scripts/invocation_worker.py','apps/api/run.py','scripts/evaluation_worker.py','scripts/migrate_database.py','scripts/rebuild_span_indexes.py',
                         'contracts/openapi.json','schemas/trace-v1.schema.json','db/migrations/001_initial.sql',
                         'contracts/drafts/trace-v2/trace.schema.json',
                         'contracts/schemas/trace-v2-storage-v1.schema.json',
                         'contracts/schemas/trace-v2-storage-v2.schema.json'):
            self.assertIn(filename,copied)


if __name__=='__main__':unittest.main()
