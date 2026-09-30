"""Authenticated transport for persisted Claude Code conversations."""

import asyncio
import hashlib
import hmac
import json
import os
import re
import secrets
import tempfile
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Header, HTTPException, Path as ApiPath, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import Field

from trace_hunter.agent_sessions import AgentSessions
from trace_hunter.catalog import canonical
from trace_hunter.claude_session_capture import convert as convert_claude_turn, prepare as prepare_claude_turn
from trace_hunter.content import ContentCorruption, ContentRef
from .access import current_principal, require_project
from trace_hunter.access import Projects
from .traces import TraceDTO, errors


SessionId = Annotated[str, ApiPath(min_length=36, max_length=36)]


class SessionCreate(TraceDTO):
    project_id: Annotated[str, Field(min_length=1, max_length=128)]


class AgentSessionSummary(TraceDTO):
    session_id: str
    claude_session_id: str | None = None
    native_terminal: bool = False
    project_id: str
    trace_project_id: str
    created_at: str
    updated_at: str
    kind: Literal["auto_import", "agent"]
    turn_id: str | None
    task_id: str | None
    state: str | None
    run_id: str | None
    source_name: str | None


class AgentSessionList(TraceDTO):
    items: list[AgentSessionSummary]


class MessageCreate(TraceDTO):
    text: Annotated[str, Field(min_length=1, max_length=200000)]


class TraceLabelIdentity(TraceDTO):
    project_id: Annotated[str, Field(min_length=1, max_length=128)]
    run_id: Annotated[str, Field(min_length=1, max_length=512)]


class TraceLabelsRequest(TraceDTO):
    items: Annotated[list[TraceLabelIdentity], Field(min_length=1, max_length=200)]


class TraceLabel(TraceLabelIdentity):
    title: Annotated[str, Field(min_length=1, max_length=160)]


class TraceLabelsResponse(TraceDTO):
    items: list[TraceLabel]


class WorkerEvent(TraceDTO):
    type: Annotated[str, Field(min_length=1, max_length=64)]
    payload: dict


class WorkerFinish(TraceDTO):
    state: str
    claude_session_id: str | None = None
    run_id: str | None = None
    raw_ref: dict | None = None
    error: str | None = None


class WorkerTask(TraceDTO):
    task_id: str


class NativeTerminalBinding(TraceDTO):
    terminal_session_id: Annotated[str, Field(min_length=36, max_length=36)]


class TelemetryAuthorization(TraceDTO):
    token: Annotated[str, Field(min_length=32, max_length=128)]


def _owner(request):
    principal = current_principal(request)
    if principal.principal_id:
        return principal.principal_id
    guest = request.cookies.get("th_agent_guest", "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{43}", guest):
        raise HTTPException(status_code=401, detail="Open Agent capabilities to initialize this browser")
    return "guest-" + hashlib.sha256(guest.encode()).hexdigest()


def install(app, store, reply):
    sessions = (AgentSessions(os.environ.get("TRACE_HUNTER_AGENT_STATE_DIR") or
                              store.content.root / "agent-state") if store is not None else None)
    if sessions is not None:
        app.state.agent_sessions = sessions
    enabled = os.environ.get("TRACE_HUNTER_AGENT_ENABLED", "").lower() in ("1", "true", "yes")
    root = "/api/v1/agent/sessions"

    def available():
        if not enabled:
            raise HTTPException(status_code=503, detail="Agent runtime is not enabled")

    def readable_session(session_id, request):
        session = sessions.internal_get(session_id)
        try:
            owner = _owner(request)
        except HTTPException:
            owner = None
        if session["owner_id"] == owner:
            return session
        require_project(request, session["project_id"], "traces:read")
        for turn in session["turns"]:
            task_id = turn.get("task_id")
            if not task_id:
                continue
            try:
                task = app.state.tasks.get(session["project_id"], task_id)
            except KeyError:
                continue
            source = task.get("source") or {}
            if (source.get("type") in ("agent", "auto_import") and
                    source.get("agent_session_id", session_id) == session_id):
                return session
        raise KeyError("Agent session not found")

    def worker(request):
        expected = os.environ.get("TRACE_HUNTER_AGENT_WORKER_TOKEN", "")
        supplied = request.headers.get("x-agent-worker-token", "")
        if not expected or not hmac.compare_digest(supplied, expected):
            raise HTTPException(status_code=403, detail="Agent worker authentication required")

    @app.get("/api/v1/agent/capabilities", operation_id="getAgentCapabilities", tags=["Agent"])
    def capabilities(request: Request):
        current_principal(request)
        response = reply({"enabled": enabled, "version": "agent-session/1",
                          "stream": "sse", "trace_capture": "per_turn",
                          "identity": "browser_guest", "private_project_isolation": False})
        if enabled:
            guest = request.cookies.get("th_agent_guest", "")
            response.set_cookie("th_agent_guest", guest if re.fullmatch(r"[A-Za-z0-9_-]{43}", guest)
                                else secrets.token_urlsafe(32), httponly=True,
                                secure=False, samesite="strict", path="/api/v1")
        return response

    @app.post(root, status_code=201, operation_id="createAgentSession", tags=["Agent"], responses=errors(422, 500))
    def create(body: SessionCreate, request: Request):
        available()
        require_project(request, body.project_id, "traces:read")
        if not store.repository.rows("SELECT project_id FROM projects WHERE project_id=:project",
                                     {"project": body.project_id}):
            raise KeyError("Project not found")
        owner = _owner(request)
        private_project = "agent-" + hashlib.sha256(owner.encode()).hexdigest()[:20]
        Projects(store.repository).create_project(private_project, "Agent Trace · " + owner[:32])
        return reply(sessions.create(owner, body.project_id, private_project), 201)

    @app.get(root, operation_id="listAgentSessions", tags=["Agent"], response_model=AgentSessionList)
    def list_sessions(request: Request):
        available()
        return reply(AgentSessionList(items=sessions.list(_owner(request))).model_dump())

    @app.post("/api/v1/agent/trace-labels", response_model=TraceLabelsResponse,
              operation_id="getAgentTraceLabels", tags=["Agent"], responses=errors(401, 403, 422, 500))
    def trace_labels(body: TraceLabelsRequest, request: Request):
        if sessions is None:
            raise HTTPException(status_code=503, detail="Agent trace labels are unavailable")
        for project_id in {item.project_id for item in body.items}:
            require_project(request, project_id, "traces:read")
        identities = [(item.project_id, item.run_id) for item in body.items]
        return reply({"items": sessions.trace_labels(identities)})

    @app.get(root + "/{session_id}", operation_id="getAgentSession", tags=["Agent"])
    def get_session(session_id: SessionId, request: Request):
        available()
        session = readable_session(session_id, request)
        return reply({**session, "attachments": sessions.attachments(session_id, session["owner_id"])})

    @app.post(root + "/{session_id}/messages", status_code=202, operation_id="sendAgentMessage", tags=["Agent"])
    def send(session_id: SessionId, body: MessageCreate, request: Request):
        available()
        session = sessions.get(session_id, _owner(request))
        require_project(request, session["project_id"], "traces:read")
        return reply(sessions.enqueue(session_id, _owner(request), body.text), 202)

    @app.post(root + "/{session_id}/cancel", operation_id="cancelAgentTurn", tags=["Agent"])
    def cancel(session_id: SessionId, request: Request):
        available()
        return reply({"turn_id": sessions.cancel(session_id, _owner(request))})

    @app.put(root + "/{session_id}/attachments/{filename}", operation_id="uploadAgentAttachment", tags=["Agent"])
    async def attachment(session_id: SessionId, filename: Annotated[str, ApiPath(min_length=1, max_length=255)],
                         request: Request):
        available()
        session = sessions.get(session_id, _owner(request))
        require_project(request, session["project_id"], "traces:write")
        name = Path(filename).name
        if name != filename or name in (".", ".."):
            raise ValueError("Invalid attachment filename")
        descriptor, temporary = tempfile.mkstemp(dir=store.content.root, prefix=".agent-attachment-")
        size = 0
        try:
            with os.fdopen(descriptor, "wb") as output:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > store.content.max_object_bytes:
                        raise ValueError("Agent attachment exceeds content limit")
                    output.write(chunk)
            with open(temporary, "rb") as source:
                ref = store.content.put(source, media_type="application/octet-stream")
            item = sessions.add_attachment(session_id, _owner(request), name, ref.as_dict())
            return reply(item, 201)
        finally:
            os.unlink(temporary)

    @app.get(root + "/{session_id}/attachments/{attachment_id}/content", operation_id="readAgentAttachment", tags=["Agent"])
    def attachment_content(session_id: SessionId, attachment_id: str, request: Request):
        available()
        worker(request)
        owner = sessions.internal_get(session_id)["owner_id"]
        item = next((entry for entry in sessions.attachments(session_id, owner)
                     if entry["attachment_id"] == attachment_id), None)
        if item is None:
            raise KeyError("Agent attachment not found")
        ref = ContentRef(**item["content_ref"])

        def stream():
            with store.content.open_verified(ref) as source:
                while chunk := source.read(64 * 1024):
                    yield chunk

        return StreamingResponse(stream(), media_type="application/octet-stream",
                                 headers={"Cache-Control": "no-store"})

    @app.post(root + "/{session_id}/turns/{turn_id}/complete", operation_id="archiveAgentTurn", tags=["Agent"])
    async def complete(session_id: SessionId, turn_id: str, request: Request):
        available()
        worker(request)
        owner = sessions.internal_get(session_id)["owner_id"]
        session = sessions.get(session_id, owner)
        if not any(turn["turn_id"] == turn_id for turn in session["turns"]):
            raise KeyError("Agent turn not found")
        try:
            sealed = store.revisions.get(session["trace_project_id"], "agent-" + turn_id)
        except KeyError:
            sealed = None
        if sealed is not None:
            # A worker may crash after append but before receiving the response.
            # The first sealed turn is authoritative; recovery never creates v2.
            existing = json.loads(store.revisions.read(session["trace_project_id"],
                                                       "agent-" + turn_id, sealed["revision"]))
            agent = (existing.get("extensions") or {}).get("trace_hunter.agent") or {}
            if agent.get("turn_id") != turn_id or agent.get("session_id") != session_id:
                raise ValueError("Existing Agent Trace identity mismatch")
            projection = app.state.trace_index.status(session["trace_project_id"],
                                                      sealed["run_id"], sealed["revision"])
            if projection["state"] != "complete":
                projection = app.state.trace_index.project(session["trace_project_id"],
                                                           sealed["run_id"], sealed["revision"])
            if projection["state"] != "complete":
                raise RuntimeError("Agent Trace projection failed")
            return reply({"project_id": session["trace_project_id"], "run_id": sealed["run_id"],
                          "revision": sealed["revision"], "raw_ref": agent["raw_stream_ref"]})
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > store.content.max_object_bytes:
                raise ValueError("Agent capture exceeds content limit")
        capture = json.loads(raw)
        for source in capture.get("recorder_sources") or []:
            if not isinstance(source, dict) or not re.fullmatch(r"body-[a-f0-9]{32}", source.get("id", "")):
                raise ValueError("Invalid recorder source identity")
            registered = sessions.source(turn_id, source["id"])
            if registered is None or registered != source.get("content_ref"):
                raise ValueError("Recorder source is not registered for this turn")
            with store.content.open_verified(ContentRef(**registered)):
                pass
        prepared = prepare_claude_turn(bytes(raw))
        raw_ref = store.content.put_bytes(prepared, media_type="application/json")
        document = convert_claude_turn(prepared, raw_ref)
        if document["run"]["id"] != "agent-" + turn_id or document["segments"][0]["session"]["id"] != session_id:
            raise ValueError("Agent capture identity mismatch")
        payload, _ = canonical(document)
        result = store.revisions.append(session["trace_project_id"], payload.encode(),
                                        request_key="agent-turn:" + turn_id, derivation="capture")
        descriptor = result["revision"]
        projection = app.state.trace_index.project(session["trace_project_id"],
                                                   descriptor["run_id"], descriptor["revision"])
        if projection["state"] != "complete":
            raise RuntimeError("Agent Trace projection failed")
        return reply({"project_id": session["trace_project_id"], "run_id": descriptor["run_id"],
                      "revision": descriptor["revision"], "raw_ref": raw_ref.as_dict()})

    @app.get("/api/v1/projects/{project_id}/traces/{run_id}/revisions/{revision}/sources/{source_id}/content",
             operation_id="getAgentTraceSourceContent", tags=["Agent"],
             responses={200: {"description": "Verified Recorder source JSON; no-store",
                              "content": {"application/json": {"schema": {}}}},
                        404: {"description": "Source absent or outside this Trace"}})
    def source_content(project_id: str, run_id: str, revision: int, source_id: str, request: Request):
        require_project(request, project_id, "traces:read")
        if not run_id.startswith("agent-") or not re.fullmatch(r"body-[a-f0-9]{32}", source_id):
            raise KeyError("Recorder source not found")
        turn = sessions.internal_turn(run_id.removeprefix("agent-"))
        if turn["trace_project_id"] != project_id or turn["run_id"] != run_id:
            raise KeyError("Recorder source not found")
        descriptor = store.revisions.get(project_id, run_id, revision)
        if descriptor["revision"] != revision:
            raise KeyError("Recorder source not found")
        ref = sessions.source(turn["turn_id"], source_id)
        if ref is None:
            raise KeyError("Recorder source not found")
        # A source registered after sealing must not become retroactively visible.
        document = json.loads(store.revisions.read(project_id, run_id, revision))
        expected = next((item for item in document.get("sources", [])
                         if item.get("id") == source_id), None)
        if expected is None or expected.get("sha256") != ref["digest"].removeprefix("sha256:"):
            raise KeyError("Recorder source not found")

        def stream():
            try:
                with store.content.open_verified(ContentRef(**ref)) as source:
                    while chunk := source.read(64 * 1024):
                        yield chunk
            except (ContentCorruption, OSError):
                raise HTTPException(500, "Recorder source unavailable") from None

        return StreamingResponse(stream(), media_type="application/json",
                                 headers={"Cache-Control": "no-store"})

    @app.get(root + "/{session_id}/events", operation_id="watchAgentEvents", tags=["Agent"])
    async def events(session_id: SessionId, request: Request,
                     after: Annotated[int, Query(ge=0)] = 0,
                     last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None):
        available()
        owner = readable_session(session_id, request)["owner_id"]
        if last_event_id and last_event_id.isdigit():
            after = max(after, int(last_event_id))

        async def stream():
            sequence, idle = after, 0
            while not await request.is_disconnected():
                batch = sessions.events(session_id, owner, sequence)
                if batch:
                    for item in batch:
                        sequence = item["id"]
                        yield (f"id: {sequence}\nevent: {item['type']}\n"
                               f"data: {json.dumps(item, ensure_ascii=False, separators=(',', ':'))}\n\n")
                    idle = 0
                else:
                    idle += 1
                    if idle >= 40:
                        yield ": keep-alive\n\n"
                        idle = 0
                await asyncio.sleep(0.25)

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    @app.get(root + "/{session_id}/events/page", operation_id="listAgentEvents", tags=["Agent"])
    def event_page(session_id: SessionId, request: Request,
                   after: Annotated[int, Query(ge=0)] = 0):
        available()
        owner = readable_session(session_id, request)["owner_id"]
        return reply({"items": sessions.events(session_id, owner, after)})

    internal = "/api/v1/agent/worker"

    @app.post(internal + "/turns/{turn_id}/otel/authorize", operation_id="authorizeAgentTelemetry",
              tags=["Agent worker"])
    def authorize_telemetry(turn_id: str, body: TelemetryAuthorization, request: Request):
        available(); worker(request)
        sessions.authorize_telemetry(turn_id, body.token)
        return reply({"ok": True})

    @app.post(internal + "/turns/{turn_id}/otel/v1/{signal}", operation_id="ingestAgentTelemetry",
              tags=["Agent worker"])
    async def ingest_telemetry(turn_id: str, signal: str, request: Request):
        available()
        if signal not in ("logs", "traces"):
            raise HTTPException(404, "Unsupported OTLP signal")
        token = request.headers.get("x-trace-hunter-recorder-token", "")
        if not sessions.telemetry_authorized(turn_id, token):
            raise HTTPException(403, "Recorder telemetry authentication required")
        turn = sessions.internal_turn(turn_id)
        try:
            sealed = store.revisions.get(turn["trace_project_id"], "agent-" + turn_id)
        except KeyError:
            sealed = None
        if turn["state"] != "running" or sealed is not None:
            sessions.append(turn["session_id"], turn_id, "recorder_telemetry_late",
                            {"signal": signal, "state": turn["state"]})
            raise HTTPException(409, "Recorder telemetry arrived after turn sealing")
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 8 * 1024 * 1024:
                raise HTTPException(413, "Recorder telemetry exceeds 8 MiB")
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeError):
            raise HTTPException(400, "Recorder telemetry must be JSON") from None
        if not isinstance(value, dict):
            raise HTTPException(400, "Recorder telemetry must be an object")
        ref = store.content.put_bytes(bytes(raw), media_type="application/json")
        sessions.record_telemetry(turn_id, signal, ref.as_dict())
        return reply({})

    @app.get(internal + "/turns/{turn_id}/otel", operation_id="readAgentTelemetry",
             tags=["Agent worker"])
    def read_telemetry(turn_id: str, request: Request):
        available(); worker(request)
        records, used, truncated = [], 0, False
        for item in sessions.telemetry_records(turn_id):
            ref = ContentRef(**item["content_ref"])
            if used + ref.size_bytes > 32 * 1024 * 1024:
                truncated = True
                break
            with store.content.open_verified(ref) as source:
                value = json.load(source)
            records.append({"at": item["at"], "signal": item["signal"], "value": value,
                            "content_ref": item["content_ref"]})
            used += ref.size_bytes
        return reply({"items": records, "truncated": truncated})

    @app.post(internal + "/turns/{turn_id}/sources/{source_id}", operation_id="storeAgentRecorderSource",
              tags=["Agent worker"])
    async def store_source(turn_id: str, source_id: str, request: Request):
        available(); worker(request)
        if not re.fullmatch(r"body-[a-f0-9]{32}", source_id):
            raise ValueError("Invalid recorder source identity")
        sessions.internal_turn(turn_id)
        descriptor, temporary = tempfile.mkstemp(dir=store.content.root, prefix=".agent-source-")
        size = 0
        try:
            with os.fdopen(descriptor, "wb") as output:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > store.content.max_object_bytes:
                        raise ValueError("Recorder source exceeds content limit")
                    output.write(chunk)
            with open(temporary, "rb") as source:
                ref = store.content.put(source, media_type="application/json")
            return reply({"source_id": source_id,
                          "content_ref": sessions.record_source(turn_id, source_id, ref.as_dict())})
        finally:
            os.unlink(temporary)

    @app.post(internal + "/recover", operation_id="recoverAgentWorker", tags=["Agent worker"])
    def recover(request: Request):
        available(); worker(request)
        return reply({"turns": sessions.recover()})

    @app.post(internal + "/claim", operation_id="claimAgentTurn", tags=["Agent worker"])
    def claim(request: Request):
        available(); worker(request)
        return reply({"turn": sessions.claim()})

    @app.post(internal + "/turns/{turn_id}/events", operation_id="appendAgentWorkerEvent", tags=["Agent worker"])
    def append_event(turn_id: str, body: WorkerEvent, request: Request):
        available(); worker(request)
        turn = sessions.internal_turn(turn_id)
        return reply(sessions.append(turn["session_id"], turn_id, body.type, body.payload))

    @app.get(internal + "/turns/{turn_id}/cancel-requested", operation_id="getAgentCancellation", tags=["Agent worker"])
    def cancellation(turn_id: str, request: Request):
        available(); worker(request)
        turn = sessions.internal_turn(turn_id)
        return reply({"requested": sessions.cancel_requested(turn["session_id"], turn_id)})

    @app.post(internal + "/turns/{turn_id}/finish", operation_id="finishAgentTurn", tags=["Agent worker"])
    def finish(turn_id: str, body: WorkerFinish, request: Request):
        available(); worker(request)
        sessions.finish(turn_id, body.state, claude_session_id=body.claude_session_id,
                        run_id=body.run_id, raw_ref=body.raw_ref, error=body.error)
        return reply({"ok": True})

    @app.post(internal + "/turns/{turn_id}/task", operation_id="attachAgentTask", tags=["Agent worker"])
    def attach_task(turn_id: str, body: WorkerTask, request: Request):
        available(); worker(request)
        sessions.internal_turn(turn_id)
        sessions.attach_task(turn_id, body.task_id)
        return reply({"ok": True})

    @app.post(internal + "/turns/{turn_id}/native-terminal",
              operation_id="attachAgentNativeTerminal", tags=["Agent worker"])
    def attach_native_terminal(turn_id: str, body: NativeTerminalBinding, request: Request):
        available(); worker(request)
        turn = sessions.internal_turn(turn_id)
        if turn.get("task_id"):
            task = app.state.tasks.get(turn["project_id"], turn["task_id"])
            if task["source"].get("type") not in ("auto_import", "agent"):
                raise ValueError("Native terminal binding requires an Agent task")
        sessions.attach_native_terminal(turn_id, body.terminal_session_id)
        return reply({"ok": True})

    @app.post(internal + "/turns/{turn_id}/adapter-artifact",
              operation_id="archiveAgentAdapterScript", tags=["Agent worker"])
    async def archive_adapter_script(turn_id: str, request: Request,
                                     name: Annotated[str, Query(min_length=1, max_length=128)]):
        available(); worker(request)
        turn = sessions.internal_turn(turn_id)
        if not turn.get("task_id"):
            raise ValueError("Agent turn is not linked to an import task")
        task = app.state.tasks.get(turn["project_id"], turn["task_id"])
        if task["source"].get("type") != "auto_import" or task["state"] not in ("queued", "running"):
            raise ValueError("Adapter script archive requires an active automatic import")
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 256 * 1024:
                raise ValueError("Adapter script exceeds 256 KiB")
        if not raw or b"\x00" in raw:
            raise ValueError("Adapter script must be a nonempty text file")
        raw.decode("utf-8")
        ref = store.content.put_bytes(bytes(raw), media_type="text/x-python")
        return reply({"name": name, "content_ref": ref.as_dict()})

    @app.get(internal + "/sessions/{session_id}/attachments", operation_id="listAgentWorkerAttachments", tags=["Agent worker"])
    def worker_attachments(session_id: SessionId, request: Request):
        available(); worker(request)
        owner = sessions.internal_get(session_id)["owner_id"]
        return reply({"items": sessions.attachments(session_id, owner)})
