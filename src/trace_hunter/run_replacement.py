"""Audited one-off replacement of normalized runs; never starts analysis.

Callers arrange a maintenance window and full database backup. This module makes
its own private affected-row backup, then performs an atomic replacement with FKs
active. The plan intentionally contains private records and must not be logged.
"""
import copy
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path

from sqlalchemy import bindparam, text
from .catalog import canonical, validate_catalog
from .database import Conflict
from .identity import metadata
from .import_bundle import MAX_IMPORT_BYTES, safe_file
from .protocol import InvalidTrace, validate

LOCK_TABLES=('runs','analyses','collections','case_definitions','collection_cases',
    'evaluation_batches','evaluation_jobs','evaluation_attempts','evaluation_results',
    'plugin_batches','plugin_invocations','plugin_attempts','plugin_artifacts','collection_classifications')
DEFINITIONS=('collections','case_definitions','collection_cases')


def digest(raw):return hashlib.sha256(raw).hexdigest()
def now():return datetime.now(timezone.utc).isoformat()
def sorted_rows(rows):return sorted((dict(row) for row in rows),key=lambda row:canonical(row)[0])


def selected(db,table,column,values):
    if not values:return []
    statement=text(f'SELECT * FROM {table} WHERE {column} IN :values').bindparams(bindparam('values',expanding=True))
    return sorted_rows(db.execute(statement,{'values':list(values)}).mappings())


def remove(db,table,column,values):
    if not values:return 0
    statement=text(f'DELETE FROM {table} WHERE {column} IN :values').bindparams(bindparam('values',expanding=True))
    return db.execute(statement,{'values':list(values)}).rowcount


def load_bundle(root,expected_new_version,expected_count):
    root=Path(root).resolve()
    if (root/'INCOMPLETE').exists():raise ValueError('Bundle preparation is incomplete')
    manifest_raw=safe_file(root,'manifest.json').read_bytes();manifest=json.loads(manifest_raw)
    if manifest.get('state')!='ready':raise ValueError('Bundle is not ready')
    if manifest.get('conversations')!=expected_count:raise ValueError('Bundle conversation count does not match expected count')
    names=set();traces={};ids=set();files=[]
    for entry in manifest['import_files']:
        name=entry['path']
        if name in names:raise ValueError('Duplicate import path')
        names.add(name);path=safe_file(root,name)
        if path.stat().st_size>MAX_IMPORT_BYTES:raise ValueError('Import file exceeds 16 MiB')
        raw=path.read_bytes()
        if len(raw)!=entry['bytes'] or digest(raw)!=entry['sha256']:raise ValueError('Import file bytes/hash mismatch')
        value=json.loads(raw);files.append({'path':name,'bytes':len(raw),'sha256':digest(raw)})
        try:
            if value.get('schema_version')=='trace-hunter/catalog/1.0':
                validate_catalog(value);continue  # Existing catalog is intentionally not imported.
            validate(value)
        except InvalidTrace:
            # Schema diagnostics may embed raw source values; keep CLI output private.
            raise ValueError('Bundle contains a document that fails protocol validation') from None
        run=metadata(value)['run']
        if value['collector']['version']!=expected_new_version:raise ValueError('Unexpected new collector version')
        if entry.get('run_id') not in (None,run['id']) or entry.get('query_id') not in (None,run['query_id']):raise ValueError('Manifest identity mismatch')
        if run['id'] in ids or run['query_id'] in traces:raise ValueError('New bundle must contain one distinct run per query_id')
        ids.add(run['id']);traces[run['query_id']]=value
    if len(traces)!=expected_count:raise ValueError('New trace count does not match expected count')
    return traces,{'root':str(root),'manifest_sha256':digest(manifest_raw),'files':files}


def stable_projection(trace):
    value=copy.deepcopy(trace)
    value['run'].pop('id')
    value['collector'].pop('version');value.pop('evidence')
    for span in value['spans']:
        for key in ('input','output','status'):span.pop(key,None)
    return value


def validate_pair(old,new,old_version,new_version):
    if old['collector']['version']!=old_version or new['collector']['version']!=new_version:
        raise ValueError('Unexpected old/new collector version')
    if old['run']['id']==new['run']['id']:raise ValueError('Replacement requires a new run ID')
    if stable_projection(old)!=stable_projection(new):
        raise ValueError('Replacement changed preserved query/environment/source/span/time/order fields')
    evidence={entry['id']:entry for entry in new['evidence']}
    if any(evidence.get(entry['id'])!=entry for entry in old['evidence']):
        raise ValueError('Existing evidence must remain unchanged; additions are allowed')


def snapshot(db,collection_id,new_queries):
    definitions={name:sorted_rows(db.execute(text('SELECT * FROM '+name)).mappings()) for name in DEFINITIONS}
    collection=next((row for row in definitions['collections'] if row['id']==collection_id),None)
    if collection is None:raise ValueError('Collection does not exist')
    catalog=json.loads(collection['payload']);queries={case['query_id'] for case in catalog['cases']}
    linked={row['query_id'] for row in definitions['collection_cases'] if row['collection_id']==collection_id}
    if queries!=set(new_queries) or linked!=queries:raise ValueError('Existing collection Case set must exactly match new bundle queries')
    all_rows=sorted_rows(db.execute(text('SELECT * FROM runs')).mappings())
    old=[];by_query={}
    for row in all_rows:
        if digest(row['payload'].encode())!=row['digest']:raise Conflict('Stored run payload digest mismatch')
        query=metadata(json.loads(row['payload']))['run']['query_id']
        if query in queries:
            if query in by_query:raise ValueError('Every target Case must contain exactly one old run')
            by_query[query]=row;old.append(row)
    if set(by_query)!=queries:raise ValueError('Every target Case must contain exactly one old run')
    old_ids={row['id'] for row in old};old_digests={row['digest'] for row in old}
    rows={'runs':sorted_rows(old),'analyses':selected(db,'analyses','digest',old_digests)}
    for batches,jobs,attempts,results in [('evaluation_batches','evaluation_jobs','evaluation_attempts','evaluation_results'),
                                         ('plugin_batches','plugin_invocations','plugin_attempts','plugin_artifacts')]:
        rows[jobs]=selected(db,jobs,'run_id',old_ids)
        if any(row['state'] in ('queued','running') for row in rows[jobs]):raise ValueError('Target run has queued/running tasks; quiesce them before replacement')
        job_ids={row['id'] for row in rows[jobs]}
        rows[attempts]=selected(db,attempts,'job_id',job_ids)
        rows[results]=selected(db,results,'job_id',job_ids)
        rows[batches]=selected(db,batches,'id',{row['batch_id'] for row in rows[jobs]})
    rows['collection_classifications']=[]
    for row in db.execute(text('SELECT * FROM collection_classifications')).mappings():
        dispatch=json.loads(row['payload']);run_ids={item[0] for item in dispatch['runs']}
        if run_ids&old_ids:
            if run_ids-old_ids:raise ValueError('A collection dispatch mixes target and other runs; replacement refused')
            rows['collection_classifications'].append(dict(row))
    rows['collection_classifications']=sorted_rows(rows['collection_classifications'])
    return {'all_runs':{row['id']:row['digest'] for row in all_rows},'definitions':definitions,'rows':rows}


@dataclass(repr=False)
class ReplacementPlan:
    collection_id:str
    expected_old_version:str
    expected_new_version:str
    expected_count:int
    bundle:dict=field(repr=False)
    before:dict=field(repr=False)
    replacements:list=field(repr=False)
    planned_at:str

    def private_value(self):return {'schema_version':'trace-hunter/run-replacement/1.0',**self.__dict__}
    @property
    def sha256(self):return canonical(self.private_value())[1]
    def summary(self):
        return {'collection_id':self.collection_id,'plan_sha256':self.sha256,'planned_at':self.planned_at,
            'old_collector_version':self.expected_old_version,'new_collector_version':self.expected_new_version,
            'replace_count':len(self.replacements),'other_runs_preserved':len(self.before['all_runs'])-len(self.replacements),
            'affected_rows':{table:len(rows) for table,rows in self.before['rows'].items()},
            'replacements':self.replacements,'bundle_manifest_sha256':self.bundle['manifest_sha256']}


def plan(store,bundle_root,collection_id,*,expected_old_version,expected_new_version,expected_count=50):
    if type(expected_count) is not int or expected_count<1:raise ValueError('Expected count must be a positive integer')
    if not expected_old_version or not expected_new_version or expected_old_version==expected_new_version:
        raise ValueError('Explicit distinct old/new collector versions are required')
    traces,bundle=load_bundle(bundle_root,expected_new_version,expected_count)
    with store.repository.engine.connect() as db:
        if store.repository.postgres:db=db.execution_options(isolation_level='REPEATABLE READ')
        with db.begin():
            if not store.repository.postgres:db.exec_driver_sql('BEGIN')
            before=snapshot(db,collection_id,traces)
    replacements=[]
    for row in before['rows']['runs']:
        old=json.loads(row['payload']);query=metadata(old)['run']['query_id'];new=traces[query]
        validate_pair(old,new,expected_old_version,expected_new_version)
        if new['run']['id'] in before['all_runs']:raise ValueError('New run ID already exists')
        replacements.append({'query_id':query,'old_id':row['id'],'old_digest':row['digest'],
                             'new_id':new['run']['id'],'new_digest':canonical(new)[1]})
    return ReplacementPlan(collection_id,expected_old_version,expected_new_version,expected_count,bundle,before,
                           sorted(replacements,key=lambda item:item['query_id']),now())


def write_private(path,raw):
    path=Path(path)
    with path.open('xb') as handle:
        os.fchmod(handle.fileno(),0o600);handle.write(raw);handle.flush();os.fsync(handle.fileno())


def save_plan(value,path):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    raw=canonical({'plan':value.private_value(),'sha256':value.sha256})[0].encode()
    write_private(path,raw)


def read_plan(path):
    value=json.loads(Path(path).read_bytes());payload=value['plan']
    if payload.get('schema_version')!='trace-hunter/run-replacement/1.0' or canonical(payload)[1]!=value['sha256']:
        raise ValueError('Replacement plan checksum mismatch')
    payload=dict(payload);payload.pop('schema_version')
    return ReplacementPlan(**payload)


def apply(store,value,backup_dir):
    traces,bundle=load_bundle(value.bundle['root'],value.expected_new_version,value.expected_count)
    if bundle!=value.bundle:raise Conflict('Bundle changed after planning')
    for item in value.replacements:
        if canonical(traces[item['query_id']])[1]!=item['new_digest']:raise Conflict('New trace changed after planning')
    repo=store.repository;backup_dir=Path(backup_dir)
    with repo.engine.connect() as db:
        try:
            if repo.postgres:
                db.begin();db.execute(text('LOCK TABLE '+','.join(LOCK_TABLES)+' IN SHARE ROW EXCLUSIVE MODE'))
            else:
                db.exec_driver_sql('PRAGMA foreign_keys=ON');db.exec_driver_sql('BEGIN IMMEDIATE')
            current=snapshot(db,value.collection_id,traces)
            if current!=value.before:raise Conflict('Database changed after planning; create a new plan')
            # Repeat semantic comparison under the same lock protecting the snapshot.
            by_query={metadata(json.loads(row['payload']))['run']['query_id']:row for row in current['rows']['runs']}
            for query,new in traces.items():validate_pair(json.loads(by_query[query]['payload']),new,value.expected_old_version,value.expected_new_version)
            backup_dir.mkdir(mode=0o700,parents=True,exist_ok=False)
            private_plan=value.private_value();private_plan.pop('before')
            raw=canonical({'snapshot':current,'plan':private_plan})[0].encode()
            write_private(backup_dir/'affected-rows.json',raw)
            manifest={'state':'prepared','plan_sha256':value.sha256,'created_at':now(),
                'files':[{'path':'affected-rows.json','bytes':len(raw),'sha256':digest(raw)}],**value.summary()}
            write_private(backup_dir/'manifest.json',canonical(manifest)[0].encode())
            for directory in (backup_dir,backup_dir.parent):
                descriptor=os.open(directory,os.O_RDONLY)
                try:os.fsync(descriptor)
                finally:os.close(descriptor)
            # Backups and their checksum manifest are durable before the first DB mutation.
            imported_at=now()
            for new in traces.values():
                payload,new_digest=canonical(new)
                if not repo.insert_run(db,{'id':new['run']['id'],'digest':new_digest,'imported_at':imported_at,'payload':payload}):
                    raise Conflict('New run unexpectedly exists')
            for batches,jobs,attempts,results in [('evaluation_batches','evaluation_jobs','evaluation_attempts','evaluation_results'),
                                                 ('plugin_batches','plugin_invocations','plugin_attempts','plugin_artifacts')]:
                job_ids=[row['id'] for row in current['rows'][jobs]]
                remove(db,results,'job_id',job_ids);remove(db,attempts,'job_id',job_ids);remove(db,jobs,'id',job_ids)
                for batch in current['rows'][batches]:
                    if not db.execute(text('SELECT id FROM '+jobs+' WHERE batch_id=:id'),{'id':batch['id']}).first():remove(db,batches,'id',[batch['id']])
            remove(db,'collection_classifications','request_key',[row['request_key'] for row in current['rows']['collection_classifications']])
            old_ids={item['old_id'] for item in value.replacements};remove(db,'runs','id',old_ids)
            for old_digest in {item['old_digest'] for item in value.replacements}:
                if not db.execute(text('SELECT id FROM runs WHERE digest=:digest'),{'digest':old_digest}).first():remove(db,'analyses','digest',[old_digest])
            actual={row['id']:row['digest'] for row in db.execute(text('SELECT id,digest FROM runs')).mappings()}
            expected={rid:d for rid,d in current['all_runs'].items() if rid not in old_ids}
            expected.update({item['new_id']:item['new_digest'] for item in value.replacements})
            if actual!=expected:raise Conflict('Replacement run invariants failed')
            after=snapshot(db,value.collection_id,traces)
            if after['definitions']!=current['definitions']:raise Conflict('Collection/Case definitions changed')
            if {row['id'] for row in after['rows']['runs']}!={item['new_id'] for item in value.replacements}:raise Conflict('Replacement Case mapping failed')
            db.commit()
        except Exception:
            db.rollback();raise
    return {'status':'replaced','backup_dir':str(backup_dir),**value.summary()}
