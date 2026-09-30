"""Visibility-aware trigram search transport."""
from typing import Annotated, Literal

from fastapi import HTTPException, Request
from pydantic import Field
from sqlalchemy.exc import OperationalError

from trace_hunter.query import TraceSearch
from .access import current_principal, require_project
from .traces import ProjectPath, RevisionNumber, TraceDTO, TraceRouteError, TraceSourceReference

Values = Annotated[list[Annotated[str, Field(min_length=1, max_length=256)]], Field(min_length=1, max_length=50)]


class SearchFilters(TraceDTO):
    run_id: Values = Field(default_factory=list)
    object_kind: list[Literal["span", "message", "context", "tool_call"]] = Field(default_factory=list, max_length=4)
    field: list[Literal["name", "input", "output", "content", "request", "arguments"]] = Field(default_factory=list, max_length=6)
    span_id: Values = Field(default_factory=list)
    skill_name: Values = Field(default_factory=list)
    skill_action: Values = Field(default_factory=list)
    name: Values = Field(default_factory=list)
    operation: Values = Field(default_factory=list)
    status: Values = Field(default_factory=list)


class VisibleTo(TraceDTO):
    run_id: str
    revision: RevisionNumber
    model_span_id: str


class SearchRequest(TraceDTO):
    query: Annotated[str, Field(min_length=1, max_length=512)]
    mode: Literal["literal", "regex"] = "literal"
    regex_syntax: Literal["postgresql_are", "portable"] = Field(
        default="postgresql_are",
        description="PostgreSQL ARE by default; portable explicitly maps \\b/\\B word boundaries to \\y/\\Y.")
    scope: Literal["analysis", "model_context"]
    purpose: Literal["interactive", "evaluation"] = "interactive"
    visible_to: VisibleTo | None = None
    filters: SearchFilters = Field(default_factory=SearchFilters)
    revisions: Literal["latest", "all"] = "latest"
    limit: Annotated[int, Field(strict=True, ge=1, le=100)] = 50
    cursor: Annotated[str, Field(min_length=1, max_length=4096)] | None = None


class VisibilityIssue(TraceDTO):
    code: str
    object_id: str | None


class VisibilityTarget(TraceDTO):
    run_id: str
    revision: RevisionNumber
    model_span_id: str
    context_id: str | None
    status: Literal["pass", "fail", "unknown"]
    issues: list[VisibilityIssue]


class SearchMatchRange(TraceDTO):
    start: int
    end: int


class SearchItem(TraceDTO):
    run_id: str
    revision: RevisionNumber
    object_kind: Literal["span", "message", "context", "tool_call"]
    object_id: str
    span_id: str | None
    field: Literal["name", "input", "output", "content", "request", "arguments"]
    text_state: Literal["exact", "truncated"]
    score: float
    snippet: str
    snippet_start: int
    text_length: int
    snippet_truncated: bool
    match_ranges: list[SearchMatchRange]
    source_refs: list[TraceSourceReference]


class SearchResult(TraceDTO):
    items: list[SearchItem]
    next_cursor: str | None
    query_digest: str
    projector_version: str
    effective_pattern: str | None = Field(
        description="The PostgreSQL ARE pattern actually executed; null for literal mode.")
    visibility: VisibilityTarget | None
    candidate_count: int
    total_count: int
    matched_trace_count: int
    truncated: bool


class SearchCapabilities(TraceDTO):
    version: str
    modes: list[Literal["literal", "regex"]]
    scopes: list[Literal["analysis", "model_context"]]
    object_kinds: list[str]
    fields: list[str]
    structured_filters: list[str]
    max_page_size: int
    max_scan_candidates: int
    max_snippet_chars: int
    max_match_ranges: int
    projector_version: str
    backend: Literal["postgresql_pg_trgm", "sqlite_scan_fallback"]
    regex_dialect: Literal["postgresql_are"]
    regex_syntaxes: list[Literal["postgresql_are", "portable"]]
    regex_max_chars: int
    regex_examples: list[dict[str, str]]
    source_content: Literal["inline_only"]
    triggers_analysis: Literal[False]


class SearchTermInput(TraceDTO):
    term: Annotated[str, Field(min_length=2, max_length=128)]


class SearchTermItem(TraceDTO):
    term: str
    search_count: int
    average_latency_ms: float | None
    last_total_count: int | None
    active: bool
    posting_count: int
    last_searched_at: str | None
    indexed_at: str | None


class SearchTermList(TraceDTO):
    items: list[SearchTermItem]
    max_active_terms: int
    observation_scope: Literal["evaluation_literal_only"]


def install(app, search: TraceSearch, reply):
    errors = {code: {"model": TraceRouteError} for code in (401, 403, 404, 422, 500, 503)}

    @app.get("/api/v1/trace-search-capabilities", response_model=SearchCapabilities,
             operation_id="getTraceSearchCapabilities", tags=["Trace search"], responses=errors)
    def capabilities(request: Request):
        current_principal(request)
        return search.capabilities()

    @app.post("/api/v1/projects/{project_id}/search", response_model=SearchResult,
              operation_id="searchTraceContent", tags=["Trace search"], responses=errors,
              openapi_extra={"x-conditional-scopes": {
                  "analysis": ["traces:read", "traces:search:analysis"],
                  "model_context": ["traces:model_context"]}})
    def search_content(project_id: ProjectPath, body: SearchRequest, request: Request):
        if body.scope == "analysis":
            principal = require_project(request, project_id, "traces:read")
            principal.require(project_id, "traces:search:analysis")
        else:
            require_project(request, project_id, "traces:model_context")
        try:
            return reply(search.query(project_id, body.model_dump(exclude_unset=True)))
        except KeyError:
            raise HTTPException(404, "Visible model span projection not found") from None
        except OperationalError as error:
            if "statement timeout" in str(error).lower():
                raise HTTPException(503, "搜索范围过大，请增加结构化过滤条件后重试") from None
            raise

    @app.get("/api/v1/projects/{project_id}/search/terms", response_model=SearchTermList,
             operation_id="listEvaluationSearchTerms", tags=["Trace search"], responses=errors)
    def list_terms(project_id: ProjectPath, request: Request):
        require_project(request, project_id, "traces:read")
        return search.hot_terms.list(project_id)

    @app.post("/api/v1/projects/{project_id}/search/terms/promote", response_model=SearchTermItem,
              operation_id="promoteEvaluationSearchTerm", tags=["Trace search"], responses=errors)
    def promote_term(project_id: ProjectPath, body: SearchTermInput, request: Request):
        require_project(request, project_id, "traces:write")
        return search.hot_terms.promote(project_id, body.term)

    @app.post("/api/v1/projects/{project_id}/search/terms/demote", response_model=SearchTermItem,
              operation_id="demoteEvaluationSearchTerm", tags=["Trace search"], responses=errors)
    def demote_term(project_id: ProjectPath, body: SearchTermInput, request: Request):
        require_project(request, project_id, "traces:write")
        return search.hot_terms.demote(project_id, body.term)
