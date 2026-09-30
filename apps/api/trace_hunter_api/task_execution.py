"""Remote execution callbacks cannot select an arbitrary task or attempt."""
from typing import Annotated, Literal
from fastapi import Path, Request, Response
from pydantic import Field
from starlette.concurrency import run_in_threadpool
from .access import require_project
from .execution import AttemptDescriptor, AttemptNumber, ExecutionError, ExecutionOutput, ExecutionReceipt, decode_outputs
from .invocations import _body, _request_schema
from .runtimes import RuntimeReference
from .task_access import task_principal
from .traces import TraceDTO, TraceRouteError, ProjectPath, RunPath, Digest


class EmptyTaskExecutionRequest(TraceDTO):
    pass


class TaskCompleteRequest(TraceDTO):
    outputs: list[ExecutionOutput] = Field(max_length=100)


class TaskFailRequest(TraceDTO):
    error: ExecutionError


class RemoteResultSource(TraceDTO):
    schema_version: Literal['trace-hunter/remote-result-source/1']
    project_id: str
    invocation_id: str
    attempt: AttemptNumber
    grant_id: str
    runtime: RuntimeReference
    probe_sequence: Annotated[int, Field(ge=1)]
    receipt_digest: Digest
    digest: Digest


class TaskResultAccepted(TraceDTO):
    created: bool
    result: ExecutionReceipt
    delegation: RemoteResultSource | None


class TaskResultObserved(TraceDTO):
    result: ExecutionReceipt | None
    delegation: RemoteResultSource | None


class TaskExecutionState(TraceDTO):
    project_id: str
    invocation_id: str
    attempt: AttemptDescriptor
    latest_attempt: AttemptNumber
    invocation_status: Literal['pending', 'running', 'succeeded', 'failed', 'cancelled', 'blocked']
    can_execute: bool
    should_stop: bool


REQUEST_MODELS = (EmptyTaskExecutionRequest, TaskCompleteRequest, TaskFailRequest)


def install(app, execution):
    common = {'tags': ['Remote task execution'], 'responses': {code: {'model': TraceRouteError} for code in (401,403,404,409,413,415,422,500)}}

    @app.get('/api/v1/task/state', response_model=TaskExecutionState, operation_id='getTaskExecutionState', **common)
    def state(request: Request):
        return execution.state(task_principal(request))

    @app.get('/api/v1/task/result', response_model=TaskResultObserved, operation_id='getTaskResult', **common)
    def result(request: Request):
        return execution.result(task_principal(request))

    @app.post('/api/v1/task/heartbeat', response_model=AttemptDescriptor, operation_id='heartbeatTaskExecution',
              openapi_extra=_request_schema(EmptyTaskExecutionRequest), **common)
    async def heartbeat(request: Request):
        await _body(request, EmptyTaskExecutionRequest)
        return await run_in_threadpool(execution.heartbeat, task_principal(request))

    accepted = {**common, 'responses': {201: {'model': TaskResultAccepted}, **common['responses']}}

    @app.post('/api/v1/task/complete', response_model=TaskResultAccepted, operation_id='completeTaskExecution',
              openapi_extra=_request_schema(TaskCompleteRequest), **accepted)
    async def complete(request: Request, response: Response):
        body = await _body(request, TaskCompleteRequest)
        result = await run_in_threadpool(execution.complete, task_principal(request), outputs=decode_outputs(body.outputs))
        response.status_code = 201 if result['created'] else 200
        return result

    @app.post('/api/v1/task/fail', response_model=TaskResultAccepted, operation_id='failTaskExecution',
              openapi_extra=_request_schema(TaskFailRequest), **accepted)
    async def fail(request: Request, response: Response):
        body = await _body(request, TaskFailRequest)
        result = await run_in_threadpool(execution.fail, task_principal(request), error=body.error.model_dump())
        response.status_code = 201 if result['created'] else 200
        return result

    @app.get('/api/v1/projects/{project_id}/invocations/{invocation_id}/results/{attempt}/remote',
             response_model=TaskResultObserved, operation_id='getRemoteResultAttribution', **common)
    def attribution(project_id: ProjectPath, invocation_id: RunPath, attempt: Annotated[int, Path(ge=1,le=2147483647)], request: Request):
        require_project(request, project_id, 'invocations:read')
        return execution.result_for(project_id, invocation_id, attempt)
