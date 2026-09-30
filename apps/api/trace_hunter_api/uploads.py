"""Resumable adapter upload transport."""

from typing import Annotated

from fastapi import Header, Path, Request
from fastapi.responses import Response
from pydantic import Field

from .access import require_project
from .traces import ProjectPath, TraceDTO, errors
from trace_hunter.uploads import MAX_PART_BYTES, DuplicateUpload
from trace_hunter.content import ContentRef
from .agent import _owner


Identifier = Annotated[str, Path(min_length=1, max_length=128)]


class UploadCreate(TraceDTO):
    request_key: Annotated[str, Field(min_length=1, max_length=512)]
    source_format: Annotated[str, Field(min_length=1, max_length=128)]
    size_bytes: Annotated[int, Field(strict=True, ge=1, le=268435456)]
    sha256: Annotated[str, Field(pattern="^[0-9a-f]{64}$")]
    part_size: Annotated[int, Field(strict=True, ge=1048576, le=16777216)] = 8388608
    binding: dict = Field(default_factory=dict)
    expected_previous: Annotated[int, Field(strict=True, ge=0)] = 0
    source_name: str | None = None
    batch_id: Annotated[str, Field(min_length=1, max_length=128)] | None = None
    batch_total: Annotated[int, Field(strict=True, ge=1, le=10000)] | None = None


class UploadStatus(TraceDTO):
    upload_id: str
    project_id: str
    source_format: str
    size_bytes: int
    sha256: str
    part_size: int
    total_parts: int
    state: str
    job_id: str | None
    agent_session_id: str | None = None
    native_terminal_id: str | None = None
    agent_turn_id: str | None = None
    created_at: str
    updated_at: str
    received_parts: list[int]
    missing_parts: list[int]
    received_bytes: int
    progress: float
    reused: bool = False


class AdapterArtifact(TraceDTO):
    name: str
    content_ref: dict
    task_id: str
    created_at: str


class AdapterArtifactList(TraceDTO):
    items: list[AdapterArtifact]


async def _part_bytes(request):
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > MAX_PART_BYTES:
            raise ValueError("Upload part exceeds 16 MiB")
    return bytes(raw)


def install(app, uploads, reply):
    root = "/api/v1/projects/{project_id}/imports/uploads"

    @app.exception_handler(DuplicateUpload)
    async def duplicate_upload(request: Request, error: DuplicateUpload):
        return reply({"error": str(error), "code": "UPLOAD_ALREADY_IMPORTED" if error.imported
                      else "UPLOAD_ALREADY_SUBMITTED", "details": []}, 409)

    def saved_adapters(project_id):
        cursor, items, seen = None, [], set()
        while True:
            page = uploads.auto_imports.tasks.list(project_id, kinds={"adapter_import"},
                                                   states={"succeeded"}, limit=500, after=cursor)
            for task in page["items"]:
                if task["source"].get("type") != "auto_import":
                    continue
                for artifact in task.get("artifacts", []):
                    ref = artifact.get("ref") or {}
                    if artifact.get("kind") != "adapter_script" or ref.get("digest") in seen:
                        continue
                    seen.add(ref["digest"])
                    items.append({"name": artifact["name"], "content_ref": ref,
                                  "task_id": task["task_id"], "created_at": task["created_at"]})
            cursor = page["next_cursor"]
            if not cursor:
                return items

    @app.get("/api/v1/projects/{project_id}/import-adapters", response_model=AdapterArtifactList,
             operation_id="listAgentImportAdapters", tags=["Adapter imports"])
    def list_adapters(project_id: ProjectPath, request: Request):
        require_project(request, project_id, "traces:read")
        return reply({"items": saved_adapters(project_id) if uploads.auto_imports else []})

    @app.get("/api/v1/projects/{project_id}/import-adapters/{digest}/content",
             operation_id="readAgentImportAdapter", tags=["Adapter imports"])
    def adapter_content(project_id: ProjectPath,
                        digest: Annotated[str, Path(pattern="^[a-f0-9]{64}$")], request: Request):
        require_project(request, project_id, "traces:read")
        if uploads.auto_imports is None:
            raise KeyError("Adapter script not found")
        matched = next((item for item in saved_adapters(project_id)
                        if item["content_ref"].get("digest") == "sha256:" + digest), None)
        if matched is None:
            raise KeyError("Adapter script not found")
        with uploads.content.open_verified(ContentRef(**matched["content_ref"])) as stream:
            raw = stream.read()
        return Response(raw, media_type="text/x-python")

    @app.post(root, response_model=UploadStatus, status_code=201, operation_id="createTraceUpload",
              tags=["Adapter imports"], responses=errors(409, 422, 500))
    def create(project_id: ProjectPath, body: UploadCreate, request: Request):
        require_project(request, project_id, "traces:write")
        owner_id = _owner(request) if body.source_format == "auto" else None
        return reply(uploads.create(project_id, body.model_dump(exclude_none=True), owner_id=owner_id), 201)

    @app.get(root + "/{upload_id}", response_model=UploadStatus, operation_id="getTraceUpload",
             tags=["Adapter imports"], responses=errors(404, 422, 500))
    def get(project_id: ProjectPath, upload_id: Identifier, request: Request):
        require_project(request, project_id, "traces:read")
        return reply(uploads.describe(project_id, upload_id))

    @app.put(root + "/{upload_id}/parts/{position}", response_model=UploadStatus,
             operation_id="putTraceUploadPart", tags=["Adapter imports"], responses=errors(404, 409, 413, 422, 500))
    async def part(project_id: ProjectPath, upload_id: Identifier,
                   position: Annotated[int, Path(ge=0, le=255)], request: Request,
                   digest: Annotated[str, Header(alias="X-Chunk-SHA256", pattern="^[0-9a-f]{64}$")]):
        require_project(request, project_id, "traces:write")
        return reply(uploads.put_part(project_id, upload_id, position, await _part_bytes(request), digest))

    @app.post(root + "/{upload_id}/complete", response_model=UploadStatus,
              operation_id="completeTraceUpload", tags=["Adapter imports"], responses=errors(404, 409, 422, 500))
    def complete(project_id: ProjectPath, upload_id: Identifier, request: Request):
        require_project(request, project_id, "traces:write")
        return reply(uploads.complete(project_id, upload_id))

    @app.delete(root + "/{upload_id}", operation_id="deleteTraceUpload",
                tags=["Adapter imports"], responses=errors(404, 409, 422, 500))
    def delete(project_id: ProjectPath, upload_id: Identifier, request: Request):
        require_project(request, project_id, "traces:write")
        return reply(uploads.delete(project_id, upload_id))
