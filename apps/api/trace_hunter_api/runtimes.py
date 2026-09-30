"""Versioned runtime registration and read-only discovery from cached observations."""
from typing import Annotated, Literal
from fastapi import Header, Path, Query, Request, Response
from pydantic import Field, JsonValue
from starlette.concurrency import run_in_threadpool

from trace_hunter.traces.service import MAX_REVISION
from .access import require_operator, require_project
from .artifacts import ArtifactSubmitter, ArtifactToken
from .invocations import OperationPath, OperationReference, OperationToken, _body, _request_schema
from .traces import Count, Digest, ProjectPath, Timestamp, TraceDTO, TraceRouteError

Revision = Annotated[int,Field(strict=True,ge=1,le=MAX_REVISION)]
RevisionPath = Annotated[int,Path(ge=1,le=MAX_REVISION)]


class NoRuntimeAuth(TraceDTO):
    kind: Literal['none']


class BearerRuntimeAuth(TraceDTO):
    kind: Literal['bearer']
    secret_ref: ArtifactToken


class RuntimeConfig(TraceDTO):
    name: Annotated[str,Field(min_length=1,max_length=256)]
    endpoint: Annotated[str,Field(min_length=1,max_length=2048,description='HTTP(S) base URL without embedded credentials, query or fragment.')]
    enabled: Annotated[bool,Field(strict=True)]
    auth: Annotated[NoRuntimeAuth|BearerRuntimeAuth,Field(discriminator='kind')]
    metadata: dict[str,JsonValue]


class RuntimeAppendRequest(TraceDTO):
    expected_previous: Annotated[int,Field(strict=True,ge=0,le=MAX_REVISION-1)]
    config: RuntimeConfig


class RuntimeReference(TraceDTO):
    kind: Literal['runtime_binding']
    id: OperationToken
    revision: Revision
    digest: Digest


class RuntimeIdentity(TraceDTO):
    project_id: str
    runtime_id: OperationToken
    revision: Revision
    ref: RuntimeReference
    created_at: Timestamp


class RuntimeDescriptor(RuntimeIdentity):
    schema_version: Literal['trace-hunter/runtime-binding/1']
    config: RuntimeConfig
    created_by: ArtifactSubmitter


class RuntimeSummary(RuntimeIdentity):
    name: str
    enabled: bool


class RuntimeAccepted(TraceDTO):
    created: bool
    binding: RuntimeDescriptor


class RuntimeHistory(TraceDTO):
    items: list[RuntimeSummary]
    next_before: Revision|None


class RuntimeCapabilities(TraceDTO):
    schema_version: Literal['trace-hunter/runtime-capabilities/1']
    name: Annotated[str,Field(min_length=1,max_length=256)]
    version: OperationToken
    protocols: list[ArtifactToken] = Field(min_length=1,max_length=8,json_schema_extra={'uniqueItems':True})
    operations: list[OperationReference] = Field(max_length=100)


FailureCode = Literal['credential_unavailable','http_error','unsupported_encoding','invalid_document',
                      'response_too_large','timeout','connection_error']


class ProbeSummary(TraceDTO):
    project_id: str
    runtime_id: OperationToken
    revision: Revision
    sequence: Revision
    state: Literal['incomplete','succeeded','failed']
    started_at: Timestamp
    completed_at: Timestamp|None
    elapsed_ms: Count|None
    document_digest: Digest|None
    protocol_supported: bool|None
    operation_count: Count|None
    failure_code: FailureCode|None
    http_status: Annotated[int,Field(ge=100,le=599)]|None
    requested_by: ArtifactSubmitter
    verification: Literal['advertised']


class ProbeDescriptor(ProbeSummary):
    capabilities: RuntimeCapabilities|None


class ProbePage(TraceDTO):
    items: list[ProbeSummary]
    next_before: Revision|None


class RuntimeObservation(TraceDTO):
    sequence: Revision|None
    state: Literal['unobserved','incomplete','succeeded','failed']
    completed_at: Timestamp|None
    age_ms: Count|None
    fresh: bool
    protocol_supported: bool|None
    operation_count: Count|None
    failure_code: FailureCode|None
    verification: Literal['advertised']


class ObservedRuntime(RuntimeSummary):
    observation: RuntimeObservation


class RuntimePage(TraceDTO):
    items: list[ObservedRuntime]
    next_cursor: str|None
    consistency: Literal['live_keyset']
    max_age_seconds: Annotated[int,Field(ge=1,le=3600)]
    operation: OperationReference|None


class RuntimeDiscoveryRequest(TraceDTO):
    operation: OperationReference
    max_age_seconds: Annotated[int,Field(strict=True,ge=1,le=3600)] = 300
    limit: Annotated[int,Field(strict=True,ge=1,le=100)] = 50
    cursor: Annotated[str,Field(min_length=1,max_length=4096)]|None = None


def install(app,runtimes):
    root='/api/v1/projects/{project_id}/runtimes'
    revision=root+'/{runtime_id}/revisions/{revision}'
    errors={code:{'model':TraceRouteError} for code in (401,403,404,409,413,415,422,500)}

    @app.post(root+'/{runtime_id}/revisions',response_model=RuntimeAccepted,operation_id='appendRuntimeBinding',tags=['Runtimes'],
              responses={201:{'model':RuntimeAccepted},**errors},openapi_extra=_request_schema(RuntimeAppendRequest))
    async def append(project_id:ProjectPath,runtime_id:OperationPath,request:Request,response:Response,
                     idempotency_key:Annotated[str,Header(alias='Idempotency-Key',min_length=1,max_length=512)]):
        actor=require_operator(request)
        body=await _body(request,RuntimeAppendRequest)
        result=await run_in_threadpool(runtimes.append,project_id,runtime_id,body.config.model_dump(mode='json'),
            expected_previous=body.expected_previous,request_key=idempotency_key,actor=actor)
        response.status_code=201 if result['created'] else 200
        return result

    @app.get(root,response_model=RuntimePage,operation_id='listRuntimeBindings',tags=['Runtimes'],responses=errors)
    def listing(project_id:ProjectPath,request:Request,limit:Annotated[int,Query(ge=1,le=100)]=50,
                cursor:Annotated[str,Query(min_length=1,max_length=4096)]|None=None,
                max_age_seconds:Annotated[int,Query(ge=1,le=3600)]=300):
        require_project(request,project_id,'runtimes:read')
        return runtimes.listing(project_id,limit=limit,cursor=cursor,max_age_seconds=max_age_seconds)

    @app.post(root+'/discover',response_model=RuntimePage,operation_id='discoverRuntimeBindings',tags=['Runtimes'],responses=errors)
    def discover(project_id:ProjectPath,body:RuntimeDiscoveryRequest,request:Request):
        actor=require_project(request,project_id,'runtimes:read');actor.require(project_id,'operations:read')
        return runtimes.listing(project_id,**body.model_dump(mode='json'))

    @app.get(revision,response_model=RuntimeDescriptor,operation_id='getRuntimeBinding',tags=['Runtimes'],responses=errors)
    def get(project_id:ProjectPath,runtime_id:OperationPath,revision:RevisionPath,request:Request):
        require_project(request,project_id,'runtimes:read')
        return runtimes.get(project_id,runtime_id,revision)

    @app.get(root+'/{runtime_id}/revisions',response_model=RuntimeHistory,operation_id='listRuntimeRevisions',tags=['Runtimes'],responses=errors)
    def history(project_id:ProjectPath,runtime_id:OperationPath,request:Request,
                limit:Annotated[int,Query(ge=1,le=100)]=50,before:Annotated[int,Query(ge=1,le=MAX_REVISION)]|None=None):
        require_project(request,project_id,'runtimes:read')
        return runtimes.history(project_id,runtime_id,limit=limit,before=before)

    @app.post(revision+'/probe',response_model=ProbeDescriptor,operation_id='probeRuntimeBinding',tags=['Runtimes'],responses=errors)
    async def probe(project_id:ProjectPath,runtime_id:OperationPath,revision:RevisionPath,request:Request):
        actor=require_project(request,project_id,'runtimes:probe')
        return await runtimes.aprobe(project_id,runtime_id,revision,actor=actor)

    @app.get(revision+'/probes',response_model=ProbePage,operation_id='listRuntimeProbes',tags=['Runtimes'],responses=errors)
    def probes(project_id:ProjectPath,runtime_id:OperationPath,revision:RevisionPath,request:Request,
               limit:Annotated[int,Query(ge=1,le=100)]=50,before:Annotated[int,Query(ge=1,le=MAX_REVISION)]|None=None):
        require_project(request,project_id,'runtimes:read')
        return runtimes.probes(project_id,runtime_id,revision,limit=limit,before=before)

    @app.get(revision+'/probes/{sequence}',response_model=ProbeDescriptor,operation_id='getRuntimeProbe',tags=['Runtimes'],responses=errors)
    def probe_result(project_id:ProjectPath,runtime_id:OperationPath,revision:RevisionPath,sequence:RevisionPath,request:Request):
        require_project(request,project_id,'runtimes:read')
        return runtimes.get_probe(project_id,runtime_id,revision,sequence)
