"""HTTP boundary for observable native-source adapter imports."""

from typing import Annotated

from fastapi import Path, Query, Request

from trace_hunter.import_jobs import STAGES
from trace_hunter.interop.common import MAX_BYTES as MAX_ADAPTER_BYTES
from trace_hunter.interop.service import describe
from .access import current_principal, require_project
from .traces import ProjectPath, TraceDTO, errors


Identifier = Annotated[str, Path(min_length=1, max_length=512)]


class ImportStep(TraceDTO):
    id: str
    label: str
    state: str
    started_at: str | None
    completed_at: str | None
    details: dict


class ImportError(TraceDTO):
    code: str
    message: str
    issues: list[dict]


class ImportBatchItem(TraceDTO):
    job_id: str
    source_name: str | None
    state: str
    active_stage: str | None
    created_at: str
    started_at: str | None
    completed_at: str | None
    steps: list[ImportStep]
    error: ImportError | None


class ImportBatch(TraceDTO):
    batch_id: str
    project_id: str
    state: str
    total: int
    registered: int
    completed: int
    counts: dict[str, int]
    progress: float
    throughput: float
    eta_seconds: float | None
    created_at: str
    started_at: str | None
    completed_at: str | None
    items: list[ImportBatchItem]
    items_truncated: bool
    next_cursor: str | None


class AdapterCapability(TraceDTO):
    format: str
    binding_required: list[str]


class ImportCapabilities(TraceDTO):
    canonical_schema: str
    adapters: list[AdapterCapability]
    stages: list[dict[str, str]]
    max_source_bytes: int
    max_resumable_source_bytes: int
    progress_persistence: str


def install(app, jobs, reply):

    @app.get("/api/v1/adapter-import-capabilities", response_model=ImportCapabilities,
             operation_id="getAdapterImportCapabilities", tags=["Adapter imports"],
             responses=errors(422, 500))
    def capabilities(request: Request):
        current_principal(request)
        spec = describe()
        return reply({"canonical_schema": spec["canonical_schema"],
                      "adapters": spec["adapters"],
                      "stages": [{"id": stage_id, "label": label} for stage_id, label in STAGES],
                      "max_source_bytes": MAX_ADAPTER_BYTES,
                      "max_resumable_source_bytes": MAX_ADAPTER_BYTES,
                      "progress_persistence": "filesystem_snapshot"})

    @app.get("/api/v1/projects/{project_id}/import-batches/{batch_id}",
             response_model=ImportBatch, operation_id="getAdapterImportBatch",
             tags=["Adapter imports"], responses=errors(404, 422, 500))
    def get_batch(project_id: ProjectPath, batch_id: Identifier, request: Request,
                  limit: Annotated[int, Query(ge=1, le=500)] = 100,
                  after: Annotated[str | None, Query(min_length=1, max_length=4096)] = None):
        require_project(request, project_id, "traces:read")
        return reply(jobs.get_batch_page(project_id, batch_id, limit=limit, after=after))
