"""Authenticated execution attempts; output lineage comes from the fixed request."""

from typing import Annotated, Literal
from fastapi import Header, HTTPException, Path, Query, Request, Response
from pydantic import Field, JsonValue
from starlette.concurrency import run_in_threadpool

from trace_hunter.artifacts.service import MAX_CONTENT_BYTES
from trace_hunter.traces.service import MAX_REVISION
from .access import require_project
from .artifacts import ArtifactContentInput, ArtifactReference, ArtifactToken, ResourceId, content_bytes
from .invocations import _body, _request_schema
from .traces import Digest, ProjectPath, RunPath, Timestamp, TraceDTO, TraceRouteError

AttemptNumber = Annotated[int, Field(strict=True, ge=1, le=MAX_REVISION)]


class ExecutionActor(TraceDTO):
    kind: Literal['operator', 'service']
    principal_id: str | None


class ClaimRequest(TraceDTO):
    worker_id: ResourceId
    lease_seconds: Annotated[int, Field(strict=True, ge=1, le=300)] = 60


class AttemptFence(TraceDTO):
    attempt: AttemptNumber
    lease_id: ResourceId


class ExecutionOutput(TraceDTO):
    role: ArtifactToken
    content: ArtifactContentInput
    metadata: dict[str, JsonValue]


class CompleteRequest(AttemptFence):
    outputs: list[ExecutionOutput] = Field(max_length=100)


class ExecutionError(TraceDTO):
    code: ArtifactToken
    details: dict[str, JsonValue]


class FailRequest(AttemptFence):
    error: ExecutionError


class ManageRequest(TraceDTO):
    expected_attempt: Annotated[int, Field(strict=True, ge=0, le=MAX_REVISION)]


class AttemptDescriptor(TraceDTO):
    project_id: str
    invocation_id: str
    attempt: AttemptNumber
    lease_id: str = Field(description='Public fencing identifier; authenticated attempt ownership is also required.')
    worker_id: str
    owner: ExecutionActor
    status: Literal['running', 'succeeded', 'failed', 'expired', 'cancelled']
    lease_seconds: Annotated[int, Field(ge=1, le=300)]
    lease_state: Literal['active', 'expired', 'inactive']
    started_at: Timestamp
    heartbeat_at: Timestamp
    lease_expires_at: Timestamp
    finished_at: Timestamp | None


class ClaimAccepted(TraceDTO):
    created: bool
    attempt: AttemptDescriptor


class AttemptPage(TraceDTO):
    items: list[AttemptDescriptor]
    next_before: AttemptNumber | None


class ResultOutput(TraceDTO):
    position: Annotated[int, Field(ge=0, le=99)]
    role: ArtifactToken
    artifact: ArtifactReference


class ExecutionReceipt(TraceDTO):
    schema_version: Literal['trace-hunter/invocation-result/1']
    project_id: str
    invocation_id: str
    attempt: AttemptNumber
    outcome: Literal['succeeded', 'failed']
    outputs: list[ResultOutput]
    error: ExecutionError | None
    reported_by: ExecutionActor
    accepted_at: Timestamp
    meaning: Literal['execution_report_accepted']
    digest: Digest


class ResultAccepted(TraceDTO):
    created: bool
    result: ExecutionReceipt


class ManagedInvocation(TraceDTO):
    changed: bool
    status: Literal['pending', 'cancelled']
    current_attempt: Annotated[int, Field(ge=0, le=MAX_REVISION)]


class InvocationEvent(TraceDTO):
    sequence: Annotated[int, Field(ge=1)]
    attempt: Annotated[int, Field(ge=0)]
    kind: Literal['claimed', 'succeeded', 'failed', 'cancelled', 'retry_requested', 'lease_expired']
    actor: ExecutionActor
    recorded_at: Timestamp


class InvocationEventPage(TraceDTO):
    items: list[InvocationEvent]
    next_after: Annotated[int, Field(ge=1)] | None


class RecoveryRequest(TraceDTO):
    limit: Annotated[int, Field(strict=True, ge=1, le=100)] = 100


class RecoveredAttempt(TraceDTO):
    invocation_id: str
    attempt: AttemptNumber
    status: Literal['blocked']
    reason: Literal['lease_expired']


class RecoveryResult(TraceDTO):
    items: list[RecoveredAttempt]
    examined: Annotated[int, Field(ge=0, le=100)]
    more_candidates: bool
    limit: Annotated[int, Field(ge=1, le=100)]


def decode_outputs(items):
    outputs, total = [], 0
    for item in items:
        raw = content_bytes(item.content.data)
        total += len(raw)
        if total > MAX_CONTENT_BYTES:
            raise HTTPException(413, 'Combined output content exceeds 8 MiB')
        outputs.append({'role': item.role, 'content': raw, 'media_type': item.content.media_type, 'metadata': item.metadata})
    return outputs


REQUEST_MODELS = (ClaimRequest, AttemptFence, CompleteRequest, FailRequest, ManageRequest)


def install(app, execution):
    root = '/api/v1/projects/{project_id}/invocations/{invocation_id}'
    errors = {code: {'model': TraceRouteError} for code in (401, 403, 404, 409, 413, 415, 422, 500)}
    accepted = {201: {'model': ResultAccepted}, **errors}

    @app.post('/api/v1/projects/{project_id}/invocations/recover-expired', response_model=RecoveryResult,
              operation_id='recoverExpiredInvocations', tags=['Execution'], responses=errors)
    def recover(project_id: ProjectPath, body: RecoveryRequest, request: Request):
        return execution.recover_expired(project_id, limit=body.limit, actor=require_project(request, project_id, 'invocations:write'))

    @app.post(root+'/claim', response_model=ClaimAccepted, operation_id='claimInvocation', tags=['Execution'],
              responses={201: {'model': ClaimAccepted}, **errors}, openapi_extra=_request_schema(ClaimRequest))
    async def claim(project_id: ProjectPath, invocation_id: RunPath, request: Request, response: Response,
                    idempotency_key: Annotated[str, Header(alias='Idempotency-Key', min_length=1, max_length=512)]):
        actor = require_project(request, project_id, 'invocations:execute')
        body = await _body(request, ClaimRequest)
        result = await run_in_threadpool(execution.claim, project_id, invocation_id,
            request_key=idempotency_key, actor=actor, **body.model_dump())
        response.status_code = 201 if result['created'] else 200
        return result

    @app.post(root+'/heartbeat', response_model=AttemptDescriptor, operation_id='heartbeatInvocation', tags=['Execution'],
              responses=errors, openapi_extra=_request_schema(AttemptFence))
    async def heartbeat(project_id: ProjectPath, invocation_id: RunPath, request: Request):
        actor = require_project(request, project_id, 'invocations:execute')
        body = await _body(request, AttemptFence)
        return await run_in_threadpool(execution.heartbeat, project_id, invocation_id, actor=actor, **body.model_dump())

    @app.post(root+'/complete', response_model=ResultAccepted, operation_id='completeInvocation', tags=['Execution'],
              responses=accepted, openapi_extra=_request_schema(CompleteRequest))
    async def complete(project_id: ProjectPath, invocation_id: RunPath, request: Request, response: Response):
        actor = require_project(request, project_id, 'invocations:execute')
        body = await _body(request, CompleteRequest)
        outputs = decode_outputs(body.outputs)
        result = await run_in_threadpool(execution.complete, project_id, invocation_id, actor=actor,
            attempt=body.attempt, lease_id=body.lease_id, outputs=outputs)
        response.status_code = 201 if result['created'] else 200
        return result

    @app.post(root+'/fail', response_model=ResultAccepted, operation_id='failInvocation', tags=['Execution'],
              responses=accepted, openapi_extra=_request_schema(FailRequest))
    async def fail(project_id: ProjectPath, invocation_id: RunPath, request: Request, response: Response):
        actor = require_project(request, project_id, 'invocations:execute')
        body = await _body(request, FailRequest)
        result = await run_in_threadpool(execution.fail, project_id, invocation_id, actor=actor, **body.model_dump())
        response.status_code = 201 if result['created'] else 200
        return result

    @app.post(root+'/cancel', response_model=ManagedInvocation, operation_id='cancelInvocation', tags=['Execution'],
              responses=errors, openapi_extra=_request_schema(ManageRequest))
    async def cancel(project_id: ProjectPath, invocation_id: RunPath, request: Request):
        actor = require_project(request, project_id, 'invocations:write')
        body = await _body(request, ManageRequest)
        return await run_in_threadpool(execution.cancel, project_id, invocation_id, actor=actor, **body.model_dump())

    @app.post(root+'/retry', response_model=ManagedInvocation, operation_id='retryInvocation', tags=['Execution'],
              responses=errors, openapi_extra=_request_schema(ManageRequest))
    async def retry(project_id: ProjectPath, invocation_id: RunPath, request: Request):
        actor = require_project(request, project_id, 'invocations:write')
        body = await _body(request, ManageRequest)
        return await run_in_threadpool(execution.retry, project_id, invocation_id, actor=actor, **body.model_dump())

    @app.get(root+'/attempts', response_model=AttemptPage, operation_id='listInvocationAttempts', tags=['Execution'], responses=errors)
    def attempts(project_id: ProjectPath, invocation_id: RunPath, request: Request,
                 limit: Annotated[int, Query(ge=1, le=100)] = 50,
                 before: Annotated[int, Query(ge=1, le=MAX_REVISION)] | None = None):
        require_project(request, project_id, 'invocations:read')
        return execution.attempts(project_id, invocation_id, limit=limit, before=before)

    @app.get(root+'/results/{attempt}', response_model=ExecutionReceipt, operation_id='getInvocationResult', tags=['Execution'], responses=errors)
    def result(project_id: ProjectPath, invocation_id: RunPath, attempt: Annotated[int, Path(ge=1, le=MAX_REVISION)], request: Request):
        require_project(request, project_id, 'invocations:read')
        return execution.result(project_id, invocation_id, attempt)

    @app.get(root+'/events', response_model=InvocationEventPage, operation_id='listInvocationEvents', tags=['Execution'], responses=errors)
    def events(project_id: ProjectPath, invocation_id: RunPath, request: Request,
               limit: Annotated[int, Query(ge=1, le=100)] = 50,
               after: Annotated[int, Query(ge=0, le=MAX_REVISION)] = 0):
        require_project(request, project_id, 'invocations:read')
        return execution.events(project_id, invocation_id, limit=limit, after=after)
