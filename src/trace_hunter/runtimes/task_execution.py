"""Explicit remote execution authority over the existing invocation lifecycle."""
from contextlib import contextmanager
import json
from sqlalchemy import text
from ..access import Principal
from ..catalog import canonical
from ..content import ContentCorruption
from ..invocations.execution import WHERE, _number
from ..resources import digest_json


class TaskExecution:
    def __init__(self, access):
        self.access = access
        self.execution = access.execution
        self.repository = access.repository

    @contextmanager
    def _locked(self, principal):
        # Find the task, acquire its lifecycle lock, then authenticate again.
        # Credential rotation/revocation uses that same lock. There is no gap
        # between authorization and a later independent publication transaction.
        with self.repository.engine.begin() as db:
            session = self.access.session(principal, connection=db, execution=True)
            invocation = session['invocation']
            self.execution._lock(db, invocation['project_id'], invocation['invocation_id'])
            session = self.access.session(principal, connection=db, execution=True)
            owner = session['attempt']['owner']
            actor = (Principal.operator() if owner['kind'] == 'operator' else
                     Principal(invocation['project_id'], owner['principal_id'], frozenset({'invocations:execute'})))
            yield db, session, actor

    def state(self, principal):
        session = self.access.session(principal, execution=True)
        attempt = session['attempt']
        current = session['latest_attempt'] == attempt['attempt']
        active = current and session['invocation']['status'] == 'running' and attempt['status'] == 'running' and attempt['lease_state'] == 'active'
        return {'project_id': session['invocation']['project_id'], 'invocation_id': session['invocation']['invocation_id'],
                'attempt': attempt, 'latest_attempt': session['latest_attempt'],
                'invocation_status': session['invocation']['status'], 'can_execute': active, 'should_stop': not active}

    def heartbeat(self, principal):
        with self._locked(principal) as (db, session, actor):
            invocation = session['invocation']; attempt = session['attempt']
            return self.execution.heartbeat(invocation['project_id'], invocation['invocation_id'],
                attempt=attempt['attempt'], lease_id=attempt['lease_id'], actor=actor, connection=db)

    @staticmethod
    def _source(db, project, invocation_id, attempt, receipt):
        row = db.execute(text('''SELECT e.*, g.project_id AS grant_project,g.invocation_id AS grant_invocation,g.attempt AS grant_attempt,
            g.runtime_id,g.runtime_revision,g.runtime_digest,g.probe_sequence
            FROM invocation_remote_results e LEFT JOIN invocation_task_grants g ON g.grant_id=e.grant_id
            WHERE e.project_id=:project AND e.invocation_id=:id AND e.attempt=:attempt'''),
                         {'project': project, 'id': invocation_id, 'attempt': attempt}).mappings().first()
        if row is None:
            return None
        try:
            value = json.loads(row['evidence'])
            expected = {'schema_version': 'trace-hunter/remote-result-source/1', 'project_id': project,
                        'invocation_id': invocation_id, 'attempt': attempt, 'grant_id': row['grant_id'],
                        'runtime': {'kind': 'runtime_binding', 'id': row['runtime_id'], 'revision': row['runtime_revision'], 'digest': row['runtime_digest']},
                        'probe_sequence': row['probe_sequence'], 'receipt_digest': receipt['digest']}
            if (value != expected or digest_json(value) != row['evidence_digest'] or
                (row['grant_project'], row['grant_invocation'], row['grant_attempt']) != (project, invocation_id, attempt)):
                raise ValueError()
        except (ValueError, TypeError, KeyError):
            raise ContentCorruption('Remote result attribution does not match its receipt') from None
        return {**value, 'digest': row['evidence_digest']}

    def _finish(self, principal, method, arguments):
        with self._locked(principal) as (db, session, actor):
            invocation = session['invocation']; attempt = session['attempt']; grant = session['grant']
            project = invocation['project_id']; ident = invocation['invocation_id']; number = attempt['attempt']
            accepted = method(project, ident, attempt=number, lease_id=attempt['lease_id'], actor=actor, connection=db, **arguments)
            if accepted['created']:
                evidence = {'schema_version': 'trace-hunter/remote-result-source/1', 'project_id': project,
                            'invocation_id': ident, 'attempt': number, 'grant_id': grant['grant_id'],
                            'runtime': grant['runtime'], 'probe_sequence': grant['probe_sequence'], 'receipt_digest': accepted['result']['digest']}
                db.execute(text('''INSERT INTO invocation_remote_results(project_id,invocation_id,attempt,grant_id,evidence_digest,evidence)
                    VALUES(:project,:id,:attempt,:grant,:digest,:evidence)'''),
                    {'project': project, 'id': ident, 'attempt': number, 'grant': grant['grant_id'],
                     'digest': digest_json(evidence), 'evidence': canonical(evidence)[0]})
            source = self._source(db, project, ident, number, accepted['result'])
            return {**accepted, 'delegation': source}

    def complete(self, principal, *, outputs):
        return self._finish(principal, self.execution.complete, {'outputs': outputs})

    def fail(self, principal, *, error):
        return self._finish(principal, self.execution.fail, {'error': error})

    def result(self, principal):
        session = self.access.session(principal, execution=True)
        invocation = session['invocation']
        return self.result_for(invocation['project_id'], invocation['invocation_id'], session['attempt']['attempt'])

    def result_for(self, project_id, invocation_id, attempt):
        self.execution.invocations.get(project_id, invocation_id)
        _number(attempt, 'attempt')
        with self.repository.engine.connect() as db:
            row = db.execute(text('SELECT * FROM invocation_results WHERE '+WHERE+' AND attempt=:attempt'),
                             {'project': project_id, 'id': invocation_id, 'attempt': attempt}).mappings().first()
            if row is None:
                return {'result': None, 'delegation': None}
            receipt = self.execution._receipt(row)
            return {'result': receipt, 'delegation': self._source(db, project_id, invocation_id, attempt, receipt)}
