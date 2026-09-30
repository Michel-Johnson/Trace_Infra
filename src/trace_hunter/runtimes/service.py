"""Versioned runtime connections and explicit, immutable capability observations."""
import asyncio
import base64
from functools import partial
from anyio.to_thread import run_sync
import binascii
import hashlib
import json
import time
from sqlalchemy import text

from ..access import Principal
from ..catalog import canonical
from ..content import ContentCorruption
from ..database import Conflict
from ..invocations import Invocations
from ..invocations.execution import _now_ms, _timestamp
from ..invocations.service import _actor, _url_token, operation_ref
from ..resources import digest_json
from ..traces.service import identifier, utc_now
from .contracts import BINDING_VERSION, PROTOCOL, binding_config, capabilities, revision_number
from .http import CapabilityReader, ProbeFailure

WHERE = 'project_id=:project AND runtime_id=:runtime'
VERSION_WHERE = WHERE+' AND revision=:revision'


def _page_options(limit,cursor,context):
    if type(limit) is not int or not 1<=limit<=100:raise ValueError('limit must be 1 to 100')
    if cursor is None:return None
    try:
        if not isinstance(cursor,str) or not 1<=len(cursor)<=4096:raise ValueError()
        payload=json.loads(base64.b64decode(cursor,altchars=b'-_',validate=True))
        if payload['context']!=digest_json(context):raise ValueError()
        return _url_token(payload['after'],'cursor runtime')
    except (ValueError,TypeError,KeyError,UnicodeError,binascii.Error):raise ValueError('Invalid runtime cursor') from None


def _cursor(runtime_id,context):
    return base64.urlsafe_b64encode(canonical({'context':digest_json(context),'after':runtime_id})[0].encode()).decode()


class RuntimeBindings:
    def __init__(self,repository,reader=None):
        self.repository=repository
        self.invocations=Invocations(repository)
        self.reader=reader if reader is not None else CapabilityReader()

    @staticmethod
    def _describe(row):
        try:
            spec=json.loads(row['spec'])
            if (digest_json(spec)!=row['spec_digest'] or spec['schema_version']!='trace-hunter/runtime-binding-spec/1'
                or spec['project_id']!=row['project_id'] or spec['runtime_id']!=row['runtime_id']
                or spec['config']['enabled']!=bool(row['enabled'])):raise ValueError()
        except (TypeError,ValueError,KeyError,UnicodeError):raise ContentCorruption('Runtime binding does not match its identity') from None
        return {'schema_version':BINDING_VERSION,'project_id':row['project_id'],'runtime_id':row['runtime_id'],
            'revision':row['revision'],'ref':{'kind':'runtime_binding','id':row['runtime_id'],'revision':row['revision'],'digest':row['spec_digest']},
            'config':spec['config'],'created_by':{'kind':row['actor_kind'],'principal_id':row['actor_principal_id']},'created_at':row['created_at']}

    def append(self,project_id,runtime_id,config,*,expected_previous,request_key,actor=None):
        identifier(project_id,'project_id');_url_token(runtime_id,'runtime_id');identifier(request_key,'request_key')
        previous=revision_number(expected_previous,previous=True);config=binding_config(config)
        created_by=_actor(actor,project_id)
        spec={'schema_version':'trace-hunter/runtime-binding-spec/1','project_id':project_id,'runtime_id':runtime_id,'config':config}
        digest=digest_json(spec);fingerprint=digest_json({'spec_digest':digest,'previous':previous})
        params={'project':project_id,'runtime':runtime_id,'revision':previous+1,'key':request_key,
                'key_hash':hashlib.sha256(request_key.encode()).hexdigest(),'fingerprint':fingerprint}
        with self.repository.engine.begin() as db:
            self.invocations._require_project(db,project_id)
            reserved=db.execute(text('''INSERT INTO runtime_binding_requests(project_id,request_key_hash,request_key,fingerprint,runtime_id,revision)
                VALUES(:project,:key_hash,:key,:fingerprint,:runtime,:revision) ON CONFLICT(project_id,request_key_hash) DO NOTHING'''),params).rowcount
            if not reserved:
                old=db.execute(text('SELECT * FROM runtime_binding_requests WHERE project_id=:project AND request_key_hash=:key_hash'),params).mappings().one()
                if old['request_key']!=request_key or old['fingerprint']!=fingerprint:raise Conflict('Request key already binds another runtime revision')
                row=db.execute(text('SELECT * FROM runtime_bindings WHERE '+VERSION_WHERE),{**params,'runtime':old['runtime_id'],'revision':old['revision']}).mappings().one()
                return {'created':False,'binding':self._describe(row)}
            db.execute(text('''INSERT INTO runtime_heads(project_id,runtime_id,current_revision) VALUES(:project,:runtime,0)
                ON CONFLICT(project_id,runtime_id) DO NOTHING'''),params)
            lock=' FOR UPDATE' if self.repository.postgres else ''
            head=db.execute(text('SELECT current_revision FROM runtime_heads WHERE '+WHERE+lock),params).scalar_one()
            if head!=previous:raise Conflict('Runtime binding advanced to another revision')
            db.execute(text('''INSERT INTO runtime_bindings(project_id,runtime_id,revision,spec_digest,spec,enabled,actor_kind,actor_principal_id,created_at)
                VALUES(:project,:runtime,:revision,:digest,:spec,:enabled,:kind,:actor,:at)'''),
                {**params,'digest':digest,'spec':canonical(spec)[0],'enabled':int(config['enabled']),
                 'kind':created_by['kind'],'actor':created_by['principal_id'],'at':utc_now()})
            db.execute(text('UPDATE runtime_heads SET current_revision=:revision WHERE '+WHERE),params)
            row=db.execute(text('SELECT * FROM runtime_bindings WHERE '+VERSION_WHERE),params).mappings().one()
            return {'created':True,'binding':self._describe(row)}

    def get(self,project_id,runtime_id,revision=None):
        identifier(project_id,'project_id');_url_token(runtime_id,'runtime_id')
        params={'project':project_id,'runtime':runtime_id}
        condition=' AND revision=(SELECT current_revision FROM runtime_heads h WHERE h.project_id=runtime_bindings.project_id AND h.runtime_id=runtime_bindings.runtime_id)'
        if revision is not None:
            params['revision']=revision_number(revision);condition=' AND revision=:revision'
        rows=self.repository.rows('SELECT * FROM runtime_bindings WHERE '+WHERE+condition+' ORDER BY revision DESC LIMIT 1',params)
        if not rows:raise KeyError('Runtime binding not found')
        return self._describe(rows[0])

    def history(self,project_id,runtime_id,*,limit=50,before=None):
        self.get(project_id,runtime_id)
        if type(limit) is not int or not 1<=limit<=100:raise ValueError('limit must be 1 to 100')
        if before is not None:revision_number(before)
        params={'project':project_id,'runtime':runtime_id,'limit':limit+1,'before':before}
        rows=self.repository.rows('SELECT * FROM runtime_bindings WHERE '+WHERE+(' AND revision<:before' if before is not None else '')+
            ' ORDER BY revision DESC LIMIT :limit',params)
        return {'items':[self._binding_summary(self._describe(row)) for row in rows[:limit]],
                'next_before':rows[limit-1]['revision'] if len(rows)>limit else None}

    @staticmethod
    def _binding_summary(binding):
        return {key:binding[key] for key in ('project_id','runtime_id','revision','ref','created_at')}|{
            'name':binding['config']['name'],'enabled':binding['config']['enabled']}

    @staticmethod
    def _probe_summary(row):
        return {'project_id':row['project_id'],'runtime_id':row['runtime_id'],'revision':row['revision'],'sequence':row['sequence'],
            'state':row['state'],'started_at':_timestamp(row['started_ms']),'completed_at':_timestamp(row['completed_ms']),
            'elapsed_ms':row['elapsed_ms'],'document_digest':row['document_digest'],
            'protocol_supported':None if row['protocol_supported'] is None else bool(row['protocol_supported']),'operation_count':row['operation_count'],
            'failure_code':row['failure_code'],'http_status':row['http_status'],
            'requested_by':{'kind':row['actor_kind'],'principal_id':row['actor_principal_id']},'verification':'advertised'}

    def _begin_probe(self,binding,actor):
        project_id=binding['project_id'];runtime_id=binding['runtime_id'];revision=binding['revision']
        params={'project':project_id,'runtime':runtime_id,'revision':revision}
        with self.repository.engine.begin() as db:
            if not self.repository.postgres:
                db.execute(text('UPDATE runtime_bindings SET enabled=enabled WHERE '+VERSION_WHERE),params)
            lock=' FOR UPDATE' if self.repository.postgres else ''
            row=db.execute(text('SELECT * FROM runtime_bindings WHERE '+VERSION_WHERE+lock),params).mappings().one()
            if self._describe(row)!=binding:raise ContentCorruption('Runtime changed before probe')
            sequence=db.execute(text('SELECT COALESCE(MAX(sequence),0)+1 FROM runtime_probes WHERE '+VERSION_WHERE),params).scalar_one()
            revision_number(sequence)
            db.execute(text('''INSERT INTO runtime_probes(project_id,runtime_id,revision,sequence,state,started_ms,protocol_supported,actor_kind,actor_principal_id)
                VALUES(:project,:runtime,:revision,:sequence,'incomplete',:now,NULL,:kind,:actor)'''),
                {**params,'sequence':sequence,'now':_now_ms(db),'kind':'operator' if actor.is_operator else 'service','actor':actor.principal_id})
        return sequence

    def _prepare_probe(self,project_id,runtime_id,revision,actor):
        if not isinstance(actor,Principal):raise ValueError('Probe requires an authenticated principal')
        actor.require(project_id,'runtimes:probe')
        binding=self.get(project_id,runtime_id,revision);sequence=self._begin_probe(binding,actor)
        return binding,{'project':project_id,'runtime':runtime_id,'revision':revision,'sequence':sequence}

    def probe(self,project_id,runtime_id,revision,*,actor):
        return asyncio.run(self.aprobe(project_id,runtime_id,revision,actor=actor))

    async def aprobe(self,project_id,runtime_id,revision,*,actor):
        binding,params=await run_sync(partial(self._prepare_probe,project_id,runtime_id,revision,actor))
        started=time.monotonic();document=None;error=None;http_status=None
        try:
            document=await self.reader.aread(binding['config'])
            document=capabilities(document)
            http_status=200
        except ProbeFailure as failure:
            document=None;error=failure.code;http_status=failure.http_status
        except (ValueError,TypeError,KeyError):
            document=None;error='invalid_document'
        elapsed=round((time.monotonic()-started)*1000)
        return await run_sync(partial(self._finish_probe,params,document,error,http_status,elapsed))

    def _finish_probe(self,params,document,error,http_status,elapsed):
        with self.repository.engine.begin() as db:
            if document is not None:
                for operation in document['operations']:
                    db.execute(text('''INSERT INTO runtime_advertised_operations(project_id,runtime_id,revision,sequence,operation_id,operation_version,operation_digest)
                        VALUES(:project,:runtime,:revision,:sequence,:operation,:version,:digest)'''),
                        {**params,'operation':operation['operation_id'],'version':operation['version'],'digest':operation['digest']})
            db.execute(text('''UPDATE runtime_probes SET state=:state,completed_ms=:now,elapsed_ms=:elapsed,document=:document,
                document_digest=:digest,protocol_supported=:supported,operation_count=:count,failure_code=:error,http_status=:http
                WHERE project_id=:project AND runtime_id=:runtime AND revision=:revision AND sequence=:sequence AND state='incomplete' '''),
                {**params,'state':'succeeded' if document is not None else 'failed','now':_now_ms(db),'elapsed':elapsed,
                 'document':canonical(document)[0] if document is not None else None,'digest':digest_json(document) if document is not None else None,
                 'supported':None if document is None else int(PROTOCOL in document['protocols']),
                 'count':len(document['operations']) if document is not None else None,'error':error,'http':http_status})
        return self.get_probe(params['project'],params['runtime'],params['revision'],params['sequence'])

    def get_probe(self,project_id,runtime_id,revision,sequence):
        self.get(project_id,runtime_id,revision);revision_number(sequence)
        rows=self.repository.rows('SELECT * FROM runtime_probes WHERE '+VERSION_WHERE+' AND sequence=:sequence',
            {'project':project_id,'runtime':runtime_id,'revision':revision,'sequence':sequence})
        if not rows:raise KeyError('Runtime probe not found')
        row=rows[0];value=self._probe_summary(row);document=None
        if row['document'] is not None:
            try:
                document=json.loads(row['document'])
                if digest_json(document)!=row['document_digest']:raise ValueError()
            except (TypeError,ValueError,UnicodeError):raise ContentCorruption('Runtime capability document is damaged') from None
        return {**value,'capabilities':document}

    def probes(self,project_id,runtime_id,revision,*,limit=50,before=None):
        self.get(project_id,runtime_id,revision)
        if type(limit) is not int or not 1<=limit<=100:raise ValueError('limit must be 1 to 100')
        if before is not None:revision_number(before)
        rows=self.repository.rows('SELECT * FROM runtime_probes WHERE '+VERSION_WHERE+(' AND sequence<:before' if before is not None else '')+
            ' ORDER BY sequence DESC LIMIT :limit',{'project':project_id,'runtime':runtime_id,'revision':revision,'before':before,'limit':limit+1})
        return {'items':[self._probe_summary(row) for row in rows[:limit]],'next_before':rows[limit-1]['sequence'] if len(rows)>limit else None}

    def listing(self,project_id,*,limit=50,cursor=None,max_age_seconds=300,operation=None):
        identifier(project_id,'project_id')
        if type(max_age_seconds) is not int or not 1<=max_age_seconds<=3600:raise ValueError('max_age_seconds must be 1 to 3600')
        if operation is not None:
            operation=operation_ref(operation)
            installed=self.invocations.get_operation(project_id,operation['operation_id'],operation['version'])
            if installed['ref']!=operation:raise Conflict('Operation digest does not match the registered version')
        context={'project':project_id,'operation':operation,'max_age_seconds':max_age_seconds}
        after=_page_options(limit,cursor,context)
        ordered='r.runtime_id COLLATE "C"' if self.repository.postgres else 'r.runtime_id COLLATE BINARY'
        where=['r.project_id=:project'];params={'project':project_id,'limit':limit+1,'after':after}
        if after is not None:where.append(ordered+'>:after')
        with self.repository.engine.connect() as db:
            now=_now_ms(db);params['cutoff']=now-max_age_seconds*1000
            if operation is not None:
                where+=['r.enabled=1',"p.state='succeeded'",'p.protocol_supported=1','p.completed_ms>=:cutoff',
                    '''EXISTS(SELECT 1 FROM runtime_advertised_operations a WHERE a.project_id=r.project_id AND a.runtime_id=r.runtime_id
                       AND a.revision=r.revision AND a.sequence=p.sequence AND a.operation_id=:op AND a.operation_version=:version AND a.operation_digest=:digest)''']
                params.update(op=operation['operation_id'],version=operation['version'],digest=operation['digest'])
            rows=db.execute(text('''SELECT r.*,p.sequence AS probe_sequence,p.state AS probe_state,p.completed_ms AS probe_completed,
                    p.protocol_supported AS probe_supported,p.operation_count AS probe_count,p.failure_code AS probe_error
                FROM runtime_heads h JOIN runtime_bindings r ON r.project_id=h.project_id AND r.runtime_id=h.runtime_id AND r.revision=h.current_revision
                LEFT JOIN runtime_probes p ON p.project_id=r.project_id AND p.runtime_id=r.runtime_id AND p.revision=r.revision
                    AND p.sequence=(SELECT MAX(q.sequence) FROM runtime_probes q WHERE q.project_id=r.project_id AND q.runtime_id=r.runtime_id AND q.revision=r.revision AND q.state<>'incomplete')
                WHERE '''+' AND '.join(where)+' ORDER BY '+ordered+' LIMIT :limit'),params).mappings().all()
        items=[]
        for row in rows[:limit]:
            observed=row['probe_completed'];age=None if observed is None else max(0,now-observed)
            items.append({**self._binding_summary(self._describe(row)),'observation':{
                'sequence':row['probe_sequence'],'state':row['probe_state'] or 'unobserved','completed_at':_timestamp(observed),
                'age_ms':age,'fresh':age is not None and age<=max_age_seconds*1000,
                'protocol_supported':None if row['probe_supported'] is None else bool(row['probe_supported']),'operation_count':row['probe_count'],
                'failure_code':row['probe_error'],'verification':'advertised'}})
        return {'items':items,'next_cursor':_cursor(rows[limit-1]['runtime_id'],context) if len(rows)>limit else None,
                'consistency':'live_keyset','max_age_seconds':max_age_seconds,'operation':operation}
