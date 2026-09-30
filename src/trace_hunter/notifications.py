"""Transactional event feed and consumer progress, separate from training receipts."""
import json
import re
from sqlalchemy import text
from .access import Principal
from .catalog import canonical
from .content import ContentCorruption
from .database import Conflict
from .resources import digest_json
from .traces.service import identifier

MAX_SEQUENCE = 9007199254740991  # Exact JSON integer in Python, Rust and JavaScript.
EVENT_TYPES = frozenset(('invocation.requested','invocation.claimed','invocation.result.accepted','invocation.cancelled',
    'invocation.retry_requested','invocation.lease_expired','external_action.prepared','external_action.started',
    'external_action.reported','external_action.reconciled','external_action.abandoned','external_action.outcome_unknown'))


def _clock(db):
    from .invocations.execution import _now_ms
    return _now_ms(db)


def sequence(value,field):
    if type(value) is not int or not 0 <= value <= MAX_SEQUENCE:
        raise ValueError(field+' must be a non-negative notification sequence')
    return value


def consumer_identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}', value):
        raise ValueError('consumer_id must be a slug of 1 to 128 characters')
    return value


def publish(db, project_id, event_type, invocation_id, *, attempt=None, payload=None):
    """The caller owns the transaction. Per-project locking prevents late commits
    behind an already acknowledged cursor, not just duplicate numeric IDs."""
    if event_type not in EVENT_TYPES:
        raise ValueError('Unknown notification type')
    payload = {} if payload is None else payload
    raw = canonical(payload)[0]
    if not isinstance(payload,dict) or len(raw.encode()) > 8192:
        raise ValueError('Notification metadata exceeds 8 KiB')
    db.execute(text('INSERT INTO notification_heads(project_id,sequence) VALUES(:project,0) ON CONFLICT(project_id) DO NOTHING'),{'project':project_id})
    # UPDATE acquires the same row lock until commit on both supported databases.
    value = db.execute(text('UPDATE notification_heads SET sequence=sequence+1 WHERE project_id=:project AND sequence<:maximum RETURNING sequence'),
                       {'project':project_id,'maximum':MAX_SEQUENCE}).scalar_one_or_none()
    if value is None:
        raise Conflict('Notification sequence is exhausted')
    db.execute(text('''INSERT INTO notifications(project_id,sequence,event_type,invocation_id,attempt,payload,payload_digest,recorded_ms)
        VALUES(:project,:sequence,:event,:invocation,:attempt,:payload,:digest,:now)'''),
        {'project':project_id,'sequence':value,'event':event_type,'invocation':invocation_id,'attempt':attempt,
         'payload':raw,'digest':digest_json(payload),'now':_clock(db)})
    return value


class Notifications:
    def __init__(self,repository):
        self.repository=repository

    @staticmethod
    def _project(db,project_id):
        identifier(project_id,'project_id')
        if db.execute(text('SELECT project_id FROM projects WHERE project_id=:project'),{'project':project_id}).first() is None:
            raise KeyError('Project not found')

    @staticmethod
    def _item(row):
        from .invocations.execution import _timestamp
        try:
            payload=json.loads(row['payload'])
            if digest_json(payload)!=row['payload_digest']:raise ValueError()
        except (ValueError,TypeError):raise ContentCorruption('Notification metadata is damaged') from None
        return {'project_id':row['project_id'],'sequence':row['sequence'],'event_type':row['event_type'],
                'invocation_id':row['invocation_id'],'attempt':row['attempt'],'payload':payload,
                'payload_digest':row['payload_digest'],'recorded_at':_timestamp(row['recorded_ms'])}

    def read(self,project_id,*,after=0,limit=100):
        sequence(after,'after')
        if type(limit) is not int or not 1<=limit<=100:raise ValueError('limit must be 1 to 100')
        with self.repository.engine.connect() as db:
            self._project(db,project_id)
            # Reading head before rows bounds this response to a committed prefix.
            # Later committed rows can be fetched on the next read.
            head=db.execute(text('SELECT sequence FROM notification_heads WHERE project_id=:project'),{'project':project_id}).scalar_one_or_none() or 0
            if after>head:raise ValueError('after exceeds the committed notification head')
            rows=db.execute(text('''SELECT * FROM notifications WHERE project_id=:project AND sequence>:after AND sequence<=:head ORDER BY sequence LIMIT :limit'''),
                            {'project':project_id,'after':after,'head':head,'limit':limit}).mappings().all()
        expected=min(limit,head-after)
        if len(rows)!=expected or [r['sequence'] for r in rows]!=list(range(after+1,after+expected+1)):
            raise ContentCorruption('Notification prefix contains a gap')
        return {'items':[self._item(row) for row in rows],'next_after':rows[-1]['sequence'] if rows else after,
                'has_more':after+len(rows)<head,'head':head,'history_scope':'recorded_events'}

    @staticmethod
    def _owner(actor,project_id):
        if not isinstance(actor,Principal):raise ValueError('An authenticated principal is required')
        actor.require(project_id,'notifications:ack')
        return {'kind':'operator' if actor.is_operator else 'service','principal_id':actor.principal_id}

    @staticmethod
    def _consumer(row):
        from .invocations.execution import _timestamp
        return {'project_id':row['project_id'],'consumer_id':row['consumer_id'],
                'owner':{'kind':row['owner_kind'],'principal_id':row['owner_principal_id']},
                'acknowledged_sequence':row['acknowledged_sequence'],'created_at':_timestamp(row['created_ms']),
                'updated_at':_timestamp(row['updated_ms']),'meaning':'notification_progress_reported'}

    def create_consumer(self,project_id,consumer_id,*,actor):
        owner=self._owner(actor,project_id);consumer_identifier(consumer_id)
        with self.repository.engine.begin() as db:
            self._project(db,project_id);now=_clock(db)
            db.execute(text('''INSERT INTO notification_consumers(project_id,consumer_id,owner_kind,owner_principal_id,acknowledged_sequence,created_ms,updated_ms)
                VALUES(:project,:id,:kind,:principal,0,:now,:now) ON CONFLICT(project_id,consumer_id) DO NOTHING'''),
                {'project':project_id,'id':consumer_id,'kind':owner['kind'],'principal':owner['principal_id'],'now':now})
            row=db.execute(text('SELECT * FROM notification_consumers WHERE project_id=:project AND consumer_id=:id'),{'project':project_id,'id':consumer_id}).mappings().one()
            if self._consumer(row)['owner']!=owner:raise PermissionError('Consumer belongs to another principal')
            return self._consumer(row)

    def get_consumer(self,project_id,consumer_id,*,actor):
        owner=self._owner(actor,project_id);consumer_identifier(consumer_id)
        rows=self.repository.rows('SELECT * FROM notification_consumers WHERE project_id=:project AND consumer_id=:id',{'project':project_id,'id':consumer_id})
        if not rows:raise KeyError('Consumer not found')
        result=self._consumer(rows[0])
        if result['owner']!=owner:raise PermissionError('Consumer belongs to another principal')
        return result

    def acknowledge(self,project_id,consumer_id,*,expected_after,through,actor):
        owner=self._owner(actor,project_id);consumer_identifier(consumer_id)
        sequence(expected_after,'expected_after');sequence(through,'through')
        if through<expected_after:raise ValueError('Consumer progress cannot move backwards')
        params={'project':project_id,'id':consumer_id}
        with self.repository.engine.begin() as db:
            if not self.repository.postgres:
                db.execute(text('UPDATE notification_consumers SET updated_ms=updated_ms WHERE project_id=:project AND consumer_id=:id'),params)
            suffix=' FOR UPDATE' if self.repository.postgres else ''
            row=db.execute(text('SELECT * FROM notification_consumers WHERE project_id=:project AND consumer_id=:id'+suffix),params).mappings().first()
            if row is None:raise KeyError('Consumer not found')
            current=self._consumer(row)
            if current['owner']!=owner:raise PermissionError('Consumer belongs to another principal')
            if row['acknowledged_sequence']==through:return {'changed':False,'consumer':current}
            if row['acknowledged_sequence']!=expected_after:raise Conflict('Consumer checkpoint advanced')
            head=db.execute(text('SELECT sequence FROM notification_heads WHERE project_id=:project'),params).scalar_one_or_none() or 0
            if through>head:raise ValueError('through exceeds the committed notification head')
            now=_clock(db)
            db.execute(text('UPDATE notification_consumers SET acknowledged_sequence=:through,updated_ms=:now WHERE project_id=:project AND consumer_id=:id'),
                       {**params,'through':through,'now':now})
            return {'changed':True,'consumer':self._consumer({**row,'acknowledged_sequence':through,'updated_ms':now})}
