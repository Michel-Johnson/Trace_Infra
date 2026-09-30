"""Public transport for snapshot-bound evidence queries and metrics."""

from typing import Annotated, Any, Literal

from fastapi import Request
from pydantic import Field

from .access import current_principal, require_project
from .traces import ProjectPath, TraceDTO, errors


class VisibleTarget(TraceDTO):
    run_id: str
    revision: Annotated[int, Field(strict=True, ge=1)]
    model_span_id: str


class QueryScopeRequest(TraceDTO):
    mode: Literal["analysis", "model_context"]
    visible_to: VisibleTarget | None = None


class AttributePredicate(TraceDTO):
    path: Annotated[str, Field(min_length=1, max_length=512)]
    op: Literal["eq", "neq", "exists"] = "eq"
    value: Any = None


class TimeRange(TraceDTO):
    from_: str | None = Field(default=None, alias="from")
    to: str | None = None


class ObjectQueryRequest(TraceDTO):
    scope: QueryScopeRequest = Field(default_factory=lambda: QueryScopeRequest(mode="analysis"))
    revisions: Literal["latest", "all"] = "latest"
    filters: dict[str, list[Any]] = Field(default_factory=dict)
    attributes: list[AttributePredicate] = Field(default_factory=list, max_length=20)
    trace_attributes: list[AttributePredicate] = Field(default_factory=list, max_length=20)
    time: TimeRange = Field(default_factory=TimeRange)
    fields: list[str] | None = None
    order: Literal["source", "time"] = "source"
    limit: Annotated[int, Field(strict=True, ge=1, le=1000)] = 100
    cursor: str | None = None
    snapshot: str | None = None


class MetricsRequest(TraceDTO):
    query: dict = Field(default_factory=dict)
    metrics: list[Literal["count", "rate", "error_rate", "sum", "avg", "p50", "p95", "p99", "histogram"]] = Field(
        default_factory=lambda: ["count", "error_rate", "p50", "p95", "p99"])
    group_by: list[str] = Field(default_factory=list, max_length=5)
    interval_seconds: Annotated[int, Field(strict=True, ge=1, le=86400)] | None = None
    histogram: list[Annotated[float, Field(ge=0)]] = Field(default_factory=lambda: [10, 100, 1000, 10000])
    exemplars: Annotated[int, Field(strict=True, ge=0, le=20)] = 3


class GenericResult(TraceDTO):
    version: str
    projector_version: str | None = None
    snapshot: str | None = None
    query_digest: str | None = None
    scope: dict
    items: list[dict] | None = None
    groups: list[dict] | None = None
    nodes: list[dict] | None = None
    edges: list[dict] | None = None
    coverage: dict | None = None
    next_cursor: str | None = None
    truncated: bool | None = None
    partial: bool | None = None
    warnings: list[str] | None = None
    anchor: dict | None = None


def install(app, service, reply):
    responses = errors(404, 422, 500, 503)

    def authorize(project_id, scope, request):
        if scope.mode == "model_context":
            require_project(request, project_id, "traces:model_context")
        else:
            principal = require_project(request, project_id, "traces:read")
            principal.require(project_id, "traces:search:analysis")

    @app.get("/api/v1/advanced-query-capabilities", operation_id="getAdvancedQueryCapabilities",
             tags=["Advanced queries"], responses=responses)
    def capabilities(request: Request):
        current_principal(request)
        return reply(service.capabilities())

    @app.post("/api/v1/projects/{project_id}/objects/query", response_model=GenericResult,
              operation_id="queryObjects", tags=["Advanced queries"], responses=responses)
    def objects(project_id: ProjectPath, body: ObjectQueryRequest, request: Request):
        authorize(project_id, body.scope, request)
        return reply(service.query(project_id, body.model_dump(by_alias=True, exclude_none=True)))

    @app.post("/api/v1/projects/{project_id}/metrics/query", response_model=GenericResult,
              operation_id="queryTraceMetrics", tags=["Advanced queries"], responses=responses)
    def metrics(project_id: ProjectPath, body: MetricsRequest, request: Request):
        scope = QueryScopeRequest.model_validate(body.query.get("scope", {"mode": "analysis"}))
        authorize(project_id, scope, request)
        return reply(service.metrics(project_id, body.model_dump(exclude_none=True)))
