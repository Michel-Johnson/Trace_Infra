"""Batch evidence and analysis task endpoints."""

from typing import Annotated

from fastapi import Request
from fastapi.responses import Response
from pydantic import Field

from .access import require_project
from .tasks import Task
from .traces import ProjectPath, TraceDTO, errors


class EvidenceExportRequest(TraceDTO):
    request_key: Annotated[str, Field(min_length=1, max_length=512)]
    title: Annotated[str, Field(min_length=1, max_length=256)] = "批量证据导出"
    query: dict = Field(default_factory=dict)
    fields: list[str] = Field(default_factory=list, max_length=32)
    include_documents: bool = True
    page_size: Annotated[int, Field(strict=True, ge=1, le=1000)] = 500


def install(app, service, reply):
    @app.post("/api/v1/projects/{project_id}/evidence-exports", response_model=Task, status_code=202,
              operation_id="createEvidenceExport", tags=["Batch operations"], responses=errors(409, 422, 500))
    def evidence(project_id: ProjectPath, body: EvidenceExportRequest, request: Request):
        principal = require_project(request, project_id, "traces:read")
        principal.require(project_id, "traces:search:analysis")
        return reply(service.evidence(project_id, body.model_dump(exclude_none=True)), 202)

    @app.get("/api/v1/projects/{project_id}/evidence-exports/{task_id}/content",
             operation_id="readEvidenceExport", tags=["Batch operations"], responses=errors(404, 409, 422, 500))
    def content(project_id: ProjectPath, task_id: str, request: Request):
        principal = require_project(request, project_id, "traces:read")
        principal.require(project_id, "traces:search:analysis")
        raw, ref = service.read_evidence(project_id, task_id)
        return Response(raw, media_type=ref.media_type, headers={"Cache-Control": "no-store",
                        "ETag": '"' + ref.digest + '"', "X-Content-SHA256": ref.digest.removeprefix("sha256:")})
