"""Durable attempts and fenced, atomic result publication.

A lease_id is an identifier, not a bearer credential: every executor mutation
also checks the authenticated project principal and attempt owner. Expiry never
silently reruns arbitrary actions; an explicit retry preserves the old attempt.
"""

from collections import Counter
from contextlib import nullcontext
from datetime import datetime, timezone
import hashlib
import json
import uuid

from sqlalchemy import text

from ..access import Principal
from ..actions.lifecycle import ensure_resolved, mark_unknown
from ..artifacts import Artifacts
from ..artifacts.service import MAX_CONTENT_BYTES, MIME
from ..catalog import canonical
from ..content import ContentCorruption
from ..database import Conflict
from ..notifications import publish
from ..resources import digest_json, json_object, namespace_token
from ..traces.service import MAX_REVISION, identifier
from .service import Invocations

WHERE = 'project_id=:project AND invocation_id=:id'
MAX_LEASE_SECONDS = 300


def _now_ms(db):
    sql = 'SELECT CAST(EXTRACT(EPOCH FROM clock_timestamp())*1000 AS BIGINT)' if db.dialect.name == 'postgresql' else "SELECT CAST((julianday('now')-2440587.5)*86400000 AS INTEGER)"
    return int(db.execute(text(sql)).scalar_one())


def _timestamp(value):
    return None if value is None else datetime.fromtimestamp(value/1000, timezone.utc).isoformat(timespec='milliseconds')


def _actor(actor, project_id, scope):
    if not isinstance(actor, Principal):
        raise ValueError('An authenticated principal is required')
    actor.require(project_id, scope)
    return {'kind': 'operator' if actor.is_operator else 'service', 'principal_id': actor.principal_id}


def _number(value, name, minimum=1):
    if type(value) is not int or not minimum <= value <= MAX_REVISION:
        raise ValueError(name + ' is outside the supported attempt range')
    return value


def _prepared_outputs(outputs):
    if not isinstance(outputs, list) or len(outputs)>100:
        raise ValueError('outputs must contain at most 100 entries')
    normalized, encoded, total = [], [], 0
    for item in outputs:
        if not isinstance(item, dict) or set(item) != {'role','content','media_type','metadata'}:
            raise ValueError('An output requires role, content bytes, media_type and metadata')
        role=namespace_token(item['role'],'output role')
        raw=item['content'];media=item['media_type']
        if not isinstance(raw,bytes):raise ValueError('Output content must be bytes')
        total+=len(raw)
        if total>MAX_CONTENT_BYTES:raise ValueError('Combined output content exceeds 8 MiB')
        if not isinstance(media,str) or len(media)>255 or not media.isascii() or not MIME.fullmatch(media):
            raise ValueError('Invalid output media_type')
        metadata=json_object(item['metadata'],'output metadata')
        normalized.append({'role':role,'content':raw,'media_type':media,'metadata':metadata})
        encoded.append({'role':role,'content':{'digest':'sha256:'+hashlib.sha256(raw).hexdigest(),'size_bytes':len(raw),'media_type':media},'metadata':metadata})
    return normalized,encoded


class InvocationExecution:
    def __init__(self, repository, content):
        self.repository=repository
        self.invocations=Invocations(repository)
        self.artifacts=Artifacts(repository,content)

    def _lock(self, db, project_id, invocation_id):
        identifier(project_id,'project_id');identifier(invocation_id,'invocation_id')
        params={'project':project_id,'id':invocation_id}
        if not self.repository.postgres:
            # SQLite's legacy driver starts a real writer here, before SELECT.
            db.execute(text('UPDATE invocations SET status=status WHERE '+WHERE),params)
        lock=' FOR UPDATE' if self.repository.postgres else ''
        row=db.execute(text('SELECT * FROM invocations WHERE '+WHERE+lock),params).mappings().first()
        if row is None:raise KeyError('Invocation not found')
        self.invocations._describe(row)
        return row,params

    @staticmethod
    def _latest(db,params):
        return db.execute(text('SELECT * FROM invocation_attempts WHERE '+WHERE+' ORDER BY attempt DESC LIMIT 1'),params).mappings().first()

    @staticmethod
    def _event(db,params,attempt,kind,actor,now):
        sequence=db.execute(text('SELECT COALESCE(MAX(sequence),0)+1 FROM invocation_events WHERE '+WHERE),params).scalar_one()
        db.execute(text('''INSERT INTO invocation_events(project_id,invocation_id,sequence,attempt,kind,actor_kind,actor_principal_id,recorded_ms)
            VALUES(:project,:id,:sequence,:attempt,:kind,:actor_kind,:actor_id,:now)'''),
            {**params,'sequence':sequence,'attempt':attempt,'kind':kind,'actor_kind':actor['kind'],'actor_id':actor['principal_id'],'now':now})
        if kind in ('claimed', 'cancelled', 'retry_requested', 'lease_expired'):
            publish(db, params['project'], 'invocation.' + kind, params['id'], attempt=attempt)

    @staticmethod
    def _attempt(row,now):
        return {'project_id':row['project_id'],'invocation_id':row['invocation_id'],'attempt':row['attempt'],
            'lease_id':row['lease_id'],'worker_id':row['worker_id'],'owner':{'kind':row['owner_kind'],'principal_id':row['owner_principal_id']},
            'status':row['status'],'lease_seconds':row['lease_seconds'],
            'lease_state':'inactive' if row['status']!='running' else 'active' if now<row['lease_expires_ms'] else 'expired',
            'started_at':_timestamp(row['started_ms']),'heartbeat_at':_timestamp(row['heartbeat_ms']),
            'lease_expires_at':_timestamp(row['lease_expires_ms']),'finished_at':_timestamp(row['finished_ms'])}

    @staticmethod
    def _owner(row,attempt,lease_id,actor):
        _number(attempt,'attempt');identifier(lease_id,'lease_id')
        if row is None or row['attempt']!=attempt or row['lease_id']!=lease_id:
            raise Conflict('Attempt or lease is no longer current')
        if row['owner_kind']!=actor['kind'] or row['owner_principal_id']!=actor['principal_id']:
            raise PermissionError('Attempt belongs to another executor')

    @staticmethod
    def _active(invocation,row,now):
        if invocation['status']!='running' or row['status']!='running' or now>=row['lease_expires_ms']:
            raise Conflict('Attempt is inactive or its lease expired')

    def claim(self,project_id,invocation_id,*,request_key,worker_id,lease_seconds=60,actor):
        owner=_actor(actor,project_id,'invocations:execute')
        identifier(request_key,'request_key');identifier(worker_id,'worker_id')
        if type(lease_seconds) is not int or not 1<=lease_seconds<=MAX_LEASE_SECONDS:raise ValueError('lease_seconds must be 1 to 300')
        fingerprint=digest_json({'owner':owner,'worker_id':worker_id,'lease_seconds':lease_seconds})
        with self.repository.engine.begin() as db:
            invocation,params=self._lock(db,project_id,invocation_id)
            key={**params,'key_hash':hashlib.sha256(request_key.encode()).hexdigest(),'key':request_key}
            previous=db.execute(text('SELECT * FROM invocation_claim_requests WHERE '+WHERE+' AND request_key_hash=:key_hash'),key).mappings().first()
            now=_now_ms(db)
            if previous is not None:
                if previous['request_key']!=request_key or previous['fingerprint']!=fingerprint:raise Conflict('Claim key already binds another executor or options')
                row=db.execute(text('SELECT * FROM invocation_attempts WHERE '+WHERE+' AND attempt=:attempt'),{**params,'attempt':previous['attempt']}).mappings().one()
                return {'created':False,'attempt':self._attempt(row,now)}
            latest=self._latest(db,params)
            if invocation['status']!='pending':raise Conflict('Invocation is not pending; expiry requires an explicit retry')
            number=_number(1 if latest is None else latest['attempt']+1,'attempt')
            row={'project_id':project_id,'invocation_id':invocation_id,'attempt':number,'lease_id':'lease_'+uuid.uuid4().hex,
                'worker_id':worker_id,'owner_kind':owner['kind'],'owner_principal_id':owner['principal_id'],'status':'running',
                'lease_seconds':lease_seconds,'started_ms':now,'heartbeat_ms':now,'lease_expires_ms':now+lease_seconds*1000,'finished_ms':None}
            db.execute(text('INSERT INTO invocation_attempts('+','.join(row)+') VALUES('+','.join(':'+key for key in row)+')'),row)
            db.execute(text('''INSERT INTO invocation_claim_requests(project_id,invocation_id,request_key_hash,request_key,fingerprint,attempt)
                VALUES(:project,:id,:key_hash,:key,:fingerprint,:attempt)'''),{**key,'fingerprint':fingerprint,'attempt':number})
            db.execute(text("UPDATE invocations SET status='running' WHERE "+WHERE),params)
            self._event(db,params,number,'claimed',owner,now)
            return {'created':True,'attempt':self._attempt(row,now)}

    def heartbeat(self,project_id,invocation_id,*,attempt,lease_id,actor,connection=None):
        owner=_actor(actor,project_id,'invocations:execute')
        with self.repository.engine.begin() if connection is None else nullcontext(connection) as db:
            invocation,params=self._lock(db,project_id,invocation_id);row=self._latest(db,params);now=_now_ms(db)
            self._owner(row,attempt,lease_id,owner);self._active(invocation,row,now)
            expiry=now+row['lease_seconds']*1000
            db.execute(text('UPDATE invocation_attempts SET heartbeat_ms=:now,lease_expires_ms=:expiry WHERE '+WHERE+' AND attempt=:attempt'),
                       {**params,'attempt':attempt,'now':now,'expiry':expiry})
            return self._attempt({**row,'heartbeat_ms':now,'lease_expires_ms':expiry},now)

    @staticmethod
    def _receipt(row):
        try:
            value=json.loads(row['receipt'])
            if (digest_json(value)!=row['receipt_digest'] or
                any(value.get(key)!=row[key] for key in ('project_id','invocation_id','attempt'))):raise ValueError()
        except (TypeError,ValueError,UnicodeError):raise ContentCorruption('Execution receipt does not match its digest') from None
        return {**value,'digest':row['receipt_digest']}

    def complete(self,project_id,invocation_id,*,attempt,lease_id,outputs,actor,connection=None):
        prepared,encoded=_prepared_outputs(outputs)
        return self._finish(project_id,invocation_id,attempt,lease_id,actor,'succeeded',prepared,
                            {'outcome':'succeeded','outputs':encoded},None,connection=connection)

    def fail(self,project_id,invocation_id,*,attempt,lease_id,error,actor,connection=None):
        if not isinstance(error,dict) or set(error)!={'code','details'}:raise ValueError('Failure requires code and details')
        error={'code':namespace_token(error['code'],'error code'),'details':json_object(error['details'],'error details')}
        return self._finish(project_id,invocation_id,attempt,lease_id,actor,'failed',[],{'outcome':'failed','error':error},error,connection=connection)

    def _finish(self,project_id,invocation_id,attempt,lease_id,actor,outcome,outputs,submission,error,*,connection=None):
        owner=_actor(actor,project_id,'invocations:execute');fingerprint=digest_json(submission)
        with self.repository.engine.begin() if connection is None else nullcontext(connection) as db:
            invocation,params=self._lock(db,project_id,invocation_id)
            _number(attempt,'attempt');identifier(lease_id,'lease_id')
            row=db.execute(text('SELECT * FROM invocation_attempts WHERE '+WHERE+' AND attempt=:attempt'),{**params,'attempt':attempt}).mappings().first()
            self._owner(row,attempt,lease_id,owner)
            previous=db.execute(text('SELECT * FROM invocation_results WHERE '+WHERE+' AND attempt=:attempt'),{**params,'attempt':attempt}).mappings().first()
            if previous is not None:
                if previous['submission_digest']!=fingerprint:raise Conflict('Attempt already binds a different result')
                return {'created':False,'result':self._receipt(previous)}
            latest=self._latest(db,params)
            if latest['attempt']!=attempt:raise Conflict('Attempt is no longer current')
            now=_now_ms(db);self._active(invocation,row,now)
            if outcome == 'succeeded':
                ensure_resolved(db, project_id, invocation_id)
            else:
                mark_unknown(db, project_id, invocation_id, attempt, reason='attempt_failed', actor=owner, now=now)
            value=self.invocations._describe(invocation)
            definition_row=db.execute(text('''SELECT * FROM operation_versions WHERE project_id=:project AND operation_id=:operation AND version=:version'''),
                {**params,'operation':value['operation']['operation_id'],'version':value['operation']['version']}).mappings().one()
            definition=self.invocations._describe_operation(definition_row)
            if definition['ref']!=value['operation']:raise ContentCorruption('Operation binding changed')
            roles={item['role']:item for item in definition['definition']['outputs']}
            counts=Counter(item['role'] for item in outputs)
            if outcome=='succeeded' and (set(counts)-roles.keys() or any(not rule['min_items']<=counts[role]<=rule['max_items'] for role,rule in roles.items())):
                raise ValueError('Output roles or counts do not satisfy the fixed operation')
            published=[]
            for position,item in enumerate(outputs):
                artifact=self.artifacts.publish(project_id,item['content'],media_type=item['media_type'],artifact_type=roles[item['role']]['artifact_type'],
                    inputs=value['inputs'],producer_claim={'name':value['operation']['operation_id'],'version':value['operation']['version']},
                    config=value['config'],metadata=item['metadata'],request_key=uuid.uuid4().hex,actor=actor,connection=db)['artifact']
                db.execute(text('''INSERT INTO invocation_outputs(project_id,invocation_id,attempt,output_position,role,artifact_id,descriptor_digest)
                    VALUES(:project,:id,:attempt,:position,:role,:artifact,:digest)'''),
                    {**params,'attempt':attempt,'position':position,'role':item['role'],'artifact':artifact['artifact_id'],'digest':artifact['descriptor_digest']})
                published.append({'position':position,'role':item['role'],'artifact':artifact['ref']})
            finished=_now_ms(db)
            receipt={'schema_version':'trace-hunter/invocation-result/1','project_id':project_id,'invocation_id':invocation_id,'attempt':attempt,
                'outcome':outcome,'outputs':published,'error':error,'reported_by':owner,'accepted_at':_timestamp(finished),'meaning':'execution_report_accepted'}
            receipt_digest=digest_json(receipt)
            db.execute(text('''INSERT INTO invocation_results(project_id,invocation_id,attempt,submission_digest,receipt_digest,receipt)
                VALUES(:project,:id,:attempt,:submission,:digest,:receipt)'''),
                {**params,'attempt':attempt,'submission':fingerprint,'digest':receipt_digest,'receipt':canonical(receipt)[0]})
            db.execute(text('UPDATE invocation_attempts SET status=:status,finished_ms=:now WHERE '+WHERE+' AND attempt=:attempt'),
                       {**params,'attempt':attempt,'status':outcome,'now':finished})
            db.execute(text('UPDATE invocations SET status=:status WHERE '+WHERE),{**params,'status':outcome})
            self._event(db,params,attempt,outcome,owner,finished)
            publish(db, project_id, 'invocation.result.accepted', invocation_id, attempt=attempt,
                    payload={'outcome': outcome, 'receipt_digest': receipt_digest})
            return {'created':True,'result':{**receipt,'digest':receipt_digest}}

    def cancel(self,project_id,invocation_id,*,expected_attempt,actor):
        return self._manage(project_id,invocation_id,expected_attempt,actor,'cancel')

    def retry(self,project_id,invocation_id,*,expected_attempt,actor):
        return self._manage(project_id,invocation_id,expected_attempt,actor,'retry')

    def _manage(self, project_id, invocation_id, expected_attempt, actor, action):
        manager = _actor(actor, project_id, 'invocations:write')
        _number(expected_attempt, 'expected_attempt', 0)
        with self.repository.engine.begin() as db:
            invocation, params = self._lock(db, project_id, invocation_id)
            latest = self._latest(db, params)
            now = _now_ms(db)
            number = 0 if latest is None else latest['attempt']
            if number != expected_attempt:
                raise Conflict('Invocation advanced to another attempt')
            if action == 'cancel':
                if invocation['status'] == 'cancelled':
                    return {'changed': False, 'status': 'cancelled', 'current_attempt': number}
                if invocation['status'] not in ('pending', 'running'):
                    raise Conflict('Completed invocation cannot be cancelled')
                state = 'cancelled'
                attempt_state = 'cancelled'
                mark_unknown(db, project_id, invocation_id, number, reason='attempt_cancelled', actor=manager, now=now)
            else:
                if invocation['status'] == 'pending':
                    return {'changed': False, 'status': 'pending', 'current_attempt': number}
                expired = latest is not None and latest['status'] == 'running' and (now >= latest['lease_expires_ms'])
                if invocation['status'] not in ('failed', 'cancelled', 'blocked') and (not expired):
                    raise Conflict('Invocation cannot be retried in its current state')
                ensure_resolved(db, project_id, invocation_id, retry=True)
                state = 'pending'
                attempt_state = 'expired' if expired else None
            if latest is not None and latest['status'] == 'running' and (attempt_state is not None):
                db.execute(text('UPDATE invocation_attempts SET status=:state,finished_ms=:now WHERE ' + WHERE + ' AND attempt=:attempt'), {**params, 'state': attempt_state, 'now': now, 'attempt': number})
            db.execute(text('UPDATE invocations SET status=:state WHERE ' + WHERE), {**params, 'state': state})
            self._event(db, params, number, 'cancelled' if action == 'cancel' else 'retry_requested', manager, now)
            return {'changed': True, 'status': state, 'current_attempt': number}

    def recover_expired(self, project_id, *, limit=100, actor):
        manager = _actor(actor, project_id, 'invocations:write')
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('limit must be 1 to 100')
        order = 'i.invocation_id COLLATE "C"' if self.repository.postgres else 'i.invocation_id COLLATE BINARY'
        with self.repository.engine.connect() as db:
            self.invocations._require_project(db, project_id)
            rows = db.execute(text("""SELECT i.invocation_id,a.attempt FROM invocations i JOIN invocation_attempts a
                ON a.project_id=i.project_id AND a.invocation_id=i.invocation_id
                AND a.attempt=(SELECT MAX(n.attempt) FROM invocation_attempts n WHERE n.project_id=i.project_id AND n.invocation_id=i.invocation_id)
                WHERE i.project_id=:project AND i.status='running' AND a.status='running' AND a.lease_expires_ms<=:now
                ORDER BY """+order+' LIMIT :limit'), {'project': project_id, 'now': _now_ms(db), 'limit': limit+1}).mappings().all()
        items = []
        for candidate in rows[:limit]:
            with self.repository.engine.begin() as db:
                invocation, params = self._lock(db, project_id, candidate['invocation_id'])
                attempt = self._latest(db, params); now = _now_ms(db)
                if (invocation['status'] != 'running' or attempt['status'] != 'running' or
                    attempt['attempt'] != candidate['attempt'] or now < attempt['lease_expires_ms']):
                    continue
                mark_unknown(db, project_id, candidate['invocation_id'], attempt['attempt'], reason='lease_expired', actor=manager, now=now)
                db.execute(text("UPDATE invocation_attempts SET status='expired',finished_ms=:now WHERE "+WHERE+' AND attempt=:attempt'),
                           {**params, 'attempt': attempt['attempt'], 'now': now})
                db.execute(text("UPDATE invocations SET status='blocked' WHERE "+WHERE), params)
                self._event(db, params, attempt['attempt'], 'lease_expired', manager, now)
                items.append({'invocation_id': candidate['invocation_id'], 'attempt': attempt['attempt'],
                              'status': 'blocked', 'reason': 'lease_expired'})
        return {'items': items, 'examined': min(len(rows), limit), 'more_candidates': len(rows) > limit, 'limit': limit}

    def attempts(self,project_id,invocation_id,*,limit=50,before=None):
        self.invocations.get(project_id,invocation_id)
        if type(limit) is not int or not 1<=limit<=100:raise ValueError('limit must be 1 to 100')
        if before is not None:_number(before,'before')
        params={'project':project_id,'id':invocation_id,'limit':limit+1,'before':before}
        condition='' if before is None else ' AND attempt<:before'
        with self.repository.engine.connect() as db:
            rows=db.execute(text('SELECT * FROM invocation_attempts WHERE '+WHERE+condition+' ORDER BY attempt DESC LIMIT :limit'),params).mappings().all()
            now=_now_ms(db)
        return {'items':[self._attempt(row,now) for row in rows[:limit]],'next_before':rows[limit-1]['attempt'] if len(rows)>limit else None}

    def result(self,project_id,invocation_id,attempt):
        self.invocations.get(project_id,invocation_id);_number(attempt,'attempt')
        rows=self.repository.rows('SELECT * FROM invocation_results WHERE '+WHERE+' AND attempt=:attempt',{'project':project_id,'id':invocation_id,'attempt':attempt})
        if not rows:raise KeyError('Attempt has no accepted result')
        return self._receipt(rows[0])

    def events(self,project_id,invocation_id,*,limit=50,after=0):
        self.invocations.get(project_id,invocation_id);_number(after,'after',0)
        if type(limit) is not int or not 1<=limit<=100:raise ValueError('limit must be 1 to 100')
        rows=self.repository.rows('SELECT * FROM invocation_events WHERE '+WHERE+' AND sequence>:after ORDER BY sequence LIMIT :limit',
            {'project':project_id,'id':invocation_id,'after':after,'limit':limit+1})
        items=[{'sequence':row['sequence'],'attempt':row['attempt'],'kind':row['kind'],
                'actor':{'kind':row['actor_kind'],'principal_id':row['actor_principal_id']},'recorded_at':_timestamp(row['recorded_ms'])} for row in rows[:limit]]
        return {'items':items,'next_after':items[-1]['sequence'] if len(rows)>limit else None}
