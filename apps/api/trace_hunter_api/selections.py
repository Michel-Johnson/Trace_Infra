"""Explicit immutable selections for replay, analysis and training inputs."""

from typing import Annotated, Literal

from fastapi import Header, HTTPException, Query, Request, Response
from pydantic import Field

from trace_hunter.content import ContentCorruption
from .access import require_project
from .query import TraceQueryFilters
from .traces import Count, Digest, ProjectPath, RunPath, Timestamp, RevisionNumber, TraceContentReference, TraceDTO, TraceRouteError


class SelectionQuery(TraceDTO):
    filters: TraceQueryFilters = Field(default_factory=TraceQueryFilters)
    revisions: Literal["latest", "all"] = "latest"


class SelectionActor(TraceDTO):
    kind: Literal["service", "operator", "unknown"]
    principal_id: str | None


class SelectionDescriptor(TraceDTO):
    kind: Literal["selection_snapshot"]
    project_id: str
    selection_id: str
    query_digest: str
    query: SelectionQuery
    member_count: Count
    manifest: TraceContentReference
    created_at: Timestamp
    consistency: Literal["fixed_revision_membership"]
    actor: SelectionActor


class SelectionAccepted(TraceDTO):
    created: bool
    selection: SelectionDescriptor


class SelectionMember(TraceDTO):
    position: Count
    run_id: str
    revision: RevisionNumber
    content_digest: Digest


class SelectionMembers(TraceDTO):
    items: list[SelectionMember]
    next_after: Count | None
    member_count: Count
    manifest_digest: Digest


class SelectionManifest(TraceDTO):
    schema_version: Literal["trace-hunter/selection-manifest/1"]
    project_id: str
    selection_id: str
    query_digest: str
    members: list[SelectionMember]


def install(app, selections):
    errors = {code: {"model": TraceRouteError} for code in (401, 403, 404, 409, 422, 500, 503)}
    root = "/api/v1/projects/{project_id}/selections"
    resource = root + "/{selection_id}"

    @app.post(root, response_model=SelectionAccepted, operation_id="freezeTraceSelection",
              tags=["Selections"], response_model_exclude_unset=True, responses={201: {"model": SelectionAccepted}, **errors})
    def freeze(project_id: ProjectPath, body: SelectionQuery, request: Request,
               idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=512)],
               response: Response):
        actor = require_project(request, project_id, "selections:write")
        result = selections.freeze(project_id, body.model_dump(exclude_unset=True), request_key=idempotency_key, actor=actor)
        response.status_code = 201 if result["created"] else 200
        return result

    @app.get(resource, response_model=SelectionDescriptor, operation_id="getTraceSelection",
             tags=["Selections"], response_model_exclude_unset=True, responses=errors)
    def get(project_id: ProjectPath, selection_id: RunPath, request: Request):
        require_project(request, project_id, "traces:read")
        return selections.get(project_id, selection_id)

    @app.get(resource + "/members", response_model=SelectionMembers, operation_id="listSelectionMembers",
             tags=["Selections"], response_model_exclude_unset=True, responses=errors)
    def members(project_id: ProjectPath, selection_id: RunPath, request: Request,
                limit: Annotated[int, Query(ge=1, le=100)] = 100,
                after: Annotated[int | None, Query(ge=0, le=9999)] = None):
        require_project(request, project_id, "traces:read")
        return selections.members(project_id, selection_id, limit=limit, after=after)

    @app.get(resource + "/manifest", response_model=SelectionManifest, operation_id="getSelectionManifest",
             tags=["Selections"], response_model_exclude_unset=True, responses=errors)
    def manifest(project_id: ProjectPath, selection_id: RunPath, request: Request):
        require_project(request, project_id, "traces:read")
        try:
            raw = selections.read_manifest(project_id, selection_id)
        except (ContentCorruption, OSError):
            raise HTTPException(500, "Selection manifest unavailable or failed integrity validation") from None
        # Bytes are the digest authority; do not reserialize through the DTO.
        return Response(raw, media_type="application/json", headers={"Cache-Control": "no-store"})
