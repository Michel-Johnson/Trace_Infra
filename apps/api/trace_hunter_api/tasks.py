"""Unified project task transport for agents, CLI and the web workbench."""

import asyncio
import hmac
import json
import os
from typing import Annotated, Literal
from uuid import UUID

from fastapi import Header, Path, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import Field

from trace_hunter.tasks import MAX_TASKS, TASK_KINDS, TASK_STATES, TERMINAL_STATES
from .access import current_principal, require_project
from .traces import ProjectPath, TraceDTO, errors


Identifier = Annotated[str, Path(min_length=1, max_length=512)]
TaskState = Literal["queued", "running", "succeeded", "failed", "cancelled"]
TaskKind = Literal["adapter_import", "evaluation", "analysis", "custom"]


class TaskStage(TraceDTO):
    id: str
    label: str
    state: str = "pending"
    started_at: str | None = None
    completed_at: str | None = None
    details: dict = Field(default_factory=dict)


class TaskStageDefinition(TraceDTO):
    id: Annotated[str, Field(min_length=1, max_length=128)]
    label: Annotated[str, Field(min_length=1, max_length=256)]


class TaskArtifact(TraceDTO):
    kind: str
    name: str
    ref: dict = Field(default_factory=dict)


class TaskEvent(TraceDTO):
    at: str
    type: str
    message: str


class TaskError(TraceDTO):
    code: str
    message: str
    issues: list[dict] = Field(default_factory=list)


class Task(TraceDTO):
    task_id: str
    project_id: str
    kind: TaskKind
    title: str
    state: TaskState
    current_stage: str | None
    progress: Annotated[float, Field(ge=0, le=1)]
    processed: Annotated[int, Field(ge=0)]
    total: Annotated[int, Field(ge=0)] | None
    created_at: str
    started_at: str | None
    updated_at: str
    completed_at: str | None
    revision: Annotated[int, Field(ge=1)]
    steps: list[TaskStage]
    events: list[TaskEvent]
    artifacts: list[TaskArtifact]
    result: dict | None
    error: TaskError | None
    source: dict


class TaskList(TraceDTO):
    items: list[Task]
    total: int
    truncated: bool
    next_cursor: str | None
    retention_limit: int


class TaskCreate(TraceDTO):
    kind: Literal["evaluation", "analysis", "custom"]
    title: Annotated[str, Field(min_length=1, max_length=256)]
    request_key: Annotated[str, Field(min_length=1, max_length=512)]
    steps: list[TaskStageDefinition] = Field(default_factory=list, max_length=50)
    agent_session_id: UUID | None = None


class TaskUpdate(TraceDTO):
    state: Literal["running", "succeeded", "failed", "cancelled"] | None = None
    current_stage: Annotated[str, Field(min_length=1, max_length=128)] | None = None
    progress: Annotated[float, Field(ge=0, le=1)] | None = None
    processed: Annotated[int, Field(ge=0)] | None = None
    total: Annotated[int, Field(ge=0)] | None = None
    message: Annotated[str, Field(min_length=1, max_length=1000)] | None = None
    result: dict | None = None
    error: TaskError | None = None
    artifacts: list[TaskArtifact] | None = Field(default=None, max_length=100)


class TaskCapabilities(TraceDTO):
    version: str
    kinds: list[str]
    states: list[str]
    transports: list[str]
    poll_interval_ms: int
    progress_persistence: str
    source_of_truth: str
    cancel_running_imports: bool
    retryable_sources: list[str]
    pagination: str
    retention_limit: int


def install(app, registry, imports, reply, batch=None):
    root = "/api/v1/projects/{project_id}/tasks"

    def with_agent_links(project_id, items):
        sessions = getattr(app.state, "agent_sessions", None)
        if sessions is None:
            return items
        unresolved = [item["task_id"] for item in items
                      if item.get("source", {}).get("type") in ("agent", "auto_import")
                      and not item["source"].get("agent_session_id")]
        links = sessions.project_task_links(project_id, unresolved)
        return [{**item, "source": {**item["source"],
                 "agent_session_id": links[item["task_id"]]["session_id"],
                 "agent_session_kind": links[item["task_id"]]["kind"]}}
                if item["task_id"] in links else item for item in items]

    @app.get("/api/v1/task-capabilities", response_model=TaskCapabilities,
             operation_id="getTaskCapabilities", tags=["Tasks"], responses=errors(422, 500))
    def capabilities(request: Request):
        current_principal(request)
        return reply({"version": "trace-hunter/tasks/1.1", "kinds": sorted(TASK_KINDS),
                      "states": sorted(TASK_STATES), "transports": ["poll", "sse"],
                      "poll_interval_ms": 1000, "progress_persistence": "filesystem_snapshot",
                      "source_of_truth": "backend", "cancel_running_imports": False,
                      "retryable_sources": ["evidence_export", "auto_import"],
                      "pagination": "descending_keyset", "retention_limit": MAX_TASKS})

    @app.post(root, status_code=202, response_model=Task, operation_id="createTask",
              tags=["Tasks"], responses=errors(409, 422, 500))
    def create(project_id: ProjectPath, body: TaskCreate, request: Request):
        require_project(request, project_id, "traces:write")
        if len({step.id for step in body.steps}) != len(body.steps):
            raise ValueError("Task step ids must be unique")
        session_kind = "interactive"
        if body.agent_session_id and getattr(app.state, "agent_sessions", None):
            try:
                agent_session = app.state.agent_sessions.internal_get(str(body.agent_session_id))
            except KeyError:
                pass
            else:
                if agent_session["project_id"] != project_id:
                    raise ValueError("Agent session belongs to another project")
                session_kind = ("native_import" if app.state.agent_sessions.is_native_terminal(
                    str(body.agent_session_id), agent_session["owner_id"]) else "background")
        task, _ = registry.create(project_id, request_key=body.request_key, kind=body.kind,
                                  title=body.title, steps=[{**step.model_dump(), "state": "pending", "started_at": None,
                                                          "completed_at": None, "details": {}} for step in body.steps],
                                  source={"type": "agent", **({"agent_session_id": str(body.agent_session_id),
                                                                "agent_session_kind": session_kind}
                                                               if body.agent_session_id else {})})
        return reply(task, 202)

    @app.get(root, response_model=TaskList, operation_id="listTasks",
             tags=["Tasks"], responses=errors(422, 500))
    def listing(project_id: ProjectPath, request: Request,
                state: Annotated[list[TaskState] | None, Query()] = None,
                kind: Annotated[list[TaskKind] | None, Query()] = None,
                limit: Annotated[int, Query(ge=1, le=500)] = 100,
                after: Annotated[str | None, Query(min_length=1, max_length=4096)] = None):
        require_project(request, project_id, "traces:read")
        value = registry.list(project_id, states=set(state or []), kinds=set(kind or []),
                              limit=limit, after=after)
        value["items"] = with_agent_links(project_id, value["items"])
        return reply(value)

    @app.get(root + "/{task_id}", response_model=Task, operation_id="getTask",
             tags=["Tasks"], responses=errors(404, 422, 500))
    def get(project_id: ProjectPath, task_id: Identifier, request: Request):
        require_project(request, project_id, "traces:read")
        return reply(with_agent_links(project_id, [registry.get(project_id, task_id)])[0])

    @app.patch(root + "/{task_id}", response_model=Task, operation_id="updateTask",
               tags=["Tasks"], responses=errors(403, 404, 409, 422, 500))
    def update(project_id: ProjectPath, task_id: Identifier, body: TaskUpdate, request: Request):
        require_project(request, project_id, "traces:write")
        task = registry.get(project_id, task_id)
        if task["source"].get("type") == "auto_import":
            expected = os.environ.get("TRACE_HUNTER_AGENT_WORKER_TOKEN", "")
            if not expected or not hmac.compare_digest(request.headers.get("x-agent-worker-token", ""), expected):
                raise PermissionError("Automatic import progress requires the Agent worker")
        value = body.model_dump(exclude_unset=True)
        if "error" in value and value["error"] is not None:
            value["error"] = value["error"]
        return reply(registry.update(project_id, task_id, **value))

    @app.post(root + "/{task_id}/cancel", response_model=Task, operation_id="cancelTask",
              tags=["Tasks"], responses=errors(404, 409, 422, 500))
    def cancel(project_id: ProjectPath, task_id: Identifier, request: Request):
        require_project(request, project_id, "traces:write")
        task = registry.get(project_id, task_id)
        if task["source"].get("type") == "adapter_import":
            imports.cancel(project_id, task["source"]["job_id"])
            return reply(registry.get(project_id, task_id))
        if task["source"].get("type") == "auto_import":
            app.state.uploads.cancel(project_id, task["source"]["upload_id"])
            return reply(registry.get(project_id, task_id))
        return reply(registry.cancel(project_id, task_id))

    @app.post(root + "/{task_id}/retry", response_model=Task, operation_id="retryTask",
              tags=["Tasks"], responses=errors(404, 409, 422, 500))
    def retry(project_id: ProjectPath, task_id: Identifier, request: Request):
        require_project(request, project_id, "traces:write")
        task = registry.get(project_id, task_id)
        if task["source"].get("type") == "auto_import":
            app.state.uploads.retry(project_id, task["source"]["upload_id"])
            return reply(registry.get(project_id, task_id))
        if task["source"].get("type") != "evidence_export":
            raise ValueError("Only evidence exports and automatic imports support task retry")
        if batch is None:
            raise ValueError("Task retry executor is unavailable")
        return reply(batch.retry(project_id, task_id))

    @app.get(root + "/{task_id}/events", operation_id="watchTask",
             tags=["Tasks"], responses=errors(404, 422, 500))
    async def events(project_id: ProjectPath, task_id: Identifier, request: Request,
                     after: Annotated[int, Query(ge=0)] = 0,
                     last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None):
        require_project(request, project_id, "traces:read")
        if last_event_id and last_event_id.isdigit():
            after = max(after, int(last_event_id))
        registry.get(project_id, task_id)

        async def stream():
            revision, idle = after, 0
            while True:
                if await request.is_disconnected():
                    return
                task = registry.get(project_id, task_id)
                if task["revision"] > revision:
                    revision = task["revision"]; idle = 0
                    yield f"id: {revision}\nevent: task\ndata: {json.dumps(task, ensure_ascii=False, separators=(',', ':'))}\n\n"
                    if task["state"] in TERMINAL_STATES:
                        return
                else:
                    idle += 1
                    if idle >= 40:
                        yield ": keep-alive\n\n"; idle = 0
                await asyncio.sleep(0.25)

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})
