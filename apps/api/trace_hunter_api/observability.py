"""Project storage and index health transport."""

from fastapi import Request

from .access import require_project
from .traces import ProjectPath, TraceDTO, errors


class ObservabilitySummary(TraceDTO):
    project_id: str
    projector_version: str
    database: str
    search_backend: str
    trigram_index: str
    counts: dict[str, int]
    projection: dict[str, int]
    capture_coverage: dict[str, dict[str, int]]


def install(app, observability, reply):
    @app.get("/api/v1/projects/{project_id}/observability", response_model=ObservabilitySummary,
             operation_id="getProjectObservability", tags=["Project observability"],
             responses=errors(404, 422, 500))
    def summary(project_id: ProjectPath, request: Request):
        require_project(request, project_id, "traces:read")
        return reply(observability.summary(project_id))
