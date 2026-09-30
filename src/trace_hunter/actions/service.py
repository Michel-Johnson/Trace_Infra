"""Stable external operation intentions and explicitly reconciled effect claims."""
import hashlib
import json
import uuid
from contextlib import contextmanager
from sqlalchemy import text
from ..artifacts import Artifacts
from ..catalog import canonical
from ..content import ContentCorruption, ContentRef
from ..database import Conflict
from ..invocations.execution import _actor, _now_ms, _number
from ..resources import digest_json, json_object, namespace_token, resource_ref
from ..runtimes.task_execution import TaskExecution
from ..traces.service import identifier
from .lifecycle import CURRENT_ACTIONS, describe, describe_current, load, record, checked_event


class ExternalActions:
    def __init__(self,access,content):
        self.access=access
        self.tasks=TaskExecution(access)
        self.repository=access.repository
        self.content=content

    @contextmanager
    def _task(self,principal,*,active=False):
        with self.tasks._locked(principal) as (db,session,actor):
            invocation=session['invocation'];attempt=session['attempt']
            if active and (session['latest_attempt']!=attempt['attempt'] or invocation['status']!='running' or attempt['lease_state']!='active'):
                raise Conflict('Task attempt is not active')
            identity={'kind':'task','grant_id':session['grant']['grant_id'],'principal_id':actor.principal_id,'attempt':attempt['attempt']}
            yield db,session,identity

    @staticmethod
    def _owned(db,session,action_id):
        identifier(action_id,'action_id')
        row=load(db,session['invocation']['project_id'],action_id)
        if row['invocation_id']!=session['invocation']['invocation_id']:raise PermissionError('Action belongs to another task')
        return row

    def prepare(self,principal,*,action_key,operation,request):
        identifier(action_key,'action_key');namespace_token(operation,'operation');request=json_object(request,'action request')
        with self._task(principal,active=True) as (db,session,actor):
            invocation=session['invocation'];project=invocation['project_id'];ident=invocation['invocation_id']
            spec={'schema_version':'trace-hunter/external-action/1','project_id':project,'invocation_id':ident,
                  'action_key':action_key,'operation':operation,'request':request}
            fingerprint=digest_json(spec);key=hashlib.sha256(action_key.encode()).hexdigest()
            previous=db.execute(text('SELECT action_id FROM external_actions WHERE project_id=:project AND invocation_id=:id AND action_key_hash=:key'),
                                {'project':project,'id':ident,'key':key}).scalar_one_or_none()
            if previous is not None:
                row=load(db,project,previous)
                if row['spec_digest']!=fingerprint or row['action_key']!=action_key:raise Conflict('Action key already identifies a different intention')
                return {'created':False,'action':describe(row)}
            now=_now_ms(db)
            row={'project_id':project,'action_id':'act_'+uuid.uuid4().hex,'invocation_id':ident,'action_key_hash':key,'action_key':action_key,
                 'spec_digest':fingerprint,'spec':canonical(spec)[0],'created_attempt':session['grant']['attempt'],
                 'created_grant':session['grant']['grant_id'],'created_ms':now,'state':'prepared','version':1,'current_execution':0}
            db.execute(text('INSERT INTO external_actions('+','.join(row)+') VALUES('+','.join(':'+key for key in row)+')'),row)
            record(db,row,'prepared',state='prepared',execution=0,actor=actor,details={},now=now,initial=True)
            return {'created':True,'action':describe(row)}

    @staticmethod
    def _execution(row):
        from ..invocations.execution import _timestamp
        return {key:row[key] for key in ('execution','invocation_attempt','grant_id','fence_id','start_version','state')}|{
            'started_at':_timestamp(row['started_ms']),
            'provider_key':hashlib.sha256((row['project_id']+'\0'+row['action_id']+'\0'+str(row['execution'])).encode()).hexdigest()}

    def begin(self,principal,action_id,*,expected_version):
        _number(expected_version,'expected_version')
        with self._task(principal,active=True) as (db,session,actor):
            action=self._owned(db,session,action_id)
            params={'project':action['project_id'],'id':action_id,'execution':action['current_execution']}
            current=db.execute(text('SELECT * FROM external_action_executions WHERE project_id=:project AND action_id=:id AND execution=:execution'),params).mappings().first()
            if current is not None and current['start_version']==expected_version and current['invocation_attempt']==actor['attempt']:
                return {'created':False,'can_send':False,'execution':self._execution(current),'action':describe(action)}
            if action['version']!=expected_version:raise Conflict('Action version changed')
            if action['state'] not in ('prepared','not_applied'):raise Conflict('Action is unresolved or already applied; do not send it again')
            number=_number(action['current_execution']+1,'execution');now=_now_ms(db)
            current={'project_id':action['project_id'],'action_id':action_id,'execution':number,'invocation_attempt':actor['attempt'],
                     'grant_id':session['grant']['grant_id'],'fence_id':'effect_'+uuid.uuid4().hex,'start_version':expected_version,'started_ms':now,'state':'running'}
            db.execute(text('INSERT INTO external_action_executions('+','.join(current)+') VALUES('+','.join(':'+key for key in current)+')'),current)
            record(db,action,'started',state='running',execution=number,actor=actor,details={},now=now)
            return {'created':True,'can_send':True,'execution':self._execution(current),'action':describe(load(db,action['project_id'],action_id))}

    def report(self,principal,action_id,*,execution,fence_id,outcome,details,request_key):
        _number(execution,'execution');identifier(fence_id,'fence_id');identifier(request_key,'request_key')
        if outcome not in ('applied','not_applied','outcome_unknown'):raise ValueError('Unknown external effect outcome')
        details=json_object(details,'effect details')
        submission=digest_json({'execution':execution,'fence_id':fence_id,'outcome':outcome,'details':details})
        with self._task(principal) as (db,session,actor):
            action=self._owned(db,session,action_id);params={'project':action['project_id'],'id':action_id,'execution':execution}
            current=db.execute(text('SELECT * FROM external_action_executions WHERE project_id=:project AND action_id=:id AND execution=:execution'),params).mappings().first()
            if current is None:raise KeyError('External execution not found')
            if current['invocation_attempt']!=actor['attempt'] or current['fence_id']!=fence_id:raise PermissionError('External execution belongs to another attempt or fence')
            key={**params,'key':hashlib.sha256(request_key.encode()).hexdigest()}
            previous=db.execute(text('SELECT * FROM external_action_receipts WHERE project_id=:project AND action_id=:id AND report_key_hash=:key'),key).mappings().first()
            if previous is not None:
                if previous['report_key']!=request_key or previous['submission_digest']!=submission:raise Conflict('Report key already binds another effect observation')
                try:
                    receipt=json.loads(previous['receipt'])
                    if digest_json(receipt)!=previous['receipt_digest']:raise ValueError()
                except (ValueError,TypeError):raise ContentCorruption('External effect receipt is damaged') from None
                return {'created':False,'receipt':{**receipt,'digest':previous['receipt_digest']}}
            if action['current_execution']!=execution:raise Conflict('External execution is no longer current; preserve new evidence for reconciliation')
            if action['state'] not in ('running','outcome_unknown'):raise Conflict('The effect is already resolved; contradictory observations require review')
            event=record(db,action,'reported',state=outcome,execution=execution,actor=actor,
                         details={'outcome':outcome,'evidence':details,'provenance':'executor_claim'},now=_now_ms(db))
            db.execute(text('UPDATE external_action_executions SET state=:state WHERE project_id=:project AND action_id=:id AND execution=:execution'),{**params,'state':outcome})
            value={k:v for k,v in event.items() if k!='digest'}
            db.execute(text('''INSERT INTO external_action_receipts(project_id,action_id,execution,report_key_hash,report_key,submission_digest,receipt_digest,receipt)
                VALUES(:project,:id,:execution,:key,:raw_key,:submission,:digest,:receipt)'''),
                {**key,'raw_key':request_key,'submission':submission,'digest':event['digest'],'receipt':canonical(value)[0]})
            return {'created':True,'receipt':event}

    def abandon(self,principal,action_id,*,expected_version):
        _number(expected_version,'expected_version')
        with self._task(principal,active=True) as (db,session,actor):
            action=self._owned(db,session,action_id)
            if action['state']=='abandoned':return describe(action)
            if action['version']!=expected_version:raise Conflict('Action version changed')
            if action['state']!='prepared':raise Conflict('A started effect cannot be abandoned as if it never happened')
            record(db,action,'abandoned',state='abandoned',execution=0,actor=actor,details={},now=_now_ms(db))
            return describe(load(db,action['project_id'],action_id))

    def reconcile(self,project_id,action_id,*,expected_version,outcome,evidence,reason,actor):
        _actor(actor,project_id,'invocations:write');actor.require(project_id,'artifacts:read');_number(expected_version,'expected_version')
        evidence=resource_ref(evidence)
        if evidence['kind']!='artifact' or outcome not in ('applied','not_applied'):raise ValueError('Reconciliation requires an Artifact and a known effect outcome')
        if not isinstance(reason,str) or not reason.strip() or len(reason)>1024 or '\x00' in reason:raise ValueError('A reconciliation reason of at most 1024 characters is required')
        identifier(action_id,'action_id')
        with self.repository.engine.begin() as db:
            row=load(db,project_id,action_id)
            self.access.execution._lock(db,project_id,row['invocation_id']);row=load(db,project_id,action_id)
            if row['version']!=expected_version or row['state']!='outcome_unknown':raise Conflict('Reconciliation requires the current unknown effect version')
            artifact=db.execute(text('SELECT * FROM artifacts WHERE project_id=:project AND artifact_id=:id'),{'project':project_id,'id':evidence['id']}).mappings().first()
            if artifact is None:raise KeyError('Reconciliation evidence not found')
            artifact=Artifacts._describe(artifact)
            if artifact['ref']!=evidence:raise Conflict('Evidence digest does not match')
            with self.content.open_verified(ContentRef(**artifact['content'])):pass
            event=record(db,row,'reconciled',state=outcome,execution=row['current_execution'],
                actor={'kind':'operator' if actor.is_operator else 'service','principal_id':actor.principal_id},
                details={'outcome':outcome,'evidence_ref':evidence,'reason':reason,'provenance':'authorized_reconciliation'},now=_now_ms(db))
            db.execute(text('UPDATE external_action_executions SET state=:state WHERE project_id=:project AND action_id=:id AND execution=:execution'),
                       {'project':project_id,'id':action_id,'execution':row['current_execution'],'state':outcome})
            return {'action':describe(load(db,project_id,action_id)),'receipt':event}

    def get(self,project_id,action_id):
        identifier(project_id,'project_id');identifier(action_id,'action_id')
        with self.repository.engine.connect() as db:return describe(load(db,project_id,action_id))

    def task_get(self,principal,action_id):
        session=self.access.session(principal,execution=True)
        with self.repository.engine.connect() as db:return describe(self._owned(db,session,action_id))

    def task_list(self, principal, **options):
        session = self.access.session(principal, execution=True)
        task = session['invocation']
        return self.list_for(task['project_id'], task['invocation_id'], **options)

    def task_events(self, principal, action_id, **options):
        action = self.task_get(principal, action_id)
        return self.events(action['project_id'], action_id, **options)

    @staticmethod
    def _page(rows, limit, describe_item, cursor_key):
        items, size = [], 0
        for row in rows[:limit]:
            item = describe_item(row)
            length = len(canonical(item)[0].encode())
            if items and size + length > 262144: break
            items.append(item); size += length
        return {'items':items,'next_after':items[-1][cursor_key] if len(rows)>len(items) else None}

    @staticmethod
    def _limit(limit):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('limit must be 1 to 100')

    @staticmethod
    def _summary(row):
        item = describe_current(row)
        del item['request']
        return item

    def list_for(self, project_id, invocation_id, *, after=None, limit=50):
        self.access.invocations.get(project_id, invocation_id)
        self._limit(limit)
        if after is not None: identifier(after, 'after')
        order = 'a.action_id COLLATE "C"' if self.repository.postgres else 'a.action_id COLLATE BINARY'
        rows = self.repository.rows(CURRENT_ACTIONS+' WHERE a.project_id=:project AND a.invocation_id=:id'+
            (' AND '+order+'>:after' if after is not None else '')+' ORDER BY '+order+' LIMIT :limit',
            {'project':project_id,'id':invocation_id,'after':after,'limit':limit+1})
        return self._page(rows, limit, self._summary, 'action_id')

    def events(self, project_id, action_id, *, after=0, limit=50):
        self.get(project_id, action_id); _number(after, 'after', 0); self._limit(limit)
        rows = self.repository.rows('SELECT * FROM external_action_events WHERE project_id=:project AND action_id=:id AND version>:after ORDER BY version LIMIT :limit',
                                   {'project':project_id,'id':action_id,'after':after,'limit':limit+1})
        return self._page(rows, limit, checked_event, 'version')
