"""Read-only, project-authorized query transport."""

from typing import Annotated, Literal

from fastapi import HTTPException, Request
from pydantic import Field, ValidationError

from trace_hunter.query import TraceQuery
from .access import current_principal, require_project
from .traces import TraceDTO, TraceRouteError, ProjectPath, Digest, Timestamp, RevisionNumber

QueryField = Literal[
    "run_id", "revision", "content_digest", "format_version", "created_at", "query_id", "env_id",
    "harness", "model", "status", "index_state", "record_count", "model_count", "tool_count", "indexed_at",
]
FilterValue = Annotated[str, Field(min_length=1, max_length=512)]
FilterValues = Annotated[list[FilterValue], Field(min_length=1, max_length=50)]
MetadataValue = Annotated[str, Field(max_length=4096)] | None
MetadataValues = Annotated[list[MetadataValue], Field(min_length=1, max_length=50)]


class TraceQueryFilters(TraceDTO):
    run_id: FilterValues = Field(default_factory=list)
    content_digest: FilterValues = Field(default_factory=list)
    query_id: MetadataValues = Field(default_factory=list)
    env_id: MetadataValues = Field(default_factory=list)
    harness: MetadataValues = Field(default_factory=list)
    model: MetadataValues = Field(default_factory=list)
    status: MetadataValues = Field(default_factory=list)
    index_state: FilterValues = Field(default_factory=list)


class TraceQueryRequest(TraceDTO):
    filters: TraceQueryFilters = Field(default_factory=TraceQueryFilters)
    fields: list[QueryField] = Field(default_factory=lambda: ["run_id", "revision", "content_digest", "index_state", "record_count"], min_length=1, max_length=15, json_schema_extra={"uniqueItems": True})
    revisions: Literal["latest", "all"] = "latest"
    order: Literal["run_id_asc_revision_asc", "created_at_desc"] = "run_id_asc_revision_asc"
    limit: Annotated[int, Field(strict=True, ge=1, le=100)] = 50
    cursor: Annotated[str, Field(min_length=1, max_length=4096)] | None = None


class TraceQueryItem(TraceDTO):
    run_id: str | None = None
    revision: RevisionNumber | None = None
    content_digest: Digest | None = None
    format_version: str | None = None
    created_at: Timestamp | None = None
    query_id: str | None = None
    env_id: str | None = None
    harness: str | None = None
    model: str | None = None
    status: str | None = None
    index_state: Literal["complete", "failed", "unindexed"] | None = None
    record_count: Annotated[int, Field(ge=0)] | None = None
    model_count: Annotated[int, Field(ge=0)] | None = None
    tool_count: Annotated[int, Field(ge=0)] | None = None
    indexed_at: Timestamp | None = None


class TraceQueryResult(TraceDTO):
    items: list[TraceQueryItem]
    next_cursor: str | None
    query_digest: str
    projector_version: str
    consistency: Literal["live_keyset"]


class TraceQueryCapabilities(TraceDTO):
    version: str
    fields: list[QueryField]
    filter_fields: list[str]
    filter_operator: Literal["any_of"]
    max_filter_values: int
    max_page_size: int
    max_item_bytes: int
    revision_modes: list[Literal["latest", "all"]]
    order: Literal["run_id_asc_revision_asc"]
    orders: list[Literal["run_id_asc_revision_asc", "created_at_desc"]]
    consistency: Literal["live_keyset"]
    projector_version: str
    source_content: Literal[False]
    triggers_analysis: Literal[False]


def install(app, query: TraceQuery, reply):
    errors = {code: {"model": TraceRouteError} for code in (401, 403, 422, 500, 503)}

    @app.get("/api/v1/query-capabilities", response_model=TraceQueryCapabilities,
             operation_id="getTraceQueryCapabilities", tags=["Trace queries"], responses=errors)
    def capabilities(request: Request):
        current_principal(request)
        return TraceQuery.capabilities()

    @app.post("/api/v1/projects/{project_id}/traces/query", response_model=TraceQueryResult,
              operation_id="queryTraceRevisions", tags=["Trace queries"], responses=errors)
    def query_traces(project_id: ProjectPath, body: TraceQueryRequest, request: Request):
        require_project(request, project_id, "traces:read")
        value = body.model_dump(exclude_unset=True)
        result = query.query(project_id, value)
        try:
            payload = TraceQueryResult.model_validate(result).model_dump(exclude_unset=True)
        except ValidationError:
            raise HTTPException(500, "Query response does not match its contract") from None
        return reply(payload)
