"""Project-scoped, read-only aggregate transport."""

from typing import Annotated, Literal

from fastapi import Request
from pydantic import Field

from .access import require_project
from .query import TraceQueryFilters
from .traces import Count, ProjectPath, Timestamp, TraceDTO, TraceIndexCounts, TraceRouteError

GroupField = Literal["query_id", "env_id", "harness", "model", "status", "index_state"]


class TraceAggregateRequest(TraceDTO):
    filters: TraceQueryFilters = Field(default_factory=TraceQueryFilters)
    revisions: Literal["latest", "all"] = "latest"
    group_by: GroupField | None = None
    limit: Annotated[int, Field(strict=True, ge=1, le=50)] = 50


class AggregateRecordStatus(TraceDTO):
    ok: Count | None
    error: Count | None
    unknown: Count | None
    other: Count | None


class AggregateErrorRate(TraceDTO):
    basis: Literal["indexed_records_with_known_outcome"]
    numerator: Count | None
    denominator: Count | None
    value: Annotated[float, Field(ge=0, le=1)] | None


class TraceAggregateMetrics(TraceDTO):
    matched_revisions: Count
    index_complete: Count
    index_failed: Count
    unindexed: Count
    indexed_revision_count: Count
    counts: TraceIndexCounts
    record_status: AggregateRecordStatus
    error_rate: AggregateErrorRate
    latest_indexed_at: Timestamp | None


class TraceAggregateGroup(TraceDTO):
    value: str | None
    metrics: TraceAggregateMetrics


class TraceAggregateWatermark(TraceDTO):
    kind: Literal["coverage"]
    matched_revisions: Count
    indexed_revision_count: Count
    latest_indexed_at: Timestamp | None


class TraceAggregateResult(TraceDTO):
    version: Literal["trace-aggregates/1"]
    projector_version: str
    consistency: Literal["statement_snapshot"]
    query_digest: str
    group_by: GroupField | None
    totals: TraceAggregateMetrics
    groups: list[TraceAggregateGroup]
    group_count: Count
    remaining_group_count: Count
    truncated: bool
    watermark: TraceAggregateWatermark


def install(app, aggregates):
    @app.post("/api/v1/projects/{project_id}/traces/aggregate", response_model=TraceAggregateResult,
              operation_id="aggregateTraceRevisions", tags=["Trace queries"],
              responses={code: {"model": TraceRouteError} for code in (401, 403, 422, 500, 503)})
    def summarize(project_id: ProjectPath, body: TraceAggregateRequest, request: Request):
        require_project(request, project_id, "traces:read")
        return aggregates.summarize(project_id, body.model_dump(exclude_unset=True))
