"""Release failures stay isolated and restore configuration, never old data."""
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from urllib.parse import urlsplit

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'src')]
from scripts import deploy_release as release
from scripts.release_manifest import MANIFEST_NAME, MANIFEST_VERSION, REQUIRED_FILES, create_manifest, verify_manifest


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        folder=tempfile.TemporaryDirectory();self.addCleanup(folder.cleanup)
        self.home=Path(folder.name)
        self.root=self.home/'apps/trace-hunter-web-releases/candidate'
        self.data=self.home/'apps/trace-hunter-data'
        self.root.mkdir(parents=True);self.data.mkdir(parents=True)
        self.commit='a'*40
        for name in REQUIRED_FILES:
            path=self.root/name;path.parent.mkdir(parents=True,exist_ok=True)
            path.write_text('fixture '+name+'\n')
        self.caddy=self.home/'apps/trace-hunter-tools/caddy'
        self.caddy.parent.mkdir(parents=True);self.caddy.write_text('fixture');self.caddy.chmod(0o700)
        self.api_raw=b'API_HOST=127.0.0.1\nAPI_PORT=8767\n'
        (self.data/'database.env').write_text('DATABASE_URL=postgresql://fixture@127.0.0.1:55432/trace_hunter\n')
        (self.data/'api.env').write_bytes(self.api_raw)
        self.old_root=self.home/'apps/trace-hunter-web-releases/old/dist'
        self.old_root.mkdir(parents=True)
        self.web_raw=('SITE_ADDRESS=http://127.0.0.1:8766\nWEB_HOST=127.0.0.1\n'
                      'API_UPSTREAM=127.0.0.1:8767\nWEB_ROOT='+str(self.old_root)+'\n').encode()
        (self.data/'web.env').write_bytes(self.web_raw)
        self.units=self.home/'.config/systemd/user';self.units.mkdir(parents=True)
        (self.units/'trace-hunter-worker.service').write_text('old worker')
        for name,value in [('ROOT',self.root),('HOME_DIR',self.home),('DATA',self.data)]:
            manager=patch.object(release,name,value);manager.start();self.addCleanup(manager.stop)
        manager=patch.dict(os.environ,{},clear=True);manager.start();self.addCleanup(manager.stop)
        self.freeze()

    def freeze(self):
        files={path.relative_to(self.root).as_posix():hashlib.sha256(path.read_bytes()).hexdigest()
               for path in self.root.rglob('*') if path.is_file() and path.name not in
               (MANIFEST_NAME,'verified.json','release-test-result.json','release-tests.log')}
        (self.root/MANIFEST_NAME).write_text(json.dumps({'schema_version':MANIFEST_VERSION,'commit':self.commit,'files':files}))
        self.frozen=verify_manifest(self.root)
        (self.root/'verified.json').write_text(json.dumps({**self.frozen,
            'tests':{'tests':3,'failures':0,'errors':0,'skipped':0}}))

    def test_environment_keeps_shared_config_and_removes_browser_credentials(self):
        content=self.data/'objects'
        (self.data/'api.env').write_bytes(self.api_raw+f'TRACE_HUNTER_CONTENT_DIR="{content}"\n'.encode())
        with patch.dict(os.environ,{'TRACE_HUNTER_AUTH_USERNAME':'fixture','TRACE_HUNTER_AUTH_PASSWORD':'private-fixture'}):
            result=release.environment()
            self.assertEqual(result['TRACE_HUNTER_CONTENT_DIR'],str(content))
            self.assertEqual(result['API_HOST'],'127.0.0.1')
            self.assertNotIn('TRACE_HUNTER_AUTH_PASSWORD',result)
            self.assertNotIn('TRACE_HUNTER_AUTH_USERNAME',result)
            self.assertNotIn('TRACE_HUNTER_CONTENT_DIR',os.environ)

    def test_content_defaults_persist_outside_release_and_preserve_api_settings(self):
        env,raw=release.content_configuration(release.environment(),self.api_raw)
        self.assertEqual(env['TRACE_HUNTER_CONTENT_DIR'],str(self.data/'content'))
        self.assertTrue(raw.startswith(self.api_raw))
        self.assertEqual((self.data/'api.env').read_bytes(),self.api_raw)
        self.assertFalse((self.data/'content').exists())

    def test_content_rejects_release_local_and_relative_directories(self):
        for value in ('content',str(self.root/'content'),str(self.old_root/'content')):
            with self.subTest(value=value),self.assertRaises(RuntimeError):
                release.content_configuration({'TRACE_HUNTER_CONTENT_DIR':value},self.api_raw)

    def test_store_uses_explicit_content_without_exporting_environment(self):
        from trace_hunter.storage import Store
        with patch('trace_hunter.storage.Store',autospec=Store) as store:
            release.configured_store({'DATABASE_URL':'unused-fixture',
                                     'TRACE_HUNTER_CONTENT_DIR':str(self.data/'objects')})
        self.assertEqual(store.call_args.kwargs['content_store'].root,self.data/'objects')
        self.assertNotIn('TRACE_HUNTER_CONTENT_DIR',os.environ)

    def test_verification_overrides_both_database_urls_and_content(self):
        production=release.environment()
        production.update(TEST_DATABASE_URL='postgresql://wrong/trace_hunter',TRACE_HUNTER_CONTENT_DIR=str(self.data/'content'))
        temporary=self.home/'isolated-content'
        result=release.test_environment(production,temporary)
        self.assertEqual(result['DATABASE_URL'],result['TEST_DATABASE_URL'])
        self.assertTrue(result['DATABASE_URL'].endswith('/trace_hunter_stage'))
        self.assertEqual(result['TRACE_HUNTER_CONTENT_DIR'],str(temporary))
        self.assertEqual(result['CADDY_BINARY'],str(self.caddy))
        self.assertTrue(production['DATABASE_URL'].endswith('/trace_hunter'))

    def test_release_verification_requires_real_caddy_and_separate_database(self):
        with self.assertRaises(RuntimeError):
            release.test_environment({'DATABASE_URL':'postgresql://fixture/trace_hunter_stage'},self.home/'temp')
        self.caddy.unlink()
        with self.assertRaises(RuntimeError):
            release.test_environment(release.environment(),self.home/'temp')

    def test_changed_code_is_rejected_before_activation_reads_environment(self):
        (self.root/'apps/api/run.py').write_text('changed source')
        with patch.object(release,'environment') as env,self.assertRaises(RuntimeError):
            release.activate(self.commit,self.frozen['index_sha256'])
        env.assert_not_called()
        self.assertFalse((self.data/'backups').exists())

    def test_unlisted_executable_or_asset_is_rejected(self):
        for name in ('src/injected.py','dist/extra.js','sqlalchemy.py'):
            path=self.root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text('extra')
            with self.subTest(name=name),self.assertRaises(RuntimeError):verify_manifest(self.root)
            path.unlink()

    def test_manifest_does_not_follow_external_symlinks_or_traversal(self):
        outside=self.home/'outside.py';outside.write_text('outside')
        target=self.root/'apps/api/run.py';target.unlink();target.symlink_to(outside)
        with self.assertRaises(RuntimeError):verify_manifest(self.root)
        target.unlink();target.write_text('fixture apps/api/run.py\n')
        manifest=json.loads((self.root/MANIFEST_NAME).read_text())
        manifest['files']['../outside.py']=hashlib.sha256(outside.read_bytes()).hexdigest()
        (self.root/MANIFEST_NAME).write_text(json.dumps(manifest))
        with self.assertRaises(RuntimeError):verify_manifest(self.root)

    def test_manifest_supports_exact_repository_links_to_hashed_targets(self):
        target=self.root/'contracts/schemas/test.json';target.parent.mkdir(parents=True);target.write_text('{}')
        (self.root/'schemas').symlink_to('contracts/schemas',target_is_directory=True)
        api=self.root/'contracts/openapi.json';api.write_text('{"openapi":"fixture"}')
        alias=self.root/'docs/api/openapi.json';alias.parent.mkdir(parents=True);alias.symlink_to('../../contracts/openapi.json')
        frozen=create_manifest(self.root,self.commit)
        self.assertEqual(frozen['link_count'],2)
        self.assertEqual(verify_manifest(self.root),frozen)
        # An equivalent link spelling still changes the committed source.
        alias.unlink();alias.symlink_to('../../contracts/./openapi.json')
        with self.assertRaises(RuntimeError):verify_manifest(self.root)

    def test_failed_or_skipped_verification_removes_previous_approval(self):
        for code,skipped in ((1,0),(0,1)):
            self.freeze()
            def run(*args,**kwargs):
                (self.root/'release-test-result.json').write_text(json.dumps({'tests':3,'errors':0,'failures':0,'skipped':skipped}))
                self.assertTrue(kwargs['env']['DATABASE_URL'].endswith('/trace_hunter_stage'))
                self.assertNotEqual(kwargs['env']['TRACE_HUNTER_CONTENT_DIR'],str(self.data/'content'))
                return subprocess.CompletedProcess(args[0],code)
            with self.subTest(code=code,skipped=skipped),patch.object(release.subprocess,'run',side_effect=run),self.assertRaises(RuntimeError):
                release.verify()
            self.assertFalse((self.root/'verified.json').exists())

    def test_verification_rejects_source_changed_by_test_run(self):
        def run(*args,**kwargs):
            (self.root/'release-test-result.json').write_text(json.dumps({'tests':3,'errors':0,'failures':0,'skipped':0}))
            (self.root/'apps/api/run.py').write_text('modified by test')
            return subprocess.CompletedProcess(args[0],0)
        with patch.object(release.subprocess,'run',side_effect=run),self.assertRaises(RuntimeError):release.verify()
        self.assertFalse((self.root/'verified.json').exists())

    def test_successful_verification_binds_full_manifest_and_removes_test_content(self):
        captured=[]
        def run(*args,**kwargs):
            content=Path(kwargs['env']['TRACE_HUNTER_CONTENT_DIR']);content.mkdir();captured.append(content)
            (content/'test-object').write_text('fixture')
            (self.root/'release-test-result.json').write_text(json.dumps({'tests':3,'errors':0,'failures':0,'skipped':0}))
            return subprocess.CompletedProcess(args[0],0)
        with patch.object(release.subprocess,'run',side_effect=run):release.verify()
        result=json.loads((self.root/'verified.json').read_text())
        self.assertEqual(result['manifest_sha256'],self.frozen['manifest_sha256'])
        self.assertFalse(captured[0].exists())

    def start_activation_mocks(self,*,stage_failed=False,backfill_failed=False,rebuild_failed=False):
        store=MagicMock();store.repository.rows.return_value=[{'present':False}]
        opener=MagicMock()
        def response(request,**kwargs):
            path=urlsplit(request.full_url).path
            if path=='/':return io.BytesIO((self.root/'dist/index.html').read_bytes())
            value={'/api/runs':[{'id':'existing','digest':'unchanged'}],
                   '/api/health':{'database':'postgresql'},'/api/plugins':[{},{}],
                   '/api/extensions':[{'manifest':{'plugin_id':'official.call-activity'}}],
                   '/api/v1/projects/default':{'project_id':'default'},
                   '/api/v1/projects/default/operations':{'items':[{},{}]}}
            return io.BytesIO(json.dumps(value[path]).encode())
        opener.open.side_effect=response
        def run(args,**kwargs):
            code=0
            if args[:4]==['systemctl','--user','is-enabled','--quiet']:
                code=int(args[-1]=='trace-hunter-invocation-worker')
            if 'stop' in args or 'disable' in args:code=1 # a new unit can be absent
            return subprocess.CompletedProcess(args,code)
        stage=MagicMock();stage.poll.return_value=9 if stage_failed else None
        registry=MagicMock();registry.register.return_value=[{},{}]
        mocks={
            'commands':patch.object(release,'command'),
            'systemctl':patch.object(release,'systemctl'),
            'store':patch.object(release,'configured_store',return_value=store),
            'opener':patch.object(release.urllib.request,'build_opener',return_value=opener),
            'run':patch.object(release.subprocess,'run',side_effect=run),
            'stage':patch.object(release.subprocess,'Popen',return_value=stage),
            'socket':patch.object(release.socket,'socket'),
            'active':patch.object(release,'active',side_effect=lambda unit:unit=='trace-hunter-worker'),
            'sleep':patch.object(release.time,'sleep'),
            'registry':patch('trace_hunter.workers.WorkerRegistry',return_value=registry),
            'backfill':patch('scripts.backfill_trace_revisions.backfill',return_value={
                'state':'failed' if backfill_failed else 'complete','created':1,'indexed':1}),
            'rebuild':patch('scripts.rebuild_span_indexes.rebuild',return_value={
                'state':'failed' if rebuild_failed else 'complete','rebuilt':1,
                'projector_version':'trace-index/4'}),
        }
        started={}
        for name,manager in mocks.items():
            started[name]=manager.start();self.addCleanup(manager.stop)
        return started,store,stage

    def test_backup_failure_does_not_write_content_config_or_restart(self):
        mocks,store,_=self.start_activation_mocks()
        mocks['commands'].side_effect=RuntimeError('backup fixture failed')
        with self.assertRaisesRegex(RuntimeError,'backup fixture failed'):
            release.activate(self.commit,self.frozen['index_sha256'])
        self.assertEqual((self.data/'api.env').read_bytes(),self.api_raw)
        self.assertFalse((self.data/'content').exists())
        mocks['store'].assert_not_called();mocks['systemctl'].assert_not_called()

    def test_backfill_failure_preserves_old_services_and_new_persistent_content(self):
        mocks,store,_=self.start_activation_mocks(backfill_failed=True)
        with self.assertRaisesRegex(RuntimeError,'backfill'):
            release.activate(self.commit,self.frozen['index_sha256'])
        store.repository.migrate.assert_called_once()
        self.assertEqual((self.data/'api.env').read_bytes(),self.api_raw)
        self.assertTrue((self.data/'content').is_dir())
        mocks['systemctl'].assert_not_called();mocks['stage'].assert_not_called()

    def test_span_rebuild_failure_preserves_old_services(self):
        mocks,store,_=self.start_activation_mocks(rebuild_failed=True)
        with self.assertRaisesRegex(RuntimeError,'Span projection rebuild'):
            release.activate(self.commit,self.frozen['index_sha256'])
        store.repository.migrate.assert_called_once()
        self.assertEqual((self.data/'api.env').read_bytes(),self.api_raw)
        mocks['systemctl'].assert_not_called();mocks['stage'].assert_not_called()

    def test_successful_cutover_installs_both_workers_and_records_frozen_source(self):
        mocks,store,_=self.start_activation_mocks()
        invocation_started=False
        def systemctl(*args):
            nonlocal invocation_started
            if args==('enable','--now','trace-hunter-invocation-worker'):invocation_started=True
        mocks['systemctl'].side_effect=systemctl
        mocks['active'].side_effect=lambda unit:unit=='trace-hunter-worker' or invocation_started
        with patch('builtins.print'):
            release.activate(self.commit,self.frozen['index_sha256'])
        self.assertIn(b'TRACE_HUNTER_CONTENT_DIR=',(self.data/'api.env').read_bytes())
        self.assertIn(str(self.root/'dist').encode(),(self.data/'web.env').read_bytes())
        for unit in release.WORKERS:
            self.assertTrue((self.units/(unit+'.service')).is_file())
            mocks['systemctl'].assert_any_call('enable','--now',unit)
        self.assertIn('invocation_worker.py run --project default',
                      (self.units/'trace-hunter-invocation-worker.service.d/release.conf').read_text())
        record=json.loads((self.data/'platform-release.json').read_text())
        self.assertEqual(record['commit'],self.commit)
        self.assertEqual(record['manifest_sha256'],self.frozen['manifest_sha256'])
        self.assertEqual(record['existing_runs_preserved'],1)
        self.assertEqual(record['registered_operations'],2)
        self.assertEqual(record['backfill']['state'],'complete')
        self.assertEqual(record['span_index_rebuild']['projector_version'],'trace-index/4')
        self.assertEqual(record['content_directory'],str(self.data/'content'))
        store.repository.migrate.assert_called_once()

    def test_stage_failure_stops_only_stage_process_and_leaves_services_unchanged(self):
        mocks,store,stage=self.start_activation_mocks(stage_failed=True)
        with self.assertRaisesRegex(RuntimeError,'Staged API failed'):
            release.activate(self.commit,self.frozen['index_sha256'])
        stage.send_signal.assert_called_once();stage.wait.assert_called_once()
        mocks['systemctl'].assert_not_called()
        self.assertEqual((self.data/'web.env').read_bytes(),self.web_raw)
        self.assertEqual((self.data/'api.env').read_bytes(),self.api_raw)

    def test_partial_cutover_restores_shared_env_and_old_worker_without_reverting_data(self):
        mocks,store,stage=self.start_activation_mocks()
        original=release.atomic;failed=False
        web_override=self.units/'trace-hunter-web.service.d/release.conf'
        def interrupted(path,raw):
            nonlocal failed
            if path==web_override and not failed:
                failed=True
                self.assertIn(b'TRACE_HUNTER_CONTENT_DIR=',(self.data/'api.env').read_bytes())
                raise OSError('fixture cutover interrupted')
            return original(path,raw)
        with patch.object(release,'atomic',side_effect=interrupted),self.assertRaisesRegex(OSError,'cutover interrupted'):
            release.activate(self.commit,self.frozen['index_sha256'])
        self.assertEqual((self.data/'api.env').read_bytes(),self.api_raw)
        self.assertEqual((self.data/'web.env').read_bytes(),self.web_raw)
        self.assertEqual((self.units/'trace-hunter-worker.service').read_text(),'old worker')
        self.assertFalse((self.units/'trace-hunter-invocation-worker.service').exists())
        self.assertTrue((self.data/'content').exists())
        self.assertFalse((self.data/'platform-release.json').exists())
        mocks['systemctl'].assert_any_call('restart','trace-hunter-api','trace-hunter-web')
        mocks['systemctl'].assert_any_call('start','trace-hunter-worker')
        self.assertNotIn(unittest.mock.call('start','trace-hunter-invocation-worker'),mocks['systemctl'].call_args_list)
        self.assertTrue(all('pg_restore' not in ' '.join(call.args) for call in mocks['commands'].call_args_list))
        store.repository.migrate.assert_called_once()


if __name__=='__main__':unittest.main()
