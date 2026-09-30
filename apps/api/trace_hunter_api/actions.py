"""Task-bound external action bookkeeping and authorized reconciliation."""
from typing import Annotated, Literal
from fastapi import Path, Query, Request, Response
from pydantic import Field, JsonValue
from starlette.concurrency import run_in_threadpool
from .access import require_project
from .artifacts import ArtifactReference, ArtifactToken, ResourceId
from .execution import AttemptNumber
from .invocations import _body, _request_schema
from .task_access import task_principal
from .traces import Digest, ProjectPath, RunPath, Timestamp, TraceDTO, TraceRouteError

ActionPath = Annotated[str, Path(pattern=r'^act_[0-9a-f]{32}$')]
Limit = Annotated[int, Query(ge=1, le=100)]
After = Annotated[str | None, Query(max_length=512)]
EventAfter = Annotated[int, Query(ge=0, le=2147483647)]
Outcome = Literal['applied', 'not_applied', 'outcome_unknown']


class ExternalActionReference(TraceDTO):
    kind: Literal['external_action']
    id: str
    digest: Digest


class ExternalActionSummary(TraceDTO):
    project_id: str
    action_id: str
    invocation_id: str
    ref: ExternalActionReference
    action_key: str
    operation: str
    created_attempt: AttemptNumber
    created_grant: str
    state: Literal['prepared', 'running', 'outcome_unknown', 'applied', 'not_applied', 'abandoned']
    version: AttemptNumber
    current_execution: Annotated[int, Field(ge=0, le=2147483647)]


class ExternalAction(ExternalActionSummary):
    request: dict[str, JsonValue]


class ExternalActionPage(TraceDTO):
    items: list[ExternalActionSummary]
    next_after: str | None


class ExternalActionActor(TraceDTO):
    kind: Literal['operator', 'service', 'task']
    principal_id: str | None
    grant_id: str | None = None
    attempt: AttemptNumber | None = None


class ExternalActionEvent(TraceDTO):
    schema_version: Literal['trace-hunter/external-action-event/1']
    project_id: str
    action_id: str
    version: AttemptNumber
    event_kind: Literal['prepared', 'started', 'reported', 'reconciled', 'abandoned', 'outcome_unknown']
    state: Literal['prepared', 'running', 'outcome_unknown', 'applied', 'not_applied', 'abandoned']
    execution: Annotated[int, Field(ge=0)]
    actor: ExternalActionActor
    details: dict[str, JsonValue]
    recorded_at: Timestamp
    digest: Digest


class ExternalActionEventPage(TraceDTO):
    items: list[ExternalActionEvent]
    next_after: int | None


class ActionPrepareRequest(TraceDTO):
    action_key: ResourceId
    operation: ArtifactToken
    request: dict[str, JsonValue]


class ActionVersionRequest(TraceDTO):
    expected_version: AttemptNumber


class ActionReportRequest(TraceDTO):
    execution: AttemptNumber
    fence_id: ResourceId
    outcome: Outcome
    details: dict[str, JsonValue]
    request_key: ResourceId


class ActionReconcileRequest(ActionVersionRequest):
    outcome: Literal['applied', 'not_applied']
    evidence: ArtifactReference
    reason: Annotated[str, Field(min_length=1, max_length=1024)]


class ActionPrepared(TraceDTO):
    created: bool
    action: ExternalAction


class ActionExecution(TraceDTO):
    execution: AttemptNumber
    invocation_attempt: AttemptNumber
    grant_id: str
    fence_id: str
    start_version: AttemptNumber
    state: Outcome | Literal['running']
    started_at: Timestamp
    provider_key: str


class ActionBegan(ActionPrepared):
    can_send: bool
    execution: ActionExecution


class ActionReported(TraceDTO):
    created: bool
    receipt: ExternalActionEvent


class ActionReconciled(TraceDTO):
    action: ExternalAction
    receipt: ExternalActionEvent


REQUEST_MODELS = (ActionPrepareRequest, ActionVersionRequest, ActionReportRequest, ActionReconcileRequest)
TASK_OPERATIONS = frozenset(('prepareTaskAction', 'beginTaskAction', 'reportTaskAction', 'abandonTaskAction',
                             'listTaskActions', 'getTaskAction', 'listTaskActionEvents'))


def install(app, actions):
    common = {'tags':['External actions'], 'response_model_exclude_unset':True,
              'responses':{c:{'model':TraceRouteError} for c in (401,403,404,409,413,415,422,500)}}
    root = '/api/v1/task/actions'
    project = '/api/v1/projects/{project_id}'

    @app.post(root, response_model=ActionPrepared, operation_id='prepareTaskAction',
              openapi_extra=_request_schema(ActionPrepareRequest), **common)
    async def prepare(request: Request, response: Response):
        body = await _body(request, ActionPrepareRequest)
        result = await run_in_threadpool(actions.prepare, task_principal(request), **body.model_dump())
        response.status_code = 201 if result['created'] else 200
        return result

    @app.get(root, response_model=ExternalActionPage, operation_id='listTaskActions', **common)
    def task_list(request: Request, limit: Limit=50, after: After=None):
        return actions.task_list(task_principal(request), limit=limit, after=after)

    @app.get(root+'/{action_id}', response_model=ExternalAction, operation_id='getTaskAction', **common)
    def task_get(action_id: ActionPath, request: Request):
        return actions.task_get(task_principal(request), action_id)

    @app.get(root+'/{action_id}/events', response_model=ExternalActionEventPage, operation_id='listTaskActionEvents', **common)
    def task_events(action_id: ActionPath, request: Request, limit: Limit=50, after: EventAfter=0):
        return actions.task_events(task_principal(request), action_id, limit=limit, after=after)

    @app.post(root+'/{action_id}/begin', response_model=ActionBegan, operation_id='beginTaskAction',
              openapi_extra=_request_schema(ActionVersionRequest), **common)
    async def begin(action_id: ActionPath, request: Request, response: Response):
        body = await _body(request, ActionVersionRequest)
        result = await run_in_threadpool(actions.begin, task_principal(request), action_id, **body.model_dump())
        response.status_code = 201 if result['created'] else 200
        return result

    @app.post(root+'/{action_id}/report', response_model=ActionReported, operation_id='reportTaskAction',
              openapi_extra=_request_schema(ActionReportRequest), **common)
    async def report(action_id: ActionPath, request: Request, response: Response):
        body = await _body(request, ActionReportRequest)
        result = await run_in_threadpool(actions.report, task_principal(request), action_id, **body.model_dump())
        response.status_code = 201 if result['created'] else 200
        return result

    @app.post(root+'/{action_id}/abandon', response_model=ExternalAction, operation_id='abandonTaskAction',
              openapi_extra=_request_schema(ActionVersionRequest), **common)
    async def abandon(action_id: ActionPath, request: Request):
        body = await _body(request, ActionVersionRequest)
        return await run_in_threadpool(actions.abandon, task_principal(request), action_id, **body.model_dump())

    @app.get(project+'/invocations/{invocation_id}/actions', response_model=ExternalActionPage, operation_id='listInvocationActions', **common)
    def project_list(project_id: ProjectPath, invocation_id: RunPath, request: Request, limit: Limit=50, after: After=None):
        require_project(request, project_id, 'invocations:read')
        return actions.list_for(project_id, invocation_id, limit=limit, after=after)

    @app.get(project+'/actions/{action_id}', response_model=ExternalAction, operation_id='getExternalAction', **common)
    def project_get(project_id: ProjectPath, action_id: ActionPath, request: Request):
        require_project(request, project_id, 'invocations:read')
        return actions.get(project_id, action_id)

    @app.get(project+'/actions/{action_id}/events', response_model=ExternalActionEventPage, operation_id='listExternalActionEvents', **common)
    def project_events(project_id: ProjectPath, action_id: ActionPath, request: Request, limit: Limit=50, after: EventAfter=0):
        require_project(request, project_id, 'invocations:read')
        return actions.events(project_id, action_id, limit=limit, after=after)

    @app.post(project+'/actions/{action_id}/reconcile', response_model=ActionReconciled, operation_id='reconcileExternalAction',
              openapi_extra=_request_schema(ActionReconcileRequest), **common)
    async def reconcile(project_id: ProjectPath, action_id: ActionPath, request: Request):
        actor = require_project(request, project_id, 'invocations:write')
        body = await _body(request, ActionReconcileRequest)
        return await run_in_threadpool(actions.reconcile, project_id, action_id, actor=actor, **body.model_dump())
