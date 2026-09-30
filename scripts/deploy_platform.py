"""Stage the split platform, then perform a checked first SQLite-to-Postgres cutover."""
import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from sqlalchemy.engine import make_url

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from trace_hunter.database import Repository
from trace_hunter.migration import migrate_sqlite

HOST='10.37.195.183'
DATA=Path.home()/'apps/trace-hunter-data'
OLD=Path.home()/'apps/trace-hunter'
BIN=Path.home()/'apps/trace-hunter-postgres-18.6/bin'


def run(*args, **kwargs):
    return subprocess.run(args,check=True,**kwargs)


def read_env(path):
    return dict(line.split('=',1) for line in path.read_text().splitlines() if line and not line.startswith('#'))


def write_env(path, values):
    temp=path.with_suffix('.new')
    fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
    with os.fdopen(fd,'w') as f:f.write(''.join(k+'='+v+'\n' for k,v in values.items()))
    temp.replace(path)


def fetch(path):
    with urllib.request.urlopen(path,timeout=5) as response:return json.load(response)


def ready(port,host='127.0.0.1'):
    for attempt in range(30):
        try:
            if fetch(f'http://{host}:{port}/api/health')['database']=='postgresql':return
        except (OSError,ValueError,KeyError):pass
        time.sleep(0.5)
    raise RuntimeError('API readiness check failed')


def snapshot(label):
    folder=DATA/'backups';folder.mkdir(exist_ok=True,mode=0o700)
    path=folder/(label+'-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')+'.sqlite')
    with sqlite3.connect((OLD/'var/platform.sqlite').as_uri()+'?mode=ro',uri=True) as source:
        with sqlite3.connect(path) as destination:source.backup(destination)
    path.chmod(0o600)
    return path


def migrate(source,url,label):
    repo=Repository(url)
    try:
        repo.migrate();result=migrate_sqlite(source,repo)
    finally:repo.close()
    (DATA/(label+'-migration.json')).write_text(json.dumps(result,indent=2)+'\n')
    return result


def verify_runs(source,base):
    with sqlite3.connect(source) as db:expected=dict(db.execute('SELECT id,digest FROM runs'))
    actual={r['id']:r['digest'] for r in fetch(base+'/api/runs')}
    if actual!=expected:raise RuntimeError('Run digest verification failed')


def stage():
    url=make_url(read_env(DATA/'database.env')['DATABASE_URL'])
    name='trace_hunter_stage'
    exists=run(str(BIN/'psql'),'-h',str(DATA/'socket'),'-p','55432','-d','postgres','-Atc',"SELECT 1 FROM pg_database WHERE datname='trace_hunter_stage'",capture_output=True,text=True).stdout.strip()
    if not exists:run(str(BIN/'createdb'),'-h',str(DATA/'socket'),'-p','55432','-O','trace_hunter',name)
    source=snapshot('stage')
    report=migrate(source,url.set(database=name).render_as_string(hide_password=False),'stage')
    api={'DATABASE_URL':url.set(database=name).render_as_string(hide_password=False),'API_HOST':'127.0.0.1','API_PORT':'18767','ALLOWED_ORIGINS':f'http://{HOST}:18766'}
    web={'SITE_ADDRESS':f'http://{HOST}:18766','WEB_HOST':HOST,'WEB_ROOT':str(ROOT/'apps/web/dist'),'API_UPSTREAM':'127.0.0.1:18767'}
    write_env(DATA/'api.env',api);write_env(DATA/'web.env',web)
    run(str(Path.home()/'apps/trace-hunter-tools/caddy'),'validate','--config',str(ROOT/'deploy/Caddyfile'),'--adapter','caddyfile',env={**os.environ,**web})
    for name in ('trace-hunter-api','trace-hunter-web'):
        (Path.home()/'.config/systemd/user'/(name+'.service')).write_text((ROOT/'deploy/services'/(name+'.service')).read_text())
    run('systemctl','--user','daemon-reload')
    run('systemctl','--user','enable','trace-hunter-api')
    run('systemctl','--user','restart','trace-hunter-api')
    ready(18767)
    run('systemctl','--user','enable','trace-hunter-web')
    run('systemctl','--user','restart','trace-hunter-web')
    ready(18766,HOST)
    verify_runs(source,f'http://{HOST}:18766')
    print(json.dumps({'stage_url':f'http://{HOST}:18766','migration':report}))


def cutover():
    if (DATA/'activated.json').exists():raise RuntimeError('Already activated; use the documented update workflow')
    ready(18766,HOST)
    original_api=read_env(DATA/'api.env');original_web=read_env(DATA/'web.env')
    production=read_env(DATA/'database.env')['DATABASE_URL']
    run('systemctl','--user','stop','trace-hunter')
    try:
        source=snapshot('cutover')
        report=migrate(source,production,'production')
        write_env(DATA/'api.env',{'API_HOST':'127.0.0.1','API_PORT':'8767','ALLOWED_ORIGINS':f'http://{HOST}:8766'})
        write_env(DATA/'web.env',{**original_web,'SITE_ADDRESS':f'http://{HOST}:8766','API_UPSTREAM':'127.0.0.1:8767'})
        run('systemctl','--user','restart','trace-hunter-api')
        ready(8767)
        run('systemctl','--user','restart','trace-hunter-web')
        ready(8766,HOST)
        verify_runs(source,f'http://{HOST}:8766')
        run('systemctl','--user','disable','trace-hunter')
        receipt={'activated_at':datetime.now(timezone.utc).isoformat(),'url':f'http://{HOST}:8766','sqlite_backup':str(source),'migration':report}
        (DATA/'activated.json').write_text(json.dumps(receipt,indent=2)+'\n')
        print(json.dumps(receipt))
    except Exception:
        run('systemctl','--user','stop','trace-hunter-web')
        write_env(DATA/'api.env',original_api);write_env(DATA/'web.env',original_web)
        run('systemctl','--user','start','trace-hunter')
        run('systemctl','--user','restart','trace-hunter-api','trace-hunter-web')
        raise


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('mode',choices=['stage','cutover']);args=parser.parse_args()
    stage() if args.mode=='stage' else cutover()
