#!/usr/bin/env python3
"""Verify, back up and activate this repository on the existing intranet host.

Run from a fully uploaded versioned release directory with its built dist/.
The database remains external to releases. Additive migrations are never undone.
"""
import base64
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import urllib.error

os.umask(0o077)
ROOT=Path(__file__).resolve().parents[1]
HOME_DIR=Path.home();DATA=HOME_DIR/'apps/trace-hunter-data'
RUNTIME=HOME_DIR/'apps/trace-hunter-next/.venv/bin/python'
sys.path[:0]=[str(ROOT),str(ROOT/'src')]
from scripts.release_manifest import verify_manifest

WORKERS=('trace-hunter-worker','trace-hunter-invocation-worker')
TEST_RUNNER='''import json, pathlib, sys, unittest
suite=unittest.defaultTestLoader.discover('tests', pattern='test_*.py')
result=unittest.TextTestRunner(verbosity=1).run(suite)
pathlib.Path('release-test-result.json').write_text(json.dumps({
    'tests':result.testsRun, 'failures':len(result.failures),
    'errors':len(result.errors), 'skipped':len(result.skipped)}))
sys.exit(0 if result.wasSuccessful() and not result.skipped and result.testsRun else 1)
'''

def environment():
    env=os.environ.copy()
    for path in (DATA/'database.env',DATA/'api.env'):
        for line in path.read_text().splitlines():
            if line.strip() and not line.lstrip().startswith('#'):
                key,value=line.split('=',1);env[key]=value.strip().strip('"')
    # Temporary browser verification secrets do not belong in child API/worker environments.
    for key in ('TRACE_HUNTER_AUTH_USERNAME','TRACE_HUNTER_AUTH_PASSWORD'):env.pop(key,None)
    env['PYTHONDONTWRITEBYTECODE']='1'
    return env

def command(*args):return subprocess.run(list(args),check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
def systemctl(*args):return command('systemctl','--user',*args)
def active(unit):return subprocess.run(['systemctl','--user','is-active','--quiet',unit]).returncode==0
def atomic(path,raw):
    path.parent.mkdir(parents=True,exist_ok=True);temp=path.with_name('.'+path.name+'.pending')
    temp.write_bytes(raw);os.chmod(temp,0o600);os.replace(temp,path)

def configured_store(env):
    # The deployment process itself does not inherit values read from api.env.
    from trace_hunter.storage import Store
    from trace_hunter.content import LocalContentStore
    return Store(env['DATABASE_URL'],content_store=LocalContentStore(env['TRACE_HUNTER_CONTENT_DIR']))

def content_configuration(env, api_raw):
    value=env.get('TRACE_HUNTER_CONTENT_DIR') or str(DATA/'content')
    content=Path(value)
    if (not content.is_absolute() or any(char in value for char in ('\n','\r','\x00','"')) or
            content.resolve().is_relative_to((HOME_DIR/'apps/trace-hunter-web-releases').resolve())):
        raise RuntimeError('Content storage must be an absolute persistent directory outside releases')
    env={**env,'TRACE_HUNTER_CONTENT_DIR':str(content)}
    lines=[line for line in api_raw.decode().splitlines()
           if line.partition('=')[0].strip()!='TRACE_HUNTER_CONTENT_DIR']
    lines.append('TRACE_HUNTER_CONTENT_DIR="'+str(content)+'"')
    return env,('\n'.join(lines)+'\n').encode()

def test_environment(env, content_directory):
    from sqlalchemy.engine import make_url
    base=make_url(env['DATABASE_URL'])
    if base.get_backend_name()!='postgresql' or base.database=='trace_hunter_stage':
        raise RuntimeError('Release verification requires separate production and stage PostgreSQL databases')
    base=base.set(database='trace_hunter_stage')
    stage=base.render_as_string(hide_password=False)
    caddy=HOME_DIR/'apps/trace-hunter-tools/caddy'
    if not caddy.is_file() or not os.access(caddy,os.X_OK):
        raise RuntimeError('Real Caddy is required for release verification')
    return {**env,'DATABASE_URL':stage,'TEST_DATABASE_URL':stage,
            'TRACE_HUNTER_CONTENT_DIR':str(content_directory),'CADDY_BINARY':str(caddy),
            'TRACE_HUNTER_TEST_ROOT':str(ROOT),'CADDYFILE_UNDER_TEST':str(ROOT/'deploy/Caddyfile'),
            'PYTHONPATH':os.pathsep.join(str(path) for path in (ROOT,ROOT/'src',ROOT/'apps/api'))}

def prepare():
    if ROOT.parent!=(HOME_DIR/'apps/trace-hunter-web-releases').resolve():raise RuntimeError('Deploy only from a versioned release directory')
    source=(HOME_DIR/'apps/trace-hunter-next/apps/trace-lab').resolve()
    link=ROOT/'apps/trace-lab'
    if source.is_dir() and not link.exists():link.symlink_to(source,target_is_directory=True)
    if not (ROOT/'dist/index.html').is_file():raise RuntimeError('Upload the complete frontend build first')

def verify():
    frozen=verify_manifest(ROOT)
    # A failed or interrupted re-verification must not leave an older approval.
    (ROOT/'verified.json').unlink(missing_ok=True)
    summary_path=ROOT/'release-test-result.json';summary_path.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory(prefix='trace-hunter-release-tests-') as directory:
        env=test_environment(environment(),Path(directory)/'content')
        with (ROOT/'release-tests.log').open('wb') as log:
            result=subprocess.run([str(RUNTIME),'-c',TEST_RUNNER],cwd=ROOT,env=env,stdout=log,stderr=log)
    if result.returncode:raise RuntimeError('Tests failed or skipped; inspect the private release-tests.log before activation')
    summary=json.loads(summary_path.read_text())
    if not summary.get('tests') or any(summary.get(key,-1)!=0 for key in ('failures','errors','skipped')):
        raise RuntimeError('Release verification did not pass all tests without skips')
    if verify_manifest(ROOT)!=frozen:raise RuntimeError('Release source changed during verification')
    atomic(ROOT/'verified.json',(json.dumps({'tested_at':time.time(),**frozen,'tests':summary})+'\n').encode())
    print('Release tests passed without skips, including dedicated PostgreSQL and real Caddy.')

def restore_services(saved, worker_states, *, auth_fallback=None):
    """Restore service configuration; persistent databases/content never roll back."""
    for unit,state in worker_states.items():
        # A partial cutover can fail before a new unit is even installed. Keep
        # restoring the previous API/configuration when that unit is absent.
        subprocess.run(['systemctl','--user','stop',unit],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        if not state['enabled']:
            subprocess.run(['systemctl','--user','disable',unit],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    for path,raw in saved.items():
        if raw is None:path.unlink(missing_ok=True)
        else:atomic(path,raw)
    if auth_fallback is not None:atomic(*auth_fallback)
    systemctl('daemon-reload')
    systemctl('restart','trace-hunter-api','trace-hunter-web')
    for unit,state in worker_states.items():
        if state['enabled']:systemctl('enable',unit)
        if state['active']:systemctl('start',unit)

def activate(commit,index_digest):
    frozen=verify_manifest(ROOT,commit=commit)
    verified=json.loads((ROOT/'verified.json').read_text())
    if (any(verified.get(key)!=value for key,value in frozen.items()) or
            index_digest!=frozen['index_sha256']):
        raise RuntimeError('Verified source or build does not match this release')
    tests=verified.get('tests',{})
    if not tests.get('tests') or any(tests.get(key,-1)!=0 for key in ('failures','errors','skipped')):
        raise RuntimeError('A complete release verification is required before activation')
    api_env_path=DATA/'api.env'
    env,content_env_raw=content_configuration(environment(),api_env_path.read_bytes())
    web_path=DATA/'web.env';old_web=web_path.read_bytes()
    web=dict(line.split('=',1) for line in old_web.decode().splitlines() if '=' in line and not line.startswith('#'))
    base=web['SITE_ADDRESS'];old_root=Path(web['WEB_ROOT']);opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    api_base=web['API_UPSTREAM'];api_base=api_base if '://' in api_base else 'http://'+api_base
    auth_enabled=bool(web.get('TRACE_HUNTER_AUTH_CONFIG'))
    username=os.environ.get('TRACE_HUNTER_AUTH_USERNAME');password=os.environ.get('TRACE_HUNTER_AUTH_PASSWORD')
    if (username is None)!=(password is None):raise RuntimeError('Provide both temporary browser verification credentials or neither')
    auth_header='Basic '+base64.b64encode((username+':'+password).encode()).decode() if username is not None else None
    caddy=HOME_DIR/'apps/trace-hunter-tools/caddy';caddyfile=ROOT/'deploy/Caddyfile'
    units=HOME_DIR/'.config/systemd/user'
    web_override=units/'trace-hunter-web.service.d/release.conf'
    api_override=units/'trace-hunter-api.service.d/release.conf'
    worker_files={unit:(units/(unit+'.service'),units/(unit+'.service.d/release.conf')) for unit in WORKERS}
    web_override_raw=f'[Service]\nWorkingDirectory={ROOT}\nExecStart=\nExecStart={caddy} run --config {caddyfile} --adapter caddyfile\n'.encode()
    subprocess.run([str(caddy),'validate','--config',str(caddyfile),'--adapter','caddyfile'],
        env={**env,**web,'WEB_ROOT':str(ROOT/'dist')},stdout=subprocess.PIPE,stderr=subprocess.PIPE,check=True)
    def fetch(path,address=api_base,authenticated=False):
        headers={'Authorization':auth_header} if authenticated and auth_header else {}
        with opener.open(urllib.request.Request(address+path,headers=headers),timeout=20) as response:return response.read()
    def get(path,address=api_base):return json.loads(fetch(path,address))
    def verify_entry():
        if auth_enabled:
            for path in ('/','/api/health','/api/runs','/about.html','/research.html'):
                try:fetch(path,base)
                except urllib.error.HTTPError as error:
                    if error.code!=401:raise RuntimeError('Unauthenticated browser entry did not return 401') from None
                else:raise RuntimeError('Browser entry is unexpectedly accessible without authentication')
        if not auth_enabled or auth_header:
            assert hashlib.sha256(fetch('/',base,bool(auth_header))).hexdigest()==index_digest
            assert json.loads(fetch('/api/health',base,bool(auth_header)))['database']=='postgresql'
            # A SPA fallback also returns 200. Verify actual static bytes instead.
            for page in ('about.html','research.html','docs/index.json'):
                if (ROOT/'dist'/page).is_file():
                    assert hashlib.sha256(fetch('/'+page,base,bool(auth_header))).digest()==hashlib.sha256((ROOT/'dist'/page).read_bytes()).digest()
    before={r['id']:r['digest'] for r in get('/api/runs')}
    backup=DATA/'backups'/ROOT.name;backup.mkdir(mode=0o700,parents=True,exist_ok=False)
    paths=(web_path,api_env_path,web_override,api_override,*(p for pair in worker_files.values() for p in pair))
    saved={path:path.read_bytes() if path.exists() else None for path in paths}
    worker_states={unit:{'active':active(unit),'enabled':subprocess.run(
        ['systemctl','--user','is-enabled','--quiet',unit],stderr=subprocess.DEVNULL).returncode==0} for unit in WORKERS}
    for index,(path,raw) in enumerate(saved.items()):
        if raw is not None:(backup/(str(index)+'-'+path.name)).write_bytes(raw)
    (backup/'restore.json').write_text(json.dumps({'files':[{'path':str(p),'backup':str(i)+'-'+p.name if raw is not None else None}
        for i,(p,raw) in enumerate(saved.items())],'workers':worker_states,
        'worker_was_active':worker_states[WORKERS[0]]['active'],'worker_was_enabled':worker_states[WORKERS[0]]['enabled']},indent=2))
    command(str(HOME_DIR/'apps/trace-hunter-postgres-18.6/bin/pg_dump'),'-h',str(DATA/'socket'),'-p','55432','-Fc','-f',str(backup/'platform.dump'),'trace_hunter')
    historical={};activation_started=False
    try:
        Path(env['TRACE_HUNTER_CONTENT_DIR']).mkdir(mode=0o700,parents=True,exist_ok=True)
        store=configured_store(env)
        try:
            for table in ('evaluation_results','plugin_artifacts'):
                if store.repository.rows('SELECT to_regclass(:name) AS present',{'name':table})[0]['present']:
                    historical[table]={(r['job_id'],r['attempt']):r['digest'] for r in store.repository.rows('SELECT job_id,attempt,digest FROM '+table)}
            store.repository.migrate()
            # Explicit release migration: preserve old payload/digest and publish
            # native revisions/indexes so existing cases are queryable by agents.
            from scripts.backfill_trace_revisions import backfill
            backfilled=backfill(store,project_id='default')
            if backfilled['state']!='complete':raise RuntimeError('Historical revision backfill did not complete')
            from scripts.rebuild_span_indexes import rebuild
            rebuilt=rebuild(store)
            if rebuilt['state']!='complete':raise RuntimeError('Span projection rebuild did not complete')
            from trace_hunter.workers import WorkerRegistry
            from trace_hunter.invocations import Invocations
            registry=WorkerRegistry()
            registered=registry.register(Invocations(store.repository),'default')
        finally:store.close()
        with socket.socket() as probe:probe.bind(('127.0.0.1',18767))
        stage_env={**env,'API_HOST':'127.0.0.1','API_PORT':'18767'}
        with (ROOT/'stage-api.log').open('wb') as log:
            stage=subprocess.Popen([str(RUNTIME),str(ROOT/'apps/api/run.py')],cwd=ROOT,env=stage_env,stdout=log,stderr=log)
            try:
                for attempt in range(60):
                    if stage.poll() is not None:raise RuntimeError('Staged API failed; inspect stage-api.log')
                    try:
                        address='http://127.0.0.1:18767'
                        assert get('/api/health',address)['database']=='postgresql'
                        assert len(get('/api/plugins',address))>=2
                        assert any(p['manifest']['plugin_id']=='official.call-activity' for p in get('/api/extensions',address))
                        staged={r['id']:r['digest'] for r in get('/api/runs',address)}
                        assert all(staged.get(rid)==digest for rid,digest in before.items())
                        assert get('/api/v1/projects/default',address)['project_id']=='default'
                        assert len(get('/api/v1/projects/default/operations',address)['items'])>=len(registered)
                        break
                    except Exception:
                        if attempt==59:raise
                        time.sleep(.2)
            finally:
                stage.send_signal(signal.SIGINT)
                try:stage.wait(timeout=15)
                except subprocess.TimeoutExpired:stage.kill();stage.wait(timeout=5)
        # Include any retained old hashed assets in the frozen manifest before
        # verification. Activation must not add unverified source or assets.
        if verify_manifest(ROOT,commit=commit)!=frozen:raise RuntimeError('Release changed after verification')
        activation_started=True
        atomic(api_env_path,content_env_raw)
        atomic(web_override,web_override_raw)
        atomic(api_override,f'[Service]\nWorkingDirectory={ROOT}\nExecStart=\nExecStart={RUNTIME} {ROOT}/apps/api/run.py\n'.encode())
        for unit,(unit_path,override) in worker_files.items():
            script='evaluation_worker.py' if unit==WORKERS[0] else 'invocation_worker.py run --project default'
            atomic(unit_path,(ROOT/'deploy'/(unit+'.service')).read_bytes())
            atomic(override,f'[Service]\nWorkingDirectory={ROOT}\nExecStart=\nExecStart={RUNTIME} {ROOT}/scripts/{script}\n'.encode())
        atomic(web_path,('\n'.join('WEB_ROOT='+str(ROOT/'dist') if l.startswith('WEB_ROOT=') else l for l in old_web.decode().splitlines())+'\n').encode())
        systemctl('daemon-reload');systemctl('restart','trace-hunter-api','trace-hunter-web')
        for unit,state in worker_states.items():
            systemctl('enable','--now',unit)
            if state['active']:systemctl('restart',unit)
        for attempt in range(60):
            try:
                assert get('/api/health')['database']=='postgresql'
                verify_entry()
                assert len(get('/api/plugins'))>=2
                assert any(p['manifest']['plugin_id']=='official.call-activity' for p in get('/api/extensions'))
                assert all(active(unit) for unit in WORKERS)
                assert get('/api/v1/projects/default')['project_id']=='default'
                assert len(get('/api/v1/projects/default/operations')['items'])>=len(registered)
                break
            except Exception:
                if attempt==59:raise
                time.sleep(.25)
        after={r['id']:r['digest'] for r in get('/api/runs')}
        assert all(after.get(k)==v for k,v in before.items())
        store=configured_store(env)
        try:
            for table,old in historical.items():
                current={(r['job_id'],r['attempt']):r['digest'] for r in store.repository.rows('SELECT job_id,attempt,digest FROM '+table)}
                assert all(current.get(k)==v for k,v in old.items())
        finally:store.close()
    except Exception:
        if activation_started:
            old_caddyfile=old_root.parent/'deploy/Caddyfile'
            old_has_auth=old_caddyfile.is_file() and 'TRACE_HUNTER_AUTH_CONFIG' in old_caddyfile.read_text()
            restore_services(saved,worker_states,auth_fallback=(web_override,web_override_raw) if auth_enabled and not old_has_auth else None)
        raise
    record={**frozen,'web_config':str(caddyfile),'browser_auth_enabled':auth_enabled,
        'authenticated_entry_verified':bool(auth_enabled and auth_header),'api_root':str(ROOT),
        'web_root':str(ROOT/'dist'),'worker_root':str(ROOT),'invocation_worker_root':str(ROOT),
        'content_directory':env['TRACE_HUNTER_CONTENT_DIR'],'backfill':backfilled,
        'span_index_rebuild':rebuilt,'registered_operations':len(registered),
        'previous_web_root':str(old_root),'backup':str(backup),'existing_runs_preserved':len(before),'runs_after':len(after),
        'historical_results_preserved':{table:len(rows) for table,rows in historical.items()}}
    atomic(ROOT/'release.json',(json.dumps(record,indent=2)+'\n').encode())
    atomic(DATA/'platform-release.json',(json.dumps(record,indent=2)+'\n').encode())
    print(json.dumps(record))

if __name__=='__main__':
    prepare()
    if sys.argv[1]=='verify':verify()
    elif sys.argv[1]=='activate':activate(*sys.argv[2:4])
    else:raise ValueError('Expected verify or activate COMMIT INDEX_SHA256')
