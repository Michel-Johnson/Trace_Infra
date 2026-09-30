"""Project-authorized Span query and duration analysis transport."""
from typing import Annotated, Literal

from fastapi import HTTPException, Request
from pydantic import Field, ValidationError

from trace_hunter.query import SpanQuery
from .access import current_principal, require_project
from .traces import ProjectPath, RevisionNumber, TraceDTO, TraceRouteError, TraceSourceReference

SpanField = Literal[
    "run_id", "revision", "source_ordinal", "span_id", "kind", "name", "operation", "status",
    "agent_id", "segment_id", "turn_id", "parent_id", "stream_id", "sequence", "clock_id",
    "start_ms", "end_ms", "source_reported_duration_ms", "interval_duration_ms", "duration_ms",
    "duration_basis", "duration_scope", "skill_name", "skill_action", "proposal_id", "call_id",
    "invocation_id", "attempt", "context_id", "visibility_status", "visibility_issues", "source_refs",
]
StringValues = Annotated[list[Annotated[str, Field(min_length=1, max_length=512)]], Field(min_length=1, max_length=50)]
Duration = Annotated[float, Field(ge=0)]


class SpanQueryFilters(TraceDTO):
    run_id: StringValues = Field(default_factory=list)
    kind: StringValues = Field(default_factory=list)
    name: StringValues = Field(default_factory=list)
    operation: StringValues = Field(default_factory=list)
    status: StringValues = Field(default_factory=list)
    agent_id: StringValues = Field(default_factory=list)
    segment_id: StringValues = Field(default_factory=list)
    turn_id: StringValues = Field(default_factory=list)
    parent_id: StringValues = Field(default_factory=list)
    skill_name: StringValues = Field(default_factory=list)
    skill_action: StringValues = Field(default_factory=list)
    proposal_id: StringValues = Field(default_factory=list)
    call_id: StringValues = Field(default_factory=list)
    invocation_id: StringValues = Field(default_factory=list)
    context_id: StringValues = Field(default_factory=list)
    visibility_status: StringValues = Field(default_factory=list)
    min_duration_ms: Duration | None = None
    max_duration_ms: Duration | None = None


class SpanQueryRequest(TraceDTO):
    filters: SpanQueryFilters = Field(default_factory=SpanQueryFilters)
    fields: list[SpanField] = Field(default_factory=lambda: [
        "run_id", "revision", "span_id", "kind", "name", "status", "skill_name",
        "skill_action", "duration_ms", "start_ms", "end_ms", "source_refs"],
        min_length=1, max_length=32, json_schema_extra={"uniqueItems": True})
    revisions: Literal["latest", "all"] = "latest"
    order: Literal["source", "duration_desc"] = "source"
    limit: Annotated[int, Field(strict=True, ge=1, le=100)] = 50
    cursor: Annotated[str, Field(min_length=1, max_length=4096)] | None = None


class SpanQueryItem(TraceDTO):
    run_id: str | None = None
    revision: RevisionNumber | None = None
    source_ordinal: int | None = None
    span_id: str | None = None
    kind: str | None = None
    name: str | None = None
    operation: str | None = None
    status: str | None = None
    agent_id: str | None = None
    segment_id: str | None = None
    turn_id: str | None = None
    parent_id: str | None = None
    stream_id: str | None = None
    sequence: int | None = None
    clock_id: str | None = None
    start_ms: Duration | None = None
    end_ms: Duration | None = None
    source_reported_duration_ms: Duration | None = None
    interval_duration_ms: Duration | None = None
    duration_ms: Duration | None = None
    duration_basis: str | None = None
    duration_scope: str | None = None
    skill_name: str | None = None
    skill_action: Literal["load", "invoke"] | None = None
    proposal_id: str | None = None
    call_id: str | None = None
    invocation_id: str | None = None
    attempt: int | None = None
    context_id: str | None = None
    visibility_status: Literal["pass", "fail", "unknown"] | None = None
    visibility_issues: list[dict] | None = None
    source_refs: list[TraceSourceReference] | None = None


class SpanQueryResult(TraceDTO):
    items: list[SpanQueryItem]
    next_cursor: str | None
    query_digest: str
    projector_version: str
    consistency: Literal["live_keyset"]


class SpanWindowAnchor(TraceDTO):
    run_id: str
    revision: RevisionNumber
    span_id: str


class SpanWindowRequest(TraceDTO):
    anchor: SpanWindowAnchor
    before: Annotated[int, Field(strict=True, ge=0, le=100)] = 20
    after: Annotated[int, Field(strict=True, ge=0, le=100)] = 20
    fields: list[SpanField] = Field(default_factory=lambda: [
        "run_id", "revision", "source_ordinal", "span_id", "kind", "name", "operation",
        "status", "skill_name", "skill_action", "duration_ms", "source_refs"],
        min_length=1, max_length=32, json_schema_extra={"uniqueItems": True})
    include: list[Literal["documents", "related_objects", "edges"]] = Field(
        default_factory=list, max_length=3, json_schema_extra={"uniqueItems": True})
    preview_chars: Annotated[int, Field(strict=True, ge=1, le=2048)] = 512


class SpanWindowDocument(TraceDTO):
    object_kind: str
    object_id: str
    span_id: str | None = None
    field: str
    preview: str
    preview_truncated: bool
    text_state: Literal["exact", "truncated"]
    source_refs: list[TraceSourceReference]


class SpanWindowObject(TraceDTO):
    object_kind: str
    object_id: str
    span_id: str | None = None
    name: str | None = None
    context_id: str | None = None
    source_refs: list[TraceSourceReference]


class SpanWindowEdge(TraceDTO):
    source_kind: str
    source_id: str
    relation: str
    target_kind: str
    target_id: str
    position: int | None = None
    payload: dict


class SpanWindowOrdering(TraceDTO):
    basis: Literal["source_ordinal"]
    causal: Literal[False]


class SpanWindowBounds(TraceDTO):
    first_ordinal: Annotated[int, Field(ge=0)]
    last_ordinal: Annotated[int, Field(ge=0)]
    returned_before: Annotated[int, Field(ge=0, le=100)]
    returned_after: Annotated[int, Field(ge=0, le=100)]
    has_more_before: bool
    has_more_after: bool


class SpanWindowResult(TraceDTO):
    version: Literal["span-window/1"]
    projector_version: str
    anchor: SpanWindowAnchor
    ordering: SpanWindowOrdering
    bounds: SpanWindowBounds
    spans: list[SpanQueryItem]
    documents: list[SpanWindowDocument]
    related_objects: list[SpanWindowObject]
    edges: list[SpanWindowEdge]
    attachments_truncated: bool


class SpanQueryCapabilities(TraceDTO):
    version: str
    fields: list[SpanField]
    filter_fields: list[str]
    duration_filters: list[str]
    orders: list[Literal["source", "duration_desc"]]
    revision_modes: list[Literal["latest", "all"]]
    max_filter_values: int
    max_page_size: int
    max_item_bytes: int
    projector_version: str
    source_content: Literal[False]
    triggers_analysis: Literal[False]
    consistency: Literal["live_keyset"]
    window: dict


def install(app, query: SpanQuery, reply):
    errors = {code: {"model": TraceRouteError} for code in (401, 403, 422, 500, 503)}

    @app.get("/api/v1/span-query-capabilities", response_model=SpanQueryCapabilities,
             operation_id="getSpanQueryCapabilities", tags=["Span queries"], responses=errors)
    def capabilities(request: Request):
        current_principal(request)
        return SpanQuery.capabilities()

    @app.post("/api/v1/projects/{project_id}/spans/query", response_model=SpanQueryResult,
              operation_id="querySpans", tags=["Span queries"], responses=errors)
    def query_spans(project_id: ProjectPath, body: SpanQueryRequest, request: Request):
        require_project(request, project_id, "traces:read")
        result = query.query(project_id, body.model_dump(exclude_unset=True))
        try:
            return reply(SpanQueryResult.model_validate(result).model_dump(exclude_unset=True))
        except ValidationError:
            raise HTTPException(500, "Span query response does not match its contract") from None

    @app.post("/api/v1/projects/{project_id}/spans/window", response_model=SpanWindowResult,
              operation_id="getSpanWindow", tags=["Span queries"], responses=errors)
    def span_window(project_id: ProjectPath, body: SpanWindowRequest, request: Request):
        require_project(request, project_id, "traces:read")
        result = query.window(project_id, body.model_dump(exclude_unset=True))
        try:
            return reply(SpanWindowResult.model_validate(result).model_dump(exclude_unset=True))
        except ValidationError:
            raise HTTPException(500, "Span window response does not match its contract") from None
