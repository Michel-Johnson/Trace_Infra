"""External effect state participates in native invocation transactions."""
import json
from sqlalchemy import text
from ..catalog import canonical
from ..content import ContentCorruption
from ..database import Conflict
from ..notifications import publish
from ..resources import digest_json

UNRESOLVED = ('prepared','running','outcome_unknown')


def describe(row):
    try:
        spec=json.loads(row['spec'])
        if digest_json(spec)!=row['spec_digest'] or spec['project_id']!=row['project_id'] or spec['invocation_id']!=row['invocation_id'] or spec['action_key']!=row['action_key']:
            raise ValueError()
    except (ValueError,TypeError,KeyError):raise ContentCorruption('External action specification is damaged') from None
    return {'project_id':row['project_id'],'action_id':row['action_id'],'invocation_id':row['invocation_id'],
            'ref':{'kind':'external_action','id':row['action_id'],'digest':row['spec_digest']},
            'action_key':row['action_key'],'operation':spec['operation'],'request':spec['request'],
            'created_attempt':row['created_attempt'],'created_grant':row['created_grant'],
            'state':row['state'],'version':row['version'],'current_execution':row['current_execution']}


def checked_event(row):
    try:
        event=json.loads(row['event'])
        if digest_json(event)!=row['event_digest'] or any(event[k]!=row[k] for k in ('project_id','action_id','version','event_kind')):raise ValueError()
    except (ValueError,TypeError,KeyError):raise ContentCorruption('External action event is damaged') from None
    return {**event,'digest':row['event_digest']}


# One joined read returns a head and its immutable event at the same snapshot.
# LEFT JOIN is deliberate: missing evidence must be reported, not hidden.
CURRENT_ACTIONS = """SELECT a.*,e.event AS head_event,e.event_digest AS head_digest,
    e.event_kind AS head_kind FROM external_actions a LEFT JOIN external_action_events e
    ON e.project_id=a.project_id AND e.action_id=a.action_id AND e.version=a.version"""


def describe_current(row):
    value = describe(row)
    if row['head_event'] is None:
        raise ContentCorruption('External action is missing its current event')
    event = checked_event({'project_id':row['project_id'],'action_id':row['action_id'],'version':row['version'],
                           'event_kind':row['head_kind'],'event':row['head_event'],'event_digest':row['head_digest']})
    if event['state'] != row['state'] or event['execution'] != row['current_execution']:
        raise ContentCorruption('External action state differs from its event')
    return value


def load(db, project_id, action_id):
    row = db.execute(text(CURRENT_ACTIONS+' WHERE a.project_id=:project AND a.action_id=:id'),
                     {'project':project_id,'id':action_id}).mappings().first()
    if row is None: raise KeyError('External action not found')
    describe_current(row)
    return row


def record(db,row,kind,*,state,execution,actor,details,now,initial=False):
    from ..invocations.execution import _timestamp
    version=row['version'] if initial else row['version']+1
    if version>2147483647:raise Conflict('External action version is exhausted')
    value={'schema_version':'trace-hunter/external-action-event/1','project_id':row['project_id'],'action_id':row['action_id'],
           'version':version,'event_kind':kind,'state':state,'execution':execution,'actor':actor,'details':details,'recorded_at':_timestamp(now)}
    digest=digest_json(value)
    db.execute(text('''INSERT INTO external_action_events(project_id,action_id,version,event_kind,event_digest,event)
        VALUES(:project,:id,:version,:kind,:digest,:event)'''),
        {'project':row['project_id'],'id':row['action_id'],'version':version,'kind':kind,'digest':digest,'event':canonical(value)[0]})
    db.execute(text('UPDATE external_actions SET state=:state,version=:version,current_execution=:execution WHERE project_id=:project AND action_id=:id'),
               {'project':row['project_id'],'id':row['action_id'],'version':version,'state':state,'execution':execution})
    publish(db,row['project_id'],'external_action.'+kind,row['invocation_id'],payload={'action_id':row['action_id'],'version':version,'state':state})
    return {**value,'digest':digest}


def ensure_resolved(db,project_id,invocation_id,*,retry=False):
    states=('running','outcome_unknown') if retry else UNRESOLVED
    clauses=','.join(':state'+str(i) for i in range(len(states)))
    params={'project':project_id,'id':invocation_id,**{'state'+str(i):s for i,s in enumerate(states)}}
    if db.execute(text('SELECT action_id FROM external_actions WHERE project_id=:project AND invocation_id=:id AND state IN ('+clauses+') LIMIT 1'),params).first():
        raise Conflict('External actions require resolution before retry' if retry else 'External actions must be resolved or abandoned before success')


def mark_unknown(db,project_id,invocation_id,attempt,*,reason,actor,now):
    rows=db.execute(text(CURRENT_ACTIONS+" WHERE a.project_id=:project AND a.invocation_id=:id AND a.state='running' ORDER BY a.action_id"),
                    {'project':project_id,'id':invocation_id}).mappings().all()
    for row in rows:
        current=db.execute(text('SELECT * FROM external_action_executions WHERE project_id=:project AND action_id=:id AND execution=:execution'),
                           {'project':project_id,'id':row['action_id'],'execution':row['current_execution']}).mappings().one()
        if current['invocation_attempt']!=attempt:raise ContentCorruption('Running external action belongs to a different attempt')
        describe_current(row)
        db.execute(text("UPDATE external_action_executions SET state='outcome_unknown' WHERE project_id=:project AND action_id=:id AND execution=:execution"),
                   {'project':project_id,'id':row['action_id'],'execution':row['current_execution']})
        record(db,row,'outcome_unknown',state='outcome_unknown',execution=row['current_execution'],actor=actor,
               details={'reason':reason,'meaning':'lease_or_executor_stopped_not_proof_of_no_effect'},now=now)
