"""Operation discovery and explicit neutral computation requests."""

from typing import Annotated, Literal
from fastapi import Header, HTTPException, Path, Query, Request, Response
from pydantic import Field, JsonValue, ValidationError
from starlette.concurrency import run_in_threadpool

from trace_hunter.invocations.service import URL_TOKEN
from .access import require_operator, require_project
from .artifacts import ArtifactInput, ArtifactSubmitter, ArtifactToken
from .traces import Count, Digest, ProjectPath, RunPath, Timestamp, TraceDTO, TraceRouteError, document_bytes

OperationToken = Annotated[str, Field(pattern='^'+URL_TOKEN.pattern.removesuffix(r'\Z')+'$', max_length=128)]
OperationPath = Annotated[str, Path(pattern='^'+URL_TOKEN.pattern.removesuffix(r'\Z')+'$', max_length=128)]
Bound = Annotated[int, Field(strict=True, ge=0, le=100)]


class OperationImplementation(TraceDTO):
    host: Literal['worker','remote']
    key: ArtifactToken
    package_digest: Digest


class OperationInputRole(TraceDTO):
    role: ArtifactToken
    kinds: list[Literal['trace_revision','selection_snapshot','artifact']] = Field(min_length=1,max_length=3,json_schema_extra={'uniqueItems':True})
    min_items: Bound
    max_items: Bound


class OperationOutputRole(TraceDTO):
    role: ArtifactToken
    artifact_type: ArtifactToken
    min_items: Bound
    max_items: Bound


class OperationDefinition(TraceDTO):
    schema_version: Literal['trace-hunter/operation/1']
    operation_id: OperationToken
    version: OperationToken
    implementation: OperationImplementation
    input_roles: list[OperationInputRole] = Field(max_length=100)
    config_schema: dict[str,JsonValue]
    outputs: list[OperationOutputRole] = Field(max_length=100)


class OperationReference(TraceDTO):
    operation_id: OperationToken
    version: OperationToken
    digest: Digest


class OperationIdentity(TraceDTO):
    project_id: str
    ref: OperationReference
    package_verification: Literal['declared']
    created_at: Timestamp


class OperationDescriptor(OperationIdentity):
    definition: OperationDefinition


class OperationSummary(OperationIdentity):
    implementation: OperationImplementation
    input_role_count: Count
    output_role_count: Count


class OperationAccepted(TraceDTO):
    created: bool
    operation: OperationDescriptor


class OperationPage(TraceDTO):
    items: list[OperationSummary]
    next_cursor: str | None
    consistency: Literal['live_keyset']


class InvocationRequest(TraceDTO):
    operation: OperationReference
    inputs: list[ArtifactInput] = Field(max_length=100)
    config: dict[str,JsonValue]


class InvocationIdentity(TraceDTO):
    schema_version: Literal['trace-hunter/invocation/1']
    kind: Literal['invocation']
    project_id: str
    invocation_id: str
    spec_digest: Digest
    status: Literal['pending','running','succeeded','failed','cancelled','blocked']
    operation: OperationReference
    requested_by: ArtifactSubmitter
    created_at: Timestamp


class InvocationDescriptor(InvocationIdentity):
    inputs: list[ArtifactInput]
    config: dict[str,JsonValue]


class InvocationSummary(InvocationIdentity):
    input_count: Count


class InvocationAccepted(TraceDTO):
    created: bool
    invocation: InvocationDescriptor


class InvocationPage(TraceDTO):
    items: list[InvocationSummary]
    next_cursor: str | None
    consistency: Literal['live_keyset']


def _request_schema(model):
    return {'requestBody':{'required':True,'content':{'application/json':{'schema':{'$ref':'#/components/schemas/'+model.__name__}}}}}


async def _body(request, model):
    raw = await document_bytes(request)
    try:
        return model.model_validate_json(raw)
    except ValidationError:
        raise HTTPException(422,'Request does not match the operation protocol') from None


def install(app, invocations):
    root='/api/v1/projects/{project_id}'
    errors={code:{'model':TraceRouteError} for code in (401,403,404,409,413,415,422,500)}

    @app.post(root+'/operations',response_model=OperationAccepted,operation_id='registerOperationVersion',tags=['Operations'],
              responses={201:{'model':OperationAccepted},**errors},openapi_extra=_request_schema(OperationDefinition))
    async def register(project_id:ProjectPath,request:Request,response:Response):
        require_operator(request)
        definition=await _body(request,OperationDefinition)
        result=await run_in_threadpool(invocations.register_operation,project_id,definition.model_dump(mode='json'))
        response.status_code=201 if result['created'] else 200
        return result

    @app.get(root+'/operations',response_model=OperationPage,operation_id='listOperationVersions',tags=['Operations'],responses=errors)
    def operations(project_id:ProjectPath,request:Request,limit:Annotated[int,Query(ge=1,le=100)]=50,
                   cursor:Annotated[str,Query(min_length=1,max_length=4096)]|None=None):
        require_project(request,project_id,'operations:read')
        return invocations.list_operations(project_id,limit=limit,cursor=cursor)

    @app.get(root+'/operations/{operation_id}/versions/{version}',response_model=OperationDescriptor,
             operation_id='getOperationVersion',tags=['Operations'],responses=errors)
    def operation(project_id:ProjectPath,operation_id:OperationPath,version:OperationPath,request:Request):
        require_project(request,project_id,'operations:read')
        return invocations.get_operation(project_id,operation_id,version)

    @app.post(root+'/invocations',response_model=InvocationAccepted,operation_id='createInvocation',tags=['Invocations'],
              responses={201:{'model':InvocationAccepted},**errors},openapi_extra=_request_schema(InvocationRequest))
    async def create(project_id:ProjectPath,request:Request,response:Response,
                     idempotency_key:Annotated[str,Header(alias='Idempotency-Key',min_length=1,max_length=512)]):
        actor=require_project(request,project_id,'invocations:write')
        actor.require(project_id,'operations:read')
        body=await _body(request,InvocationRequest)
        for kind in {item.ref.kind for item in body.inputs}:
            actor.require(project_id,'artifacts:read' if kind=='artifact' else 'traces:read')
        result=await run_in_threadpool(invocations.create,project_id,body.model_dump(mode='json'),request_key=idempotency_key,actor=actor)
        response.status_code=201 if result['created'] else 200
        return result

    @app.get(root+'/invocations',response_model=InvocationPage,operation_id='listInvocations',tags=['Invocations'],responses=errors)
    def listing(project_id:ProjectPath,request:Request,limit:Annotated[int,Query(ge=1,le=100)]=50,
                cursor:Annotated[str,Query(min_length=1,max_length=4096)]|None=None):
        require_project(request,project_id,'invocations:read')
        return invocations.list(project_id,limit=limit,cursor=cursor)

    @app.get(root+'/invocations/{invocation_id}',response_model=InvocationDescriptor,operation_id='getInvocation',tags=['Invocations'],responses=errors)
    def get(project_id:ProjectPath,invocation_id:RunPath,request:Request):
        require_project(request,project_id,'invocations:read')
        return invocations.get(project_id,invocation_id)
