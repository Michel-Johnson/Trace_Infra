"""Remote task credentials have their own routes and never become project identities."""
from typing import Annotated, Literal
from fastapi import Path, Query, Request
from pydantic import Field, JsonValue
from starlette.concurrency import run_in_threadpool
from trace_hunter.access import AuthenticationError
from trace_hunter.runtimes.task_access import TaskPrincipal
from .access import require_project
from .artifacts import ArtifactInput, ResourceReference, ArtifactDescriptor, ArtifactContentReference
from .execution import AttemptFence, AttemptNumber
from .invocations import OperationDescriptor, _body, _request_schema
from .runtimes import RuntimeReference
from .selections import SelectionDescriptor
from .traces import TraceDTO, TraceRouteError, ProjectPath, RunPath, Timestamp, Count, Digest, TraceRevisionDescriptor


class TaskGrantRequest(AttemptFence):
    runtime: RuntimeReference
    allow_execution: Annotated[bool, Field(strict=True)] = False
    ttl_seconds: Annotated[int, Field(strict=True, ge=1, le=86400)] = 3600


class TaskGrantDescriptor(TraceDTO):
    grant_id: str
    project_id: str
    invocation_id: str
    attempt: AttemptNumber
    runtime: RuntimeReference
    probe_sequence: Annotated[int, Field(ge=1)]
    permissions: list[Literal['inputs:read', 'attempt:execute']]
    issued_at: Timestamp
    expires_at: Timestamp
    revoked_at: Timestamp | None


class TaskGrantAccepted(TraceDTO):
    token: Annotated[str, Field(pattern=r'^th_ta_[A-Za-z0-9_-]{43}$')]
    grant: TaskGrantDescriptor


class TaskContext(TraceDTO):
    schema_version: Literal['trace-hunter/task-context/1']
    grant: TaskGrantDescriptor
    invocation_id: str
    invocation_digest: Digest
    operation: OperationDescriptor
    config: dict[str, JsonValue]
    input_count: Count
    lease_expires_at: Timestamp


class TaskInput(ArtifactInput):
    position: Count


class TaskInputPage(TraceDTO):
    items: list[TaskInput]
    input_count: Count
    next_after: Count | None


class TaskResource(TraceDTO):
    ref: ResourceReference
    descriptor: TraceRevisionDescriptor | ArtifactDescriptor | SelectionDescriptor


class TaskMember(TraceDTO):
    position: Count
    ref: ResourceReference


class TaskMemberPage(TraceDTO):
    ref: ResourceReference
    member_count: Count
    items: list[TaskMember]
    next_after: Count | None
    authority: Literal['verified_manifest']


class TaskContentChunk(TraceDTO):
    ref: ResourceReference
    content: ArtifactContentReference
    offset: Count
    length: Count
    data_base64: str
    next_offset: Count | None


class TaskRecord(TraceDTO):
    position: Count
    delivery: Literal['inline', 'content_only']
    value: JsonValue


class TaskRecordPage(TraceDTO):
    ref: ResourceReference
    array_path: Literal['/spans']
    record_count: Count
    items: list[TaskRecord]
    next_after: Count | None
    order: Literal['source_array']
    max_items_bytes: Literal[262144]


Position = Annotated[int, Path(ge=0, le=99)]
MemberPosition = Annotated[int | None, Query(ge=0, le=9999)]
After = Annotated[int | None, Query(ge=0, lt=2147483647)]
Limit = Annotated[int, Query(ge=1, le=100)]


def task_principal(request):
    principal = getattr(request.state, 'task_principal', None)
    if not isinstance(principal, TaskPrincipal):
        raise AuthenticationError()
    return principal


def install(app, access, evidence):
    root = '/api/v1/projects/{project_id}/invocations/{invocation_id}/task-grants'
    errors = {code: {'model': TraceRouteError} for code in (401, 403, 404, 409, 413, 422, 500)}
    common = {'tags': ['Remote task access'], 'responses': errors}

    @app.post(root, response_model=TaskGrantAccepted, status_code=201, operation_id='grantTaskAccess',
              openapi_extra=_request_schema(TaskGrantRequest), **common)
    async def grant(project_id: ProjectPath, invocation_id: RunPath, request: Request):
        actor = require_project(request, project_id, 'invocations:execute')
        body = await _body(request, TaskGrantRequest)
        return await run_in_threadpool(access.grant, project_id, invocation_id, actor=actor, **body.model_dump(mode='json'))

    @app.post(root+'/{grant_id}/revoke', response_model=TaskGrantDescriptor, operation_id='revokeTaskAccess', **common)
    def revoke(project_id: ProjectPath, invocation_id: RunPath, grant_id: RunPath, request: Request):
        return access.revoke(project_id, invocation_id, grant_id=grant_id, actor=require_project(request, project_id, 'invocations:execute'))

    @app.get('/api/v1/task', response_model=TaskContext, operation_id='getTaskContext', **common)
    def context(request: Request):
        return access.context(task_principal(request))

    @app.get('/api/v1/task/inputs', response_model=TaskInputPage, operation_id='listTaskInputs', **common)
    def inputs(request: Request, limit: Limit = 50, after: After = None):
        return access.inputs(task_principal(request), limit=limit, after=after)

    @app.get('/api/v1/task/inputs/{position}', response_model=TaskResource, operation_id='getTaskInput', **common)
    def resource(position: Position, request: Request, member_position: MemberPosition = None):
        return evidence.descriptor(task_principal(request), position, member_position=member_position)

    @app.get('/api/v1/task/inputs/{position}/members', response_model=TaskMemberPage, operation_id='listTaskMembers', **common)
    def members(position: Position, request: Request, limit: Limit = 50, after: After = None):
        return evidence.members(task_principal(request), position, limit=limit, after=after)

    @app.get('/api/v1/task/inputs/{position}/content', response_model=TaskContentChunk, operation_id='readTaskContent', **common)
    def content(position: Position, request: Request, member_position: MemberPosition = None,
                offset: Annotated[int, Query(ge=0)] = 0, limit: Annotated[int, Query(ge=1, le=262144)] = 65536):
        return evidence.chunk(task_principal(request), position, member_position=member_position, offset=offset, limit=limit)

    @app.get('/api/v1/task/inputs/{position}/records', response_model=TaskRecordPage, operation_id='listTaskRecords', **common)
    def records(position: Position, request: Request, member_position: MemberPosition = None, limit: Limit = 50, after: After = None):
        return evidence.records(task_principal(request), position, member_position=member_position, limit=limit, after=after)
