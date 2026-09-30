"""Versioned trace HTTP boundary; raw document bytes remain immutable.

Project-scoped service principals are authenticated separately from route data.
An arbitrary project ID in a URL never grants access to that namespace.
"""

from typing import Annotated, Literal

from fastapi import Header, HTTPException, Path, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError
from starlette.concurrency import run_in_threadpool

from trace_hunter.content import ContentCorruption
from trace_hunter.http_contract import MAX_BYTES
from trace_hunter.traces.service import MAX_REVISION
from .access import require_project


FormatVersion = Literal["trace-hunter/1.0", "trace-hunter/1.1", "trace-hunter/2.0-draft.1", "trace-hunter/2.0-draft.2"]
Derivation = Literal["capture", "supplement", "correction", "legacy_import"]
IndexState = Literal["unindexed", "complete", "failed"]
CoverageState = Literal["complete", "partial", "missing", "unknown"]
RunStatus = Literal["running", "completed", "partial", "failed", "cancelled", "unknown"]
Count = Annotated[int, Field(ge=0)]
RevisionNumber = Annotated[int, Field(ge=1, le=MAX_REVISION)]
Digest = Annotated[str, Field(pattern=r"^sha256:[a-f0-9]{64}$")]
HexDigest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Timestamp = Annotated[str, Field(json_schema_extra={"format": "date-time"})]
ProjectPath = Annotated[str, Path(min_length=1, max_length=512, pattern=r"^[^\x00]+$")]
RunPath = Annotated[str, Path(min_length=1, max_length=512, pattern=r"^[^\x00]+$")]
RevisionPath = Annotated[int, Path(ge=1, le=MAX_REVISION)]


class TraceDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TraceContentReference(TraceDTO):
    digest: Digest
    size_bytes: Count
    media_type: Literal["application/json"]


class TraceSourceReference(TraceDTO):
    source_id: str
    pointer: str


class TraceLegacyEnvironment(TraceDTO):
    isolation: Literal["sandbox", "non_sandbox", "unknown"]
    network_access: Literal["allowed", "blocked", "unknown"]
    observed_at: Timestamp | None
    snapshot_id: str | None
    tool_versions: dict[str, str | None]
    notes: str


class TraceDraftEnvironment(TraceDTO):
    isolation: Literal["sandbox", "non_sandbox", "unknown"]
    network_access: Literal["allowed", "blocked", "unknown"]
    snapshot_ref: TraceSourceReference | None = None
    observed_at: Timestamp | None = None
    tool_versions: dict[str, str | None] = Field(default_factory=dict)


class TraceSourceDocument(TraceDTO):
    id: str
    revision: Annotated[int, Field(ge=1)]
    previous_document_id: str | None
    sealed: bool


class TraceRevisionMetadata(TraceDTO):
    run_id: str
    query_id: str | None
    env_id: str | None
    harness: str
    model: str | None
    status: RunStatus
    title: str | None
    environment: TraceLegacyEnvironment | TraceDraftEnvironment
    legacy_document_digest: HexDigest | None
    source_document: TraceSourceDocument | None
    format_stability: Literal["stable", "experimental"]
    format_version: FormatVersion
    source_verification: Literal["document_bytes_only"]


class TraceRevisionDescriptor(TraceDTO):
    kind: Literal["trace_revision"]
    project_id: str
    run_id: str
    revision: RevisionNumber
    content: TraceContentReference
    format_version: FormatVersion
    previous_revision: RevisionNumber | None
    derivation: Derivation
    metadata: TraceRevisionMetadata
    created_at: Timestamp


class TraceIndexedRun(TraceDTO):
    query_id: str | None
    env_id: str | None
    harness: str | None
    model: str | None
    title: str | None
    status: RunStatus | None


class TraceIndexCounts(TraceDTO):
    records: Count | None
    models: Count | None
    model_batches: Count | None
    tools: Count | None
    agents: Count | None
    waits: Count | None
    other: Count | None
    unknown: Count | None = Field(description="Records with original status unknown; overlaps kind counts.")


class TraceCaptureCoverage(TraceDTO):
    tools: CoverageState
    model_requests: CoverageState
    messages: CoverageState
    contexts: CoverageState
    timing: CoverageState


class TraceIndexCoverage(TraceDTO):
    index: IndexState
    capture: TraceCaptureCoverage
    source_verification: Literal["document_bytes_only"]


class TraceIndexStatus(TraceDTO):
    project_id: str
    run_id: str
    revision: RevisionNumber
    projector_version: str
    content_digest: Digest
    format_version: FormatVersion
    state: IndexState
    error_code: Literal[
        "content_integrity_failed", "content_missing", "content_unavailable",
        "source_invalid", "projection_failed", "index_write_failed",
    ] | None
    run: TraceIndexedRun
    identity_basis: Literal["legacy_compatibility", "source"]
    counts: TraceIndexCounts
    coverage: TraceIndexCoverage
    projection_digest: HexDigest | None
    indexed_at: Timestamp | None


class TraceRevisionView(TraceDTO):
    revision: TraceRevisionDescriptor
    index: TraceIndexStatus


class TraceRevisionAccepted(TraceRevisionView):
    """Revision acceptance is durable; index failure may describe only this attempt."""

    created: bool


class TraceRevisionHistory(TraceDTO):
    items: list[TraceRevisionDescriptor]
    next_before: RevisionNumber | None


class TraceRouteError(TraceDTO):
    error: str
    details: list[JsonValue]


def errors(*statuses):
    return {status: {"model": TraceRouteError} for status in {*statuses, 401, 403}}


async def document_bytes(request: Request) -> bytes:
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise HTTPException(415, "需要 application/json")
    # Check actual streamed bytes; Content-Length is neither required nor trusted.
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > MAX_BYTES:
            raise HTTPException(413, "轨迹文档超过 16 MiB")
        raw.extend(chunk)
    return bytes(raw)


def projection_failure(descriptor, projector_version):
    """A projection outage must not hide an already committed revision."""
    metadata = descriptor["metadata"]
    return {
        "project_id": descriptor["project_id"], "run_id": descriptor["run_id"],
        "revision": descriptor["revision"], "projector_version": projector_version,
        "content_digest": descriptor["content"]["digest"], "format_version": descriptor["format_version"],
        "state": "failed", "error_code": "projection_failed",
        "run": {key: metadata.get(key) for key in TraceIndexedRun.model_fields},
        "identity_basis": "legacy_compatibility" if descriptor["format_version"] == "trace-hunter/1.0" else "source",
        "counts": {key: None for key in TraceIndexCounts.model_fields},
        "coverage": {"index": "failed", "capture": {key: "unknown" for key in TraceCaptureCoverage.model_fields},
                     "source_verification": "document_bytes_only"},
        "projection_digest": None, "indexed_at": None,
    }


def install(app, revisions, index, reply):
    """Attach operator routes; transport models do not enter the shared core."""
    root = "/api/v1/projects/{project_id}/traces"
    version = root + "/{run_id:path}/revisions/{revision}"

    def output(model, value, status=200):
        # reply is an application Response, so validate explicitly before using it.
        try:
            payload = model.model_validate(value).model_dump(mode="json", exclude_unset=True)
        except ValidationError:
            # Internal DTO failures are not invalid client input; Pydantic's
            # detailed error can also contain source values, so do not expose it.
            raise HTTPException(500, "轨迹响应不符合内部协议") from None
        return reply(payload, status)

    def project(descriptor):
        try:
            return index.project(descriptor["project_id"], descriptor["run_id"], descriptor["revision"])
        except Exception:
            # TraceIndex persists source failures itself. Storage outages can also
            # prevent persisting that status; this response reports this attempt.
            return projection_failure(descriptor, index.projector_version)

    def append(project_id, raw, request_key, expected_previous, derivation):
        result = revisions.append(project_id, raw, request_key=request_key,
                                  expected_previous=expected_previous, derivation=derivation)
        descriptor = result["revision"]
        if result["created"]:
            status = project(descriptor)
        else:
            # A same-key retry reads the outcome; retrying a failed projection is
            # an explicit POST /index operation, not another analysis/import job.
            try:
                status = index.status(project_id, descriptor["run_id"], descriptor["revision"])
            except Exception:
                status = projection_failure(descriptor, index.projector_version)
        return {**result, "index": status}

    @app.post(root, status_code=201, response_model=TraceRevisionAccepted,
              operation_id="appendTraceRevision", tags=["Trace revisions"],
              responses={200: {"model": TraceRevisionAccepted}, **errors(409, 413, 415, 422, 500)},
              openapi_extra={"requestBody": {"required": True, "content": {"application/json": {"schema": {
                  "oneOf": [{"$ref": "#/components/schemas/TraceInput"},
                            {"$ref": "#/components/schemas/TraceV2Draft"}],
                  "description": "One raw trace document, at most 16 MiB. v2 draft remains experimental.",
              }}}}})
    async def create_revision(
        project_id: ProjectPath, request: Request,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=512)],
        expected_previous: Annotated[int, Query(ge=0, le=MAX_REVISION - 1)] = 0,
        derivation: Literal["capture", "supplement", "correction"] = "capture",
    ):
        require_project(request, project_id, "traces:write")
        raw = await document_bytes(request)
        result = await run_in_threadpool(append, project_id, raw, idempotency_key, expected_previous, derivation)
        return output(TraceRevisionAccepted, result, 201 if result["created"] else 200)

    @app.get(root + "/{run_id:path}/revisions", response_model=TraceRevisionHistory,
             operation_id="listTraceRevisions", tags=["Trace revisions"], responses=errors(404, 422, 500))
    def history(project_id: ProjectPath, run_id: RunPath, request: Request,
                before: Annotated[int | None, Query(ge=1, le=MAX_REVISION)] = None,
                limit: Annotated[int, Query(ge=1, le=100)] = 50):
        require_project(request, project_id, "traces:read")
        return output(TraceRevisionHistory, revisions.history(project_id, run_id, before=before, limit=limit))

    @app.get(version, response_model=TraceRevisionView, operation_id="getTraceRevision",
             tags=["Trace revisions"], responses=errors(404, 422, 500))
    def get_revision(project_id: ProjectPath, run_id: RunPath, revision: RevisionPath, request: Request):
        require_project(request, project_id, "traces:read")
        return output(TraceRevisionView, {
            "revision": revisions.get(project_id, run_id, revision),
            "index": index.status(project_id, run_id, revision),
        })

    @app.get(version + "/content", response_class=Response, operation_id="getTraceRevisionContent",
             tags=["Trace revisions"], responses={200: {
                 "description": "Verified original bytes; no parsing, reserialization or locator fetching.",
                 "content": {"application/json": {"schema": {"oneOf": [
                     {"$ref": "#/components/schemas/TraceInput"}, {"$ref": "#/components/schemas/TraceV2Draft"},
                 ]}}},
                 "headers": {"Cache-Control": {"schema": {"const": "no-store"}}},
             }, **errors(404, 422, 500)})
    def content(project_id: ProjectPath, run_id: RunPath, revision: RevisionPath, request: Request):
        require_project(request, project_id, "traces:read")
        try:
            raw = revisions.read(project_id, run_id, revision)
        except (ContentCorruption, OSError):
            raise HTTPException(500, "轨迹原件不可用或校验失败") from None
        return Response(content=raw, media_type="application/json", headers={"Cache-Control": "no-store"})

    @app.post(version + "/index", response_model=TraceRevisionView, operation_id="rebuildTraceIndex",
              tags=["Trace revisions"], responses=errors(404, 422, 500))
    def rebuild(project_id: ProjectPath, run_id: RunPath, revision: RevisionPath, request: Request):
        require_project(request, project_id, "traces:index")
        descriptor = revisions.get(project_id, run_id, revision)
        return output(TraceRevisionView, {"revision": descriptor, "index": project(descriptor)})
