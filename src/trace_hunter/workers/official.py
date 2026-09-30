"""Execute trusted installed packages in a separate process, with attempt fencing."""

import base64
import binascii
import json
import threading
import uuid

from ..access import Principal
from ..artifacts import Artifacts
from ..catalog import canonical
from ..content import ContentCorruption
from ..database import Conflict
from ..processes import ProcessFailure, run_python
from ..invocations.execution import InvocationExecution
from ..selections import SelectionSnapshots
from ..traces.service import identifier
from .registry import WorkerRegistry

MAX_INPUT_BYTES = 16 * 1024 * 1024
MAX_CONTEXT_BYTES = 24 * 1024 * 1024
MAX_OUTPUT_BYTES = 16 * 1024 * 1024


class WorkerFailure(ValueError):
    """A stable failure code; do not record source text or subprocess diagnostics."""


def output_values(raw):
    try:
        value = json.loads(raw)
        if (not isinstance(value, dict) or set(value) != {'schema_version','outputs'} or
            value['schema_version'] != 'trace-hunter/worker-output/1' or
            not isinstance(value['outputs'], list) or len(value['outputs'])>100):
            raise ValueError()
        outputs, total = [], 0
        for item in value['outputs']:
            if not isinstance(item, dict) or set(item) != {'role','content','metadata'}:
                raise ValueError()
            content = item['content']
            if not isinstance(content, dict) or set(content) != {'media_type','encoding','data'} or content['encoding']!='base64':
                raise ValueError()
            decoded = base64.b64decode(content['data'], validate=True)
            total += len(decoded)
            if total > 8*1024*1024 or base64.b64encode(decoded).decode() != content['data']:
                raise ValueError()
            outputs.append({'role':item['role'], 'content':decoded, 'media_type':content['media_type'], 'metadata':item['metadata']})
        return outputs
    except (ValueError, TypeError, KeyError, UnicodeError, binascii.Error, RecursionError):
        raise WorkerFailure('worker.output_protocol') from None


def run_program(code, context, heartbeat, stop, *, timeout_seconds=60, heartbeat_seconds=10):
    """Interpret this worker protocol on the shared bounded process host."""
    try:
        raw = run_python(code, context, max_output_bytes=MAX_OUTPUT_BYTES,
            timeout_seconds=timeout_seconds, heartbeat=heartbeat,
            heartbeat_seconds=heartbeat_seconds, stop=stop)
    except ProcessFailure as error:
        raise WorkerFailure('worker.'+error.code) from None
    return output_values(raw)


class OfficialWorker:
    def __init__(self, store, registry=None, *, worker_id='official-worker', lease_seconds=60,
                 timeout_seconds=60, stop=None):
        self.store = store
        self.registry = registry if registry is not None else WorkerRegistry()
        self.execution = InvocationExecution(store.repository, store.content)
        self.artifacts = Artifacts(store.repository, store.content)
        self.selections = SelectionSnapshots(store.repository, store.content)
        self.actor = Principal.operator()
        identifier(worker_id, 'worker_id')
        if type(lease_seconds) is not int or not 1<=lease_seconds<=300:
            raise ValueError('lease_seconds must be 1 to 300')
        if type(timeout_seconds) is not int or not 1<=timeout_seconds<=300:
            raise ValueError('timeout_seconds must be 1 to 300')
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds
        self.timeout_seconds = timeout_seconds
        self.stop = stop if stop is not None else threading.Event()

    def _pending(self, project_id):
        identifier(project_id, 'project_id')
        clauses, params = [], {'project':project_id}
        for index, installed in enumerate(self.registry.installed):
            ref = installed.ref
            clauses.append(f'(operation_id=:op{index} AND operation_version=:version{index} AND operation_digest=:digest{index})')
            params.update({f'op{index}':ref['operation_id'], f'version{index}':ref['version'], f'digest{index}':ref['digest']})
        if not clauses:return None
        rows = self.store.repository.rows("SELECT invocation_id FROM invocations WHERE project_id=:project AND status='pending' AND ("+
            ' OR '.join(clauses)+') ORDER BY created_at,invocation_id LIMIT 1', params)
        return rows[0]['invocation_id'] if rows else None

    def _context(self, invocation):
        project = invocation['project_id']
        inputs, total = [], 0
        for item in invocation['inputs']:
            ref = item['ref']
            if ref['kind']=='trace_revision':
                descriptor = self.store.revisions.get(project, ref['id'], ref['revision'])
                content_ref = descriptor['content']
                actual_digest = content_ref['digest']
                reader = lambda:self.store.revisions.read(project, ref['id'], ref['revision'])
            elif ref['kind']=='artifact':
                descriptor = self.artifacts.get(project, ref['id'])
                content_ref = descriptor['content']
                actual_digest = descriptor['descriptor_digest']
                reader = lambda:self.artifacts.read_content(project, ref['id'])
            else:
                descriptor = self.selections.get(project, ref['id'])
                content_ref = descriptor['manifest']
                actual_digest = content_ref['digest']
                reader = lambda:self.selections.read_manifest(project, ref['id'])
            if actual_digest != ref['digest']:raise ContentCorruption('Fixed input binding no longer matches')
            total += content_ref['size_bytes']
            if total>MAX_INPUT_BYTES:raise WorkerFailure('worker.input_limit')
            raw = reader()
            inputs.append({**item, 'descriptor':descriptor, 'content':{
                'media_type':content_ref['media_type'], 'encoding':'base64', 'data':base64.b64encode(raw).decode()}})
        context = {'schema_version':'trace-hunter/worker-input/1', 'invocation':{
            key:invocation[key] for key in ('project_id','invocation_id','operation','config')}, 'inputs':inputs}
        raw = canonical(context)[0].encode()
        if len(raw)>MAX_CONTEXT_BYTES:raise WorkerFailure('worker.input_limit')
        return raw

    def run_once(self, project_id, *, invocation_id=None):
        if self.stop.is_set():return None
        target = invocation_id if invocation_id is not None else self._pending(project_id)
        if target is None:return None
        invocation = self.execution.invocations.get(project_id, target)
        installed = self.registry.resolve(invocation['operation'])
        try:
            claimed = self.execution.claim(project_id, target, request_key=uuid.uuid4().hex,
                worker_id=self.worker_id, lease_seconds=self.lease_seconds, actor=self.actor)
        except Conflict:
            return {'invocation_id':target, 'state':'not_claimed'}
        fence = {key:claimed['attempt'][key] for key in ('attempt','lease_id')}
        heartbeat = lambda:self.execution.heartbeat(project_id, target, **fence, actor=self.actor)
        try:
            code = installed.verified_code()
            context = self._context(invocation)
            heartbeat()
            outputs = run_program(code, context, heartbeat, self.stop,
                timeout_seconds=self.timeout_seconds, heartbeat_seconds=self.lease_seconds/3)
            result = self.execution.complete(project_id, target, **fence, outputs=outputs, actor=self.actor)
            return {'invocation_id':target, 'state':'succeeded', 'result':result['result']}
        except (Conflict, PermissionError):
            return {'invocation_id':target, 'state':'lease_lost'}
        except (WorkerFailure, ContentCorruption, KeyError, OSError, ValueError) as error:
            # No raw exception text, captured source or stderr becomes an error receipt.
            code = str(error) if isinstance(error, WorkerFailure) else 'worker.content_unavailable' if isinstance(error, (ContentCorruption, KeyError, OSError)) else 'worker.output_invalid'
            try:
                result = self.execution.fail(project_id, target, **fence,
                    error={'code':code, 'details':{}}, actor=self.actor)
            except (Conflict, PermissionError):
                return {'invocation_id':target, 'state':'lease_lost'}
            return {'invocation_id':target, 'state':'failed', 'result':result['result']}
