"""Attempt-bound input delegation, without giving a runtime project-wide access.

Credential rotation never changes the attempt's runtime. A read checks the live
lease, grant and issuing principal, then resolves only explicit immutable inputs.
It cannot claim, dispatch, analyze, publish, or acknowledge training consumption.
"""
from dataclasses import dataclass
import hashlib
import json
import re
import secrets
import uuid
from sqlalchemy import text

from ..access import AuthenticationError
from ..content import ContentCorruption
from ..database import Conflict
from ..invocations.execution import InvocationExecution, WHERE, _actor, _now_ms, _timestamp
from ..resources import checked_digest, digest_json
from ..traces.service import identifier
from .contracts import PROTOCOL, revision_number
from .service import RuntimeBindings

TOKEN = re.compile(r'th_ta_[A-Za-z0-9_-]{43}\Z', re.ASCII)
MAX_TTL_SECONDS = 86400
EXECUTION_PROTOCOL = "trace-hunter/task-execution/1"


@dataclass(frozen=True)
class TaskPrincipal:
    grant_id: str
    token_digest: str


def runtime_reference(value):
    if not isinstance(value, dict) or set(value) != {'kind', 'id', 'revision', 'digest'} or value['kind'] != 'runtime_binding':
        raise ValueError('A fixed runtime_binding reference is required')
    from ..invocations.service import _url_token
    return {'kind': 'runtime_binding', 'id': _url_token(value['id'], 'runtime_id'),
            'revision': revision_number(value['revision']), 'digest': checked_digest(value['digest'])}


def required_scopes(invocation):
    return {'invocations:execute', 'operations:read', 'runtimes:read'} | {
        'artifacts:read' if item['ref']['kind'] == 'artifact' else 'traces:read'
        for item in invocation['inputs']}


class TaskAccess:
    def __init__(self, repository, content):
        self.repository = repository
        self.execution = InvocationExecution(repository, content)
        self.invocations = self.execution.invocations

    @staticmethod
    def _descriptor(row):
        return {'grant_id': row['grant_id'], 'project_id': row['project_id'],
                'invocation_id': row['invocation_id'], 'attempt': row['attempt'],
                'runtime': {'kind': 'runtime_binding', 'id': row['runtime_id'],
                            'revision': row['runtime_revision'], 'digest': row['runtime_digest']},
                'probe_sequence': row['probe_sequence'], 'permissions': ['inputs:read'] + (['attempt:execute'] if row.get('execution_allowed', False) else []),
                'issued_at': _timestamp(row['issued_ms']), 'expires_at': _timestamp(row['expires_ms']),
                'revoked_at': _timestamp(row['revoked_ms'])}

    @staticmethod
    def _current_issuer(db, invocation, owner):
        if owner['owner_kind'] == 'operator':
            return
        row = db.execute(text('SELECT project_id,scopes,revoked_at FROM service_principals WHERE principal_id=:principal'),
                         {'principal': owner['owner_principal_id']}).mappings().first()
        if (row is None or row['revoked_at'] is not None or row['project_id'] != invocation['project_id']
                or not required_scopes(invocation).issubset(json.loads(row['scopes']))):
            raise AuthenticationError()

    @staticmethod
    def _runtime(db, project_id, ref, operation, now, *, allow_execution=False):
        params = {'project': project_id, 'runtime': ref['id'], 'revision': ref['revision']}
        binding = db.execute(text('''SELECT r.* FROM runtime_bindings r JOIN runtime_heads h
            ON h.project_id=r.project_id AND h.runtime_id=r.runtime_id AND h.current_revision=r.revision
            WHERE r.project_id=:project AND r.runtime_id=:runtime AND r.revision=:revision'''), params).mappings().first()
        if binding is None:
            raise Conflict('Runtime binding is not the current revision')
        described = RuntimeBindings._describe(binding)
        if described['ref'] != ref or not described['config']['enabled']:
            raise Conflict('Runtime binding is disabled or its digest does not match')
        probe = db.execute(text('''SELECT * FROM runtime_probes WHERE project_id=:project AND runtime_id=:runtime
            AND revision=:revision AND state<>'incomplete' ORDER BY sequence DESC LIMIT 1'''), params).mappings().first()
        if probe is None or probe['state'] != 'succeeded' or now - probe['completed_ms'] > 300000:
            raise Conflict('Runtime requires a successful capability observation within 300 seconds')
        try:
            document = json.loads(probe['document'])
            if digest_json(document) != probe['document_digest']:
                raise ContentCorruption('Runtime capability document is damaged')
            required_protocols = {PROTOCOL, EXECUTION_PROTOCOL} if allow_execution else {PROTOCOL}
            if not required_protocols.issubset(document['protocols']) or operation not in document['operations']:
                raise Conflict('Runtime does not advertise the fixed operation and protocol')
        except (KeyError, TypeError, json.JSONDecodeError):
            raise ContentCorruption('Runtime capability document is damaged') from None
        return probe['sequence']

    def grant(self, project_id, invocation_id, *, attempt, lease_id, runtime, ttl_seconds=3600, allow_execution=False, actor):
        owner = _actor(actor, project_id, 'invocations:execute')
        ref = runtime_reference(runtime)
        if type(allow_execution) is not bool:
            raise ValueError('allow_execution must be boolean')
        if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= MAX_TTL_SECONDS:
            raise ValueError('ttl_seconds must be 1 to 86400')
        with self.repository.engine.begin() as db:
            row, params = self.execution._lock(db, project_id, invocation_id)
            current = self.execution._latest(db, params)
            self.execution._owner(current, attempt, lease_id, owner)
            now = _now_ms(db)
            self.execution._active(row, current, now)
            invocation = self.invocations._describe(row)
            for scope in required_scopes(invocation):
                actor.require(project_id, scope)
            self._current_issuer(db, invocation, current)
            previous = db.execute(text('SELECT * FROM invocation_task_grants WHERE '+WHERE+' AND attempt=:attempt LIMIT 1'),
                                  {**params, 'attempt': attempt}).mappings().first()
            if previous is not None and self._descriptor(previous)['runtime'] != ref:
                raise Conflict('An attempt already delegates to another runtime; create a new attempt to change it')
            sequence = self._runtime(db, project_id, ref, invocation['operation'], now, allow_execution=allow_execution)
            token = 'th_ta_' + secrets.token_urlsafe(32)
            grant = {'grant_id': 'tg_' + uuid.uuid4().hex, 'project_id': project_id, 'invocation_id': invocation_id,
                     'attempt': attempt, 'runtime_id': ref['id'], 'runtime_revision': ref['revision'],
                     'runtime_digest': ref['digest'], 'probe_sequence': sequence,
                     'token_digest': hashlib.sha256(token.encode()).hexdigest(), 'issued_ms': now,
                     'expires_ms': now + ttl_seconds * 1000, 'revoked_ms': None}
            db.execute(text('UPDATE invocation_task_grants SET revoked_ms=COALESCE(revoked_ms,:now) WHERE '+WHERE+' AND attempt=:attempt'),
                       {**params, 'attempt': attempt, 'now': now})
            db.execute(text('INSERT INTO invocation_task_grants('+','.join(grant)+') VALUES('+','.join(':'+key for key in grant)+')'), grant)
            if allow_execution:
                db.execute(text('INSERT INTO invocation_task_execution_grants(grant_id) VALUES(:id)'), {'id': grant['grant_id']})
            return {'token': token, 'grant': self._descriptor({**grant, 'execution_allowed': allow_execution})}

    def revoke(self, project_id, invocation_id, *, grant_id, actor):
        owner = _actor(actor, project_id, 'invocations:execute')
        identifier(grant_id, 'grant_id')
        with self.repository.engine.begin() as db:
            _, params = self.execution._lock(db, project_id, invocation_id)
            row = db.execute(text('SELECT *,EXISTS(SELECT 1 FROM invocation_task_execution_grants e WHERE e.grant_id=invocation_task_grants.grant_id) AS execution_allowed FROM invocation_task_grants WHERE '+WHERE+' AND grant_id=:grant'),
                             {**params, 'grant': grant_id}).mappings().first()
            if row is None:
                raise KeyError('Task grant not found')
            attempt = db.execute(text('SELECT * FROM invocation_attempts WHERE '+WHERE+' AND attempt=:attempt'),
                                 {**params, 'attempt': row['attempt']}).mappings().one()
            self.execution._owner(attempt, row['attempt'], attempt['lease_id'], owner)
            at = row['revoked_ms'] if row['revoked_ms'] is not None else _now_ms(db)
            db.execute(text('UPDATE invocation_task_grants SET revoked_ms=:now WHERE grant_id=:grant'), {'grant': grant_id, 'now': at})
            return self._descriptor({**row, 'revoked_ms': at})

    def authenticate(self, token, *, execution=False):
        if not isinstance(token, str) or not TOKEN.fullmatch(token):
            raise AuthenticationError()
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self.repository.engine.connect() as db:
            grant = db.execute(text('SELECT grant_id FROM invocation_task_grants WHERE token_digest=:digest'), {'digest': digest}).scalar_one_or_none()
            if grant is None:
                raise AuthenticationError()
            principal = TaskPrincipal(grant, digest)
            self.session(principal, connection=db, execution=execution)
            return principal

    def session(self, principal, *, connection=None, execution=False):
        if not isinstance(principal, TaskPrincipal):
            raise AuthenticationError()
        if connection is None:
            with self.repository.engine.connect() as db:
                return self.session(principal, connection=db, execution=execution)
        db = connection
        # One SQL observation of delegation and its current attempt. Reads don't
        # take writer locks, renew leases, repair state or emit consumption events.
        row = db.execute(text('''SELECT g.*, EXISTS(SELECT 1 FROM invocation_task_execution_grants e WHERE e.grant_id=g.grant_id) AS execution_allowed, a.owner_kind,a.owner_principal_id,a.lease_id,a.lease_expires_ms,
            a.status AS attempt_status,a.started_ms,a.heartbeat_ms,a.finished_ms,a.worker_id,a.lease_seconds,i.status AS invocation_status,
            (SELECT MAX(n.attempt) FROM invocation_attempts n WHERE n.project_id=g.project_id AND n.invocation_id=g.invocation_id) AS latest_attempt
            FROM invocation_task_grants g JOIN invocation_attempts a ON a.project_id=g.project_id AND a.invocation_id=g.invocation_id AND a.attempt=g.attempt
            JOIN invocations i ON i.project_id=g.project_id AND i.invocation_id=g.invocation_id
            WHERE g.grant_id=:grant AND g.token_digest=:digest'''), {'grant': principal.grant_id, 'digest': principal.token_digest}).mappings().first()
        now = _now_ms(db)
        if row is None or row['revoked_ms'] is not None or now >= row['expires_ms']:
            raise AuthenticationError()
        if execution:
            if not row['execution_allowed']:
                raise PermissionError('Task credential does not grant execution')
        elif (now >= row['lease_expires_ms'] or row['attempt_status'] != 'running'
              or row['invocation_status'] != 'running' or row['latest_attempt'] != row['attempt']):
            raise AuthenticationError()
        invocation_row = db.execute(text('SELECT * FROM invocations WHERE '+WHERE),
                                    {'project': row['project_id'], 'id': row['invocation_id']}).mappings().one()
        invocation = self.invocations._describe(invocation_row)
        self._current_issuer(db, invocation, row)
        result = {'grant': self._descriptor(row), 'invocation': invocation,
                  'lease_expires_at': _timestamp(row['lease_expires_ms'])}
        if execution:
            result.update(attempt=self.execution._attempt({**row, 'status': row['attempt_status']}, now),
                          latest_attempt=row['latest_attempt'])
        return result

    def context(self, principal):
        session = self.session(principal)
        invocation = session['invocation']
        operation = self.invocations.get_operation(invocation['project_id'], invocation['operation']['operation_id'], invocation['operation']['version'])
        if operation['ref'] != invocation['operation']:
            raise ContentCorruption('Task operation binding changed')
        return {'schema_version': 'trace-hunter/task-context/1', 'grant': session['grant'],
                'invocation_id': invocation['invocation_id'], 'invocation_digest': invocation['spec_digest'],
                'operation': operation, 'config': invocation['config'], 'input_count': len(invocation['inputs']),
                'lease_expires_at': session['lease_expires_at']}

    def inputs(self, principal, *, limit=50, after=None):
        from .task_evidence import page_range
        start = page_range(limit, after)
        session = self.session(principal)
        bindings = session['invocation']['inputs']
        items = [{'position': position, **bindings[position]} for position in range(start, min(start+limit, len(bindings)))]
        return {'items': items, 'input_count': len(bindings),
                'next_after': items[-1]['position'] if items and start+len(items) < len(bindings) else None}
