#!/usr/bin/env python3
"""Dedicated Claude CLI worker; the API owns conversations and canonical storage."""

import json
import fcntl
import hashlib
import os
import re
import selectors
import shutil
import signal
import secrets
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from trace_hunter.agent_recorder import StreamCapture, body_files, body_index, body_source_id, read_jsonl


def now():
    return datetime.now(timezone.utc).isoformat()


def api(client, method, path, *, payload=None, content=None, worker=False):
    headers = {"X-Agent-Worker-Token": os.environ.get("TRACE_HUNTER_AGENT_WORKER_TOKEN", "")} if worker else {}
    response = client.request(method, path, json=payload, content=content, headers=headers, timeout=120)
    response.raise_for_status()
    return response.json()


class JournalClient:
    """Worker-owned transport; the API alone opens the conversation database."""

    def __init__(self, client):
        self.client = client

    def recover(self):
        return api(self.client, "POST", "/api/v1/agent/worker/recover", worker=True)["turns"]

    def claim(self):
        return api(self.client, "POST", "/api/v1/agent/worker/claim", worker=True)["turn"]

    def append(self, session_id, turn_id, kind, payload):
        del session_id
        return api(self.client, "POST", f"/api/v1/agent/worker/turns/{turn_id}/events",
                   payload={"type": kind, "payload": payload}, worker=True)

    def cancel_requested(self, session_id, turn_id):
        del session_id
        return api(self.client, "GET", f"/api/v1/agent/worker/turns/{turn_id}/cancel-requested",
                   worker=True)["requested"]

    def finish(self, turn_id, state, **details):
        api(self.client, "POST", f"/api/v1/agent/worker/turns/{turn_id}/finish",
            payload={"state": state, **details}, worker=True)

    def attach_task(self, turn_id, task_id):
        api(self.client, "POST", f"/api/v1/agent/worker/turns/{turn_id}/task",
            payload={"task_id": task_id}, worker=True)

    def attachments(self, session_id, owner_id):
        del owner_id
        return api(self.client, "GET", f"/api/v1/agent/worker/sessions/{session_id}/attachments",
                   worker=True)["items"]


def child_environment(home, project, turn_id, telemetry_url, telemetry_token, *, session_id=None):
    allowed = ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_MODEL",
               "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
               "ANTHROPIC_DEFAULT_HAIKU_MODEL", "CLAUDE_CODE_SUBAGENT_MODEL",
               "CLAUDE_CODE_ATTRIBUTION_HEADER", "HTTP_PROXY", "HTTPS_PROXY",
               "NO_PROXY", "http_proxy", "https_proxy", "no_proxy")
    child = {key: os.environ[key] for key in allowed if key in os.environ}
    child.update(HOME=str(home), PATH=os.environ.get("PATH", "/usr/bin:/bin"),
                 BASH_ENV=str(ROOT / "scripts" / "trace_hunter_agent_bash_env.sh"),
                 TRACE_HUNTER_PROJECT=project, TRACE_HUNTER_PROFILE="agent",
                 TRACE_HUNTER_CONFIG=str(home / ".config/trace-hunter/config.json"),
                 TRACE_HUNTER_AGENT_HOOK_PATH=str(home / ("hook-" + turn_id + ".jsonl")),
                 CLAUDE_CODE_FORWARD_SUBAGENT_TEXT="1",
                 CLAUDE_CODE_ENABLE_TELEMETRY="1", CLAUDE_CODE_ENHANCED_TELEMETRY_BETA="1",
                 OTEL_LOGS_EXPORTER="otlp", OTEL_TRACES_EXPORTER="otlp",
                 OTEL_METRICS_EXPORTER="none", OTEL_EXPORTER_OTLP_PROTOCOL="http/json",
                 OTEL_EXPORTER_OTLP_LOGS_ENDPOINT=telemetry_url + "/v1/logs",
                 OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=telemetry_url + "/v1/traces",
                 OTEL_EXPORTER_OTLP_HEADERS="X-Trace-Hunter-Recorder-Token=" + telemetry_token,
                 OTEL_LOGS_EXPORT_INTERVAL="1000", OTEL_TRACES_EXPORT_INTERVAL="1000",
                 OTEL_LOG_RAW_API_BODIES="file:" + str(home / ("body-" + turn_id)))
    if session_id:
        child["TRACE_HUNTER_AGENT_SESSION_ID"] = session_id
    return child


def prepare_workspace(home, project):
    workspace = home / "workspace"
    skills_root = workspace / ".claude" / "skills"
    skills_root.mkdir(parents=True, exist_ok=True)
    for skill in (ROOT / "skills").iterdir():
        if (skill / "SKILL.md").is_file():
            shutil.copytree(skill, skills_root / skill.name, dirs_exist_ok=True)
    cli = ROOT / "scripts" / "trace_hunter_cli.py"
    for destination in (workspace / "scripts" / "trace_hunter_cli.py",
                        skills_root / "trace-hunter-cli" / "scripts" / "trace_hunter_cli.py"):
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(cli, destination)
    schema = ROOT / "contracts" / "schemas" / "trace-v2-storage-v2.schema.json"
    schema_copy = workspace / "contracts" / "schemas" / schema.name
    schema_copy.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(schema, schema_copy)
    api_url = os.environ.get("TRACE_HUNTER_AGENT_API_URL", "http://127.0.0.1:8767")
    service = skills_root / "trace-hunter-cli" / "references" / "service.json"
    service.write_text(json.dumps({"name": "agent", "url": api_url,
                                   "api_contract": api_url + "/api/openapi.json",
                                   "authentication": "none", "default_project": project}))
    config = home / ".config" / "trace-hunter" / "config.json"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(json.dumps({"version": 1, "current": "agent", "profiles": {
        "agent": {"url": api_url,
                  "project": project}}}))
    os.chmod(config, 0o600)
    # Older worker releases installed the same hooks in settings.json. The
    # plugin is now their sole owner; clear that worker-managed file on resume.
    settings = home / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text("{}\n")
    os.chmod(settings, 0o600)
    return workspace


def read_hook_capture(home, turn_id):
    path = home / ("hook-" + turn_id + ".jsonl")
    if not path.exists():
        path = home / "hook-events.jsonl"  # Recovery of pre-Recorder turns.
    return read_jsonl(path)


def read_telemetry(client, turn_id):
    try:
        result = api(client, "GET", f"/api/v1/agent/worker/turns/{turn_id}/otel", worker=True)
    except (httpx.HTTPError, ValueError, KeyError):
        return [], {"state": "partial", "reason": "telemetry_read_failed"}, []
    items = result.get("items") or []
    return ([{"at": item["at"], "signal": item["signal"], "value": item["value"]}
             for item in items],
            {"state": "partial" if result.get("truncated") else "complete" if items else "missing",
             "count": len(items), "reason": "size_limit" if result.get("truncated") else None},
            [item["content_ref"] for item in items])


def archive_body_sources(client, turn_id, directory):
    sources, issues = [], []
    for path in body_files(directory):
        source_id = body_source_id(path)
        try:
            with path.open("rb") as source:
                preview = source.read(4096).decode("utf-8", errors="replace")
                source.seek(0)
                chunks = iter(lambda: source.read(64 * 1024), b"")
                saved = api(client, "POST", f"/api/v1/agent/worker/turns/{turn_id}/sources/{source_id}",
                            content=chunks, worker=True)
            sources.append({"id": source_id, "kind": "request" if path.name.endswith(".request.json") else "response",
                            "filename": path.name, "content_ref": saved["content_ref"],
                            "search_preview": preview})
        except (httpx.HTTPError, ValueError, KeyError, OSError) as error:
            issues.append({"source_id": source_id, "reason": type(error).__name__})
    return sources, issues


def read_subagent_transcripts(home, hook_events):
    """Preserve nested-agent records referenced by SubagentStop, without trusting paths."""
    root = home.resolve()
    result, used, seen = [], 0, set()
    for hook in hook_events:
        value = hook.get("value") or {}
        if value.get("hook_event_name") != "SubagentStop" or not value.get("agent_transcript_path"):
            continue
        supplied = str(value["agent_transcript_path"])
        path = (home / supplied[2:] if supplied.startswith("~/") else Path(supplied)).resolve()
        identity = (value.get("agent_id"), str(path))
        if identity in seen:
            continue
        seen.add(identity)
        if not path.is_relative_to(root) or not path.is_file():
            result.append({"agent_id": value.get("agent_id"), "state": "missing", "records": []})
            continue
        size = path.stat().st_size
        if size > 16 * 1024 * 1024 or used + size > 32 * 1024 * 1024:
            result.append({"agent_id": value.get("agent_id"), "state": "partial", "records": []})
            continue
        used += size
        records = []
        for line in path.read_bytes().splitlines():
            try:
                records.append(json.loads(line))
            except (ValueError, UnicodeError):
                continue
        result.append({"agent_id": value.get("agent_id"), "state": "complete",
                       "records": records})
    return result


def stage_attachments(client, sessions, turn, workspace):
    folder = workspace / "attachments"
    folder.mkdir(exist_ok=True)
    for item in sessions.attachments(turn["session_id"], turn["owner_id"]):
        name = Path(item["name"]).name
        destination = folder / (item["attachment_id"] + "-" + name)
        if destination.exists():
            continue
        response = client.get(
            f"/api/v1/agent/sessions/{turn['session_id']}/attachments/{item['attachment_id']}/content",
            headers={"X-Agent-Worker-Token": os.environ["TRACE_HUNTER_AGENT_WORKER_TOKEN"]}, timeout=120)
        response.raise_for_status()
        destination.write_bytes(response.content)
    return folder


def public_event(sessions, turn, event):
    def preview(value, limit=16000):
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        return text[:limit], len(text) > limit

    typ = event.get("type")
    if typ == "stream_event":
        delta = (event.get("event") or {}).get("delta") or {}
        if delta.get("type") == "text_delta" and isinstance(delta.get("text"), str):
            text, truncated = preview(delta["text"])
            sessions.append(turn["session_id"], turn["turn_id"], "delta",
                            {"text": text, "truncated": truncated})
    elif typ == "assistant":
        message = event.get("message") or {}
        blocks = message.get("content") or []
        text = "\n".join(block.get("text", "") for block in blocks
                         if isinstance(block, dict) and block.get("type") == "text")
        tools = [{"id": block.get("id"), "name": block.get("name")}
                 for block in blocks if isinstance(block, dict) and block.get("type") == "tool_use"]
        text, truncated = preview(text)
        sessions.append(turn["session_id"], turn["turn_id"], "message",
                        {"text": text, "tools": tools,
                         "truncated": truncated, "parent_tool_use_id": event.get("parent_tool_use_id")})
    elif typ == "user":
        message = event.get("message") or {}
        for block in message.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                content, truncated = preview(block.get("content"))
                sessions.append(turn["session_id"], turn["turn_id"], "tool_result",
                                {"tool_use_id": block.get("tool_use_id"),
                                 "content": content, "truncated": truncated,
                                 "is_error": block.get("is_error", False)})
    elif typ == "system" and event.get("subtype") == "api_retry":
        sessions.append(turn["session_id"], turn["turn_id"], "retry",
                        {"attempt": event.get("attempt"), "error": event.get("error")})


def publish_task(client, turn, state, **extra):
    if not turn.get("task_id"):
        return
    payload = {"state": state, **extra}
    try:
        api(client, "PATCH", f"/api/v1/projects/{turn['project_id']}/tasks/{turn['task_id']}",
            payload=payload, worker=bool(turn.get("auto_import")))
    except httpx.HTTPError:
        pass  # The conversation journal remains authoritative if the API restarts.


def verify_auto_import(client, turn, workspace, source):
    """Accept only a server-confirmed import of the original uploaded bytes."""
    receipt_path = workspace / "import-result.json"
    if not receipt_path.is_file() or receipt_path.stat().st_size > 8192:
        raise ValueError("Agent did not provide a bounded import-result.json")
    receipt = json.loads(receipt_path.read_text())
    if not isinstance(receipt, dict) or receipt.get("status") != "succeeded":
        raise ValueError(str(receipt.get("reason", "Agent did not complete the import"))[:500]
                         if isinstance(receipt, dict) else "Invalid Agent import receipt")
    adapter = receipt.get("adapter")
    run_id, revision = receipt.get("run_id"), receipt.get("revision")
    if (not isinstance(adapter, str) or not adapter or len(adapter) > 128 or
            not isinstance(run_id, str) or not run_id or len(run_id) > 512 or
            type(revision) is not int or revision < 1 or
            receipt.get("source_sha256") != source["source_sha256"]):
        raise ValueError("Agent import receipt lacks a valid adapter, revision or source digest")
    project = quote(turn["project_id"], safe="")
    revision_path = f"/api/v1/projects/{project}/traces/{quote(run_id, safe='')}/revisions/{revision}"
    descriptor = api(client, "GET", revision_path)
    if descriptor.get("index", {}).get("state") != "complete":
        raise ValueError("Imported Trace projection is not complete")
    document = api(client, "GET", revision_path + "/content")
    if not any(item.get("sha256") == source["source_sha256"]
               for item in document.get("sources", []) if isinstance(item, dict)):
        raise ValueError("Imported Trace does not reference the uploaded source bytes")
    result = {"adapter": adapter, "run_id": run_id, "revision": revision,
              "source_sha256": source["source_sha256"]}
    script_name = receipt.get("script_path")
    if script_name:
        if not isinstance(script_name, str) or len(script_name) > 255:
            raise ValueError("Invalid adapter script path")
        script = (workspace / script_name).resolve()
        if (not script.is_relative_to(workspace.resolve()) or not script.is_file() or
                script.suffix != ".py" or not 0 < script.stat().st_size <= 256 * 1024):
            raise ValueError("Adapter script must be a bounded Python file in this workspace")
        artifact = api(client, "POST",
                       f"/api/v1/agent/worker/turns/{turn['turn_id']}/adapter-artifact"
                       f"?name={quote(adapter, safe='')}", content=script.read_bytes(), worker=True)
        result["script_ref"] = artifact["content_ref"]
    return result


def native_import_bridge():
    url = os.environ.get("TRACE_HUNTER_IMPORT_BRIDGE_URL", "")
    token = os.environ.get("TRACE_HUNTER_IMPORT_BRIDGE_TOKEN", "")
    if not url.startswith("http://127.0.0.1:") or len(token) < 32:
        raise RuntimeError("Native import terminal bridge is not configured")
    return httpx.Client(base_url=url, headers={"X-Trace-Hunter-Import-Token": token}, timeout=120)


def bridge_file(bridge, path, destination, *, max_bytes=268435456):
    """Bounded verified local copy of a private Mulmo job artifact."""
    with bridge.stream("GET", path) as response:
        response.raise_for_status()
        used = 0
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with destination.open("wb") as output:
            for chunk in response.iter_bytes():
                used += len(chunk)
                if used > max_bytes:
                    raise ValueError("Native terminal capture exceeds limit")
                output.write(chunk)


def native_transcript_delta(bridge, session_id, offset, pending):
    path = f"/api/internal/trace-hunter/imports/{session_id}/files/transcript"
    headers = {"Range": f"bytes={offset}-"} if offset else {}
    response = bridge.get(path, headers=headers)
    if response.status_code in (404, 416):
        return offset, pending, []
    response.raise_for_status()
    chunk = response.content
    if response.status_code == 200 and offset:
        chunk = chunk[offset:] if len(chunk) >= offset else b""
    offset += len(chunk)
    lines = (pending + chunk).split(b"\n")
    events = []
    for line in lines[:-1]:
        try:
            value = json.loads(line)
        except (ValueError, UnicodeError):
            continue
        if isinstance(value, dict) and value.get("type") in ("assistant", "user", "system"):
            events.append({"at": value.get("timestamp") or now(), "value": value})
    return offset, lines[-1], events


def run_native_terminal_turn(sessions, turn, client, *, recovering=False):
    """One task turn, one real Mulmo Claude PTY; the worker only observes and archives it."""
    session_id, turn_id = turn["session_id"], turn["turn_id"]
    task = (api(client, "GET", f"/api/v1/projects/{quote(turn['project_id'], safe='')}/tasks/"
                f"{quote(turn['task_id'], safe='')}") if turn.get("task_id") else {"source": {"type": "agent"}})
    source = task.get("source") or {}
    importing = source.get("type") == "auto_import"
    if not importing and source.get("type") != "agent":
        raise ValueError("Native terminal requires an Agent or auto_import task")
    turn["auto_import"] = importing
    if not recovering:
        sessions.append(session_id, turn_id, "native_terminal_requested", {"session_id": session_id})
    home = Path(os.environ["TRACE_HUNTER_AGENT_WORK_DIR"]) / session_id
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    # The native Claude PTY gets its Skills from the existing terminal workspace.
    # This directory is only for staging attachments and reading back artifacts.
    workspace = home / "workspace"
    workspace.mkdir(parents=True, exist_ok=True, mode=0o700)
    attachments = stage_attachments(client, sessions, turn, workspace)
    started = turn.get("created_at") or now()
    bridge_path = f"/api/internal/trace-hunter/imports/{session_id}"
    result_error, cancelled = None, False
    stream_capture = StreamCapture()
    offset, pending = 0, b""
    deadline = time.monotonic() + int(os.environ.get("TRACE_HUNTER_AGENT_TURN_TIMEOUT", "1200"))
    body_dir = home / ("body-" + turn_id)
    body_dir.mkdir(exist_ok=True, mode=0o700)
    subagent_transcripts = []
    with native_import_bridge() as bridge:
        for item in sessions.attachments(session_id, turn["owner_id"]):
            name = Path(item["name"]).name
            file = attachments / (item["attachment_id"] + "-" + name)
            digest = hashlib.sha256(file.read_bytes()).hexdigest()
            if importing and digest != source["source_sha256"]:
                raise ValueError("Native import attachment differs from uploaded source")
            response = bridge.put(
                bridge_path + "/attachments/" + item["attachment_id"],
                content=file.read_bytes(),
                headers={"X-Content-SHA256": digest, "X-Trace-Name": quote(name, safe="")})
            if response.status_code != 409 or not recovering:
                response.raise_for_status()
        existing = bridge.get(bridge_path + "/status")
        if existing.status_code not in (200, 404):
            existing.raise_for_status()
        telemetry_token = secrets.token_urlsafe(32)
        if existing.status_code == 404:
            api(client, "POST", f"/api/v1/agent/worker/turns/{turn_id}/otel/authorize",
                payload={"token": telemetry_token}, worker=True)
        job = {"turnId": turn_id, "project": turn["project_id"],
               "prompt": turn["prompt"], "telemetryToken": telemetry_token}
        if turn.get("task_id"):
            job["taskId"] = turn["task_id"]
        if importing:
            job["sourceSha256"] = source["source_sha256"]
        else:
            job["kind"] = "agent"
        start = bridge.post(bridge_path + "/start", json=job)
        start.raise_for_status()
        cwd = start.json()["cwd"]
        api(client, "POST", f"/api/v1/agent/worker/turns/{turn_id}/native-terminal",
            payload={"terminal_session_id": session_id}, worker=True)
        sessions.append(session_id, turn_id, "native_terminal_ready", {"session_id": session_id,
                        "cwd": cwd, "interactive": True})
        stopped_without_receipt_at = None
        observed_progress = 0.1
        raw_path = home / ("turn-" + turn_id + ".jsonl")
        with raw_path.open("wb") as raw_output:
            while True:
                offset, pending, events = native_transcript_delta(bridge, session_id, offset, pending)
                for observed in events:
                    stream_capture.append(raw_output, observed)
                    public_event(sessions, turn, observed["value"])
                    if importing and (stage := auto_import_stage(observed["value"], observed_progress)):
                        publish_task(client, turn, "running", current_stage=stage[0], progress=stage[1],
                                     message="Agent 进入" + stage[0] + "阶段")
                        observed_progress = stage[1]
                status = bridge.get(bridge_path + "/status")
                status.raise_for_status()
                state = status.json()
                if state["stopped"] and (not importing or state["receipt"]):
                    break
                if (state["stopped"] or not state.get("live", True)) and (not state["receipt"] if importing else not state["stopped"]):
                    stopped_without_receipt_at = stopped_without_receipt_at or time.monotonic()
                    if time.monotonic() - stopped_without_receipt_at > 30:
                        result_error = ("Claude stopped without import-result.json" if importing
                                        else "Claude terminal stopped without a Stop hook")
                        break
                if sessions.cancel_requested(session_id, turn_id):
                    cancelled, result_error = True, "Cancelled"
                    bridge.post(bridge_path + "/cancel").raise_for_status()
                    break
                if time.monotonic() >= deadline:
                    result_error = "Native Claude terminal timed out"
                    bridge.post(bridge_path + "/cancel").raise_for_status()
                    break
                time.sleep(1)
            offset, pending, events = native_transcript_delta(bridge, session_id, offset, pending)
            for observed in events:
                stream_capture.append(raw_output, observed)
                public_event(sessions, turn, observed["value"])
        file_specs = [
            ("hooks", home / ("hook-" + turn_id + ".jsonl"), 32 * 1024 * 1024),
            ("body-index", body_dir / "index.jsonl", 16 * 1024 * 1024),
        ]
        if importing:
            file_specs.append(("receipt", workspace / "import-result.json", 8192))
        for kind, destination, limit in file_specs:
            try:
                bridge_file(bridge, bridge_path + "/files/" + kind, destination, max_bytes=limit)
            except httpx.HTTPStatusError as error:
                if error.response.status_code != 404:
                    raise
        body_response = bridge.get(bridge_path + "/body")
        body_response.raise_for_status()
        for name in body_response.json()["names"]:
            bridge_file(bridge, bridge_path + "/body/" + quote(name, safe=""), body_dir / name)
        index = body_dir / "index.jsonl"
        if index.exists():
            fixed = []
            for line in index.read_text().splitlines():
                try:
                    value = json.loads(line)
                except ValueError:
                    continue
                for key in ("request_file", "response_file"):
                    if isinstance(value.get(key), str):
                        value[key] = Path(value[key]).name
                fixed.append(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
            index.write_text("\n".join(fixed) + "\n")
        receipt = workspace / "import-result.json"
        if importing and receipt.exists():
            value = json.loads(receipt.read_text())
            script = value.get("script_path") if isinstance(value, dict) else None
            if isinstance(script, str) and re.fullmatch(r"[A-Za-z0-9._-]{1,255}\.py", script):
                try:
                    bridge_file(bridge, bridge_path + "/script/" + quote(script, safe=""),
                                workspace / script, max_bytes=256 * 1024)
                except httpx.HTTPStatusError as error:
                    if error.response.status_code != 404:
                        raise
        hooks, _ = read_hook_capture(home, turn_id)
        seen_subagents, subagent_bytes = set(), 0
        for hook in hooks:
            value = hook.get("value") or {}
            if value.get("hook_event_name") != "SubagentStop":
                continue
            name = Path(str(value.get("agent_transcript_path") or "")).name
            if not re.fullmatch(r"agent-[A-Za-z0-9_-]{1,128}\.jsonl", name) or name in seen_subagents:
                continue
            seen_subagents.add(name)
            response = bridge.get(bridge_path + "/subagent/" + quote(name, safe=""))
            if response.status_code == 404:
                subagent_transcripts.append({"agent_id": value.get("agent_id"), "state": "missing", "records": []})
                continue
            response.raise_for_status()
            subagent_bytes += len(response.content)
            if len(response.content) > 16 * 1024 * 1024 or subagent_bytes > 32 * 1024 * 1024:
                subagent_transcripts.append({"agent_id": value.get("agent_id"), "state": "partial", "records": []})
                continue
            records = []
            for line in response.content.splitlines():
                try:
                    records.append(json.loads(line))
                except (ValueError, UnicodeError):
                    continue
            subagent_transcripts.append({"agent_id": value.get("agent_id"), "state": "complete", "records": records})
    imported = None
    if importing and not cancelled and not result_error:
        publish_task(client, turn, "running", current_stage="readback", progress=0.9,
                     message="Verifying imported Trace")
        try:
            imported = verify_auto_import(client, turn, workspace, source)
        except (ValueError, KeyError, OSError, httpx.HTTPError) as error:
            result_error = str(error)[:500]
    state = "cancelled" if cancelled else "failed" if result_error else "succeeded"
    hook_events, hook_coverage = read_hook_capture(home, turn_id)
    indexed_bodies, body_index_coverage = body_index(body_dir)
    recorder_sources, recorder_issues = archive_body_sources(client, turn_id, body_dir)
    otlp_events, otlp_coverage, otlp_refs = read_telemetry(client, turn_id)
    raw = json.dumps({"session_id": session_id, "turn_id": turn_id, "prompt": turn["prompt"],
                      "started_at": started, "ended_at": now(), "state": state,
                      "model": os.environ.get("ANTHROPIC_MODEL"), "events": stream_capture.records,
                      "stream_coverage": stream_capture.coverage(), "hooks": hook_events,
                      "hook_coverage": hook_coverage, "subagent_transcripts": subagent_transcripts,
                      "recorder": {"version": "0.1.1", "source": "claude-code-plugin-native-tui",
                                   "telemetry_coverage": otlp_coverage, "telemetry_raw_refs": otlp_refs,
                                   "body_index_coverage": body_index_coverage, "issues": recorder_issues},
                      "otlp": otlp_events, "body_index": indexed_bodies,
                      "recorder_sources": recorder_sources},
                     ensure_ascii=False, separators=(",", ":")).encode()
    publish_task(client, turn, "running", current_stage="readback" if importing else "capture",
                 progress=0.9, message="Archiving native Claude Trace")
    captured = api(client, "POST", f"/api/v1/agent/sessions/{session_id}/turns/{turn_id}/complete",
                   content=raw, worker=True)
    sessions.append(session_id, turn_id, "trace_ready",
                    {"project_id": captured["project_id"], "run_id": captured["run_id"],
                     "revision": captured["revision"]})
    sessions.finish(turn_id, state, claude_session_id=session_id,
                    run_id=captured["run_id"], raw_ref=captured["raw_ref"], error=result_error)
    final_state = ("cancelled" if cancelled else "failed" if result_error else
                   "succeeded" if (imported or not importing) else "failed")
    result = {**(imported or {}), "agent_session_id": session_id,
              "agent_turn_id": turn_id, "agent_trace_run_id": captured["run_id"],
              "agent_trace_project_id": captured["project_id"]}
    artifacts = ([{"kind": "adapter_script", "name": imported["adapter"],
                   "ref": imported["script_ref"]}]
                 if imported and imported.get("script_ref") else [])
    publish_task(client, turn, final_state, current_stage="readback" if importing else "capture",
                 progress=1.0 if final_state == "succeeded" else None, result=result, artifacts=artifacts,
                 error={"code": "AUTO_IMPORT_FAILED" if importing else "AGENT_FAILED",
                        "message": result_error or "导入结果未通过回读校验",
                        "issues": []} if final_state == "failed" else None,
                 message="Trace 导入并回读成功" if imported else result_error or
                         ("Agent turn completed" if not importing else "导入未完成"))


AUTO_IMPORT_STAGES = {"adapter_select": 0.3, "adapter_validate": 0.5, "trace_import": 0.7}


def auto_import_stage(value, previous):
    """Use an explicit Agent progress marker, never arbitrary source text."""
    if value.get("type") != "assistant":
        return None
    blocks = (value.get("message") or {}).get("content") or []
    if not isinstance(blocks, list):
        return None
    for block in blocks:
        if not isinstance(block, dict) or block.get("type") != "text":
            continue
        for line in str(block.get("text", "")).splitlines():
            match = re.fullmatch(r"TRACE_HUNTER_IMPORT_STAGE: (adapter_select|adapter_validate|trace_import)",
                                 line.strip())
            if match and AUTO_IMPORT_STAGES[match.group(1)] > previous:
                return match.group(1), AUTO_IMPORT_STAGES[match.group(1)]
    return None


def run_turn(sessions, turn, client, *, native_terminal=True):
    session_id, turn_id = turn["session_id"], turn["turn_id"]
    home = Path(os.environ["TRACE_HUNTER_AGENT_WORK_DIR"]) / session_id
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    auto_source = None
    if turn.get("task_id"):
        task = api(client, "GET", f"/api/v1/projects/{quote(turn['project_id'], safe='')}/tasks/"
                   f"{quote(turn['task_id'], safe='')}")
        if task.get("source", {}).get("type") == "auto_import":
            auto_source = task["source"]
    turn["auto_import"] = auto_source is not None
    if auto_source or native_terminal:
        return run_native_terminal_turn(sessions, turn, client)
    workspace = prepare_workspace(home, turn["project_id"])
    attachments = stage_attachments(client, sessions, turn, workspace)
    prompt = turn["prompt"]
    if any(attachments.iterdir()):
        prompt += "\n\n本次会话已附加原始文件，存放于：" + str(attachments)
    started, stream_capture, claude_session_id = now(), StreamCapture(), turn.get("claude_session_id")
    hook_path = home / ("hook-" + turn_id + ".jsonl")
    if hook_path.exists():
        hook_path.unlink()
    body_dir = home / ("body-" + turn_id)
    body_dir.mkdir(exist_ok=True, mode=0o700)
    raw_path = home / ("turn-" + turn_id + ".jsonl")
    telemetry_token = secrets.token_urlsafe(32)
    api(client, "POST", f"/api/v1/agent/worker/turns/{turn_id}/otel/authorize",
        payload={"token": telemetry_token}, worker=True)
    telemetry_url = (os.environ.get("TRACE_HUNTER_AGENT_API_URL", "http://127.0.0.1:8767")
                     + f"/api/v1/agent/worker/turns/{turn_id}/otel")
    args = [os.environ.get("TRACE_HUNTER_CLAUDE_BIN", "claude"), "-p",
            "--output-format", "stream-json", "--verbose", "--include-partial-messages",
            "--forward-subagent-text", "--include-hook-events",
            "--append-system-prompt-file", str(ROOT / "scripts" / "trace_hunter_agent_prompt.txt"),
            "--dangerously-skip-permissions", "--strict-mcp-config",
            "--plugin-dir", str(ROOT / "plugins" / "agent-trace-recorder")]
    if claude_session_id:
        args += ["--resume", claude_session_id]
    else:
        args += ["--session-id", session_id]
    child = child_environment(home, turn["project_id"], turn_id, telemetry_url, telemetry_token,
                              session_id=turn["session_id"])
    deadline = time.monotonic() + int(os.environ.get("TRACE_HUNTER_AGENT_TURN_TIMEOUT", "1200"))
    result_error, cancelled = None, False
    observed_progress = 0.1
    with (home / "stderr.log").open("ab") as stderr, raw_path.open("ab") as raw_output:
        process = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=stderr, cwd=workspace, env=child, text=True,
                                   bufsize=1, start_new_session=True)
        process.stdin.write(prompt)
        process.stdin.close()
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        while process.poll() is None or selector.get_map():
            if sessions.cancel_requested(session_id, turn_id) or time.monotonic() >= deadline:
                cancelled = sessions.cancel_requested(session_id, turn_id)
                result_error = "Cancelled" if cancelled else "Agent turn timed out"
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                break
            if not selector.select(timeout=0.25):
                continue
            line = process.stdout.readline()
            if not line:
                selector.unregister(process.stdout)
                break
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(value, dict):
                continue
            observed = {"at": now(), "value": value}
            stream_capture.append(raw_output, observed)
            claude_session_id = value.get("session_id") or claude_session_id
            public_event(sessions, turn, value)
            if auto_source and (stage := auto_import_stage(value, observed_progress)):
                publish_task(client, turn, "running", current_stage=stage[0], progress=stage[1],
                             message="Agent 进入" + stage[0] + "阶段")
                observed_progress = stage[1]
        process.wait()
        selector.close()
        process.stdout.close()
        if process.returncode and not result_error:
            result_error = f"Claude exited with code {process.returncode}"
    state = "cancelled" if cancelled else "failed" if result_error else "succeeded"
    hook_events, hook_coverage = read_hook_capture(home, turn_id)
    subagent_transcripts = read_subagent_transcripts(home, hook_events)
    otlp_events, otlp_coverage, otlp_refs = read_telemetry(client, turn_id)
    indexed_bodies, body_index_coverage = body_index(body_dir)
    recorder_sources, recorder_issues = archive_body_sources(client, turn_id, body_dir)
    raw = json.dumps({"session_id": session_id, "turn_id": turn_id, "prompt": turn["prompt"],
                      "started_at": started, "ended_at": now(), "state": state,
                      "model": child.get("ANTHROPIC_MODEL"), "events": stream_capture.records,
                      "stream_coverage": stream_capture.coverage(),
                      "hooks": hook_events, "hook_coverage": hook_coverage,
                      "subagent_transcripts": subagent_transcripts,
                      "recorder": {"version": "0.1.1", "source": "claude-code-plugin",
                                   "telemetry_coverage": otlp_coverage,
                                   "telemetry_raw_refs": otlp_refs,
                                   "body_index_coverage": body_index_coverage,
                                   "issues": recorder_issues},
                      "otlp": otlp_events, "body_index": indexed_bodies,
                      "recorder_sources": recorder_sources},
                     ensure_ascii=False, separators=(",", ":")).encode()
    run_id, raw_ref, trace_project_id = None, None, None
    publish_task(client, turn, "running", current_stage="readback" if auto_source else "capture", progress=0.9,
                 message="Archiving Agent Trace")
    try:
        captured = api(client, "POST", f"/api/v1/agent/sessions/{session_id}/turns/{turn_id}/complete",
                       content=raw, worker=True)
        run_id, raw_ref = captured["run_id"], captured["raw_ref"]
        trace_project_id = captured["project_id"]
        sessions.append(session_id, turn_id, "trace_ready",
                        {"project_id": captured["project_id"], "run_id": run_id,
                         "revision": captured["revision"]})
    except (httpx.HTTPError, ValueError, KeyError) as error:
        result_error = (result_error + "; " if result_error else "") + "Trace capture failed: " + str(error)
        state = "failed"
    sessions.finish(turn_id, state, claude_session_id=claude_session_id,
                    run_id=run_id, raw_ref=raw_ref, error=result_error)
    if auto_source:
        imported, import_error = None, result_error
        if state == "succeeded":
            try:
                imported = verify_auto_import(client, turn, workspace, auto_source)
            except (ValueError, KeyError, OSError, httpx.HTTPError) as error:
                import_error = str(error)[:500]
        final_state = "cancelled" if state == "cancelled" else "succeeded" if imported else "failed"
        result = {**(imported or {}), "agent_session_id": session_id,
                  "agent_turn_id": turn_id, "agent_trace_run_id": run_id,
                  "agent_trace_project_id": trace_project_id}
        artifacts = ([{"kind": "adapter_script", "name": imported["adapter"],
                       "ref": imported["script_ref"]}]
                     if imported and imported.get("script_ref") else [])
        publish_task(client, turn, final_state, current_stage="readback",
                     progress=1.0 if imported else None, result=result, artifacts=artifacts,
                     error={"code": "AUTO_IMPORT_FAILED", "message": import_error or "导入结果未通过回读校验",
                            "issues": []} if final_state == "failed" else None,
                     message="Trace 导入并回读成功" if imported else import_error or "导入未完成")
    else:
        publish_task(client, turn, state, progress=1.0 if state == "succeeded" else None,
                     message=result_error or "Agent turn completed",
                     result={"run_id": run_id, "trace_project_id": trace_project_id} if run_id else {})


def recover_turn(sessions, turn, client):
    """Archive the durable stream of a process interrupted by worker restart."""
    session_id, turn_id = turn["session_id"], turn["turn_id"]
    if turn.get("task_id"):
        task = api(client, "GET", f"/api/v1/projects/{quote(turn['project_id'], safe='')}/tasks/"
                   f"{quote(turn['task_id'], safe='')}")
        turn["auto_import"] = task.get("source", {}).get("type") == "auto_import"
    if turn.get("native_requested"):
        return run_native_terminal_turn(sessions, turn, client, recovering=True)
    home = Path(os.environ["TRACE_HUNTER_AGENT_WORK_DIR"]) / session_id
    path = home / ("turn-" + turn_id + ".jsonl")
    if not path.exists():
        publish_task(client, turn, "failed", message="Agent worker interrupted before capture",
                     error={"code": "AGENT_INTERRUPTED", "message": "Agent worker interrupted", "issues": []})
        return
    events, stream_partial = [], False
    for line in path.read_bytes().splitlines():
        try:
            value = json.loads(line)
        except (ValueError, UnicodeError):
            continue
        if isinstance(value, dict) and value.get("reason") == "stream_budget":
            stream_partial = True
        elif isinstance(value, dict) and isinstance(value.get("value"), dict):
            events.append(value)
    hooks, hook_coverage = read_hook_capture(home, turn_id)
    body_dir = home / ("body-" + turn_id)
    indexed_bodies, body_index_coverage = body_index(body_dir)
    recorder_sources, recorder_issues = archive_body_sources(client, turn_id, body_dir)
    otlp_events, otlp_coverage, otlp_refs = read_telemetry(client, turn_id)
    raw = json.dumps({"session_id": session_id, "turn_id": turn_id,
                      "prompt": turn["prompt"], "started_at": turn["created_at"],
                      "ended_at": now(), "state": "failed", "events": events,
                      "stream_coverage": {"state": "partial" if stream_partial else "complete",
                                          "saved": len(events), "dropped": None},
                      "hooks": hooks, "hook_coverage": hook_coverage,
                      "subagent_transcripts": read_subagent_transcripts(home, hooks),
                      "recorder": {"version": "0.1.1", "source": "claude-code-plugin",
                                   "telemetry_coverage": otlp_coverage,
                                   "telemetry_raw_refs": otlp_refs,
                                   "body_index_coverage": body_index_coverage,
                                   "issues": recorder_issues},
                      "otlp": otlp_events, "body_index": indexed_bodies,
                      "recorder_sources": recorder_sources,
                      "interrupted": True}, ensure_ascii=False, separators=(",", ":")).encode()
    captured = api(client, "POST", f"/api/v1/agent/sessions/{session_id}/turns/{turn_id}/complete",
                   content=raw, worker=True)
    sessions.append(session_id, turn_id, "trace_ready",
                    {"project_id": captured["project_id"], "run_id": captured["run_id"],
                     "revision": captured["revision"], "partial": True})
    sessions.finish(turn_id, "failed", run_id=captured["run_id"], raw_ref=captured["raw_ref"])
    publish_task(client, turn, "failed", message="Agent worker interrupted; source remains available for retry",
                 error={"code": "AGENT_INTERRUPTED", "message": "Agent worker interrupted", "issues": []})


def run_forever():
    for name in ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_MODEL",
                 "TRACE_HUNTER_AGENT_WORKER_TOKEN", "TRACE_HUNTER_AGENT_WORK_DIR"):
        if not os.environ.get(name):
            raise RuntimeError("Agent worker requires " + name)
    for name in ("ANTHROPIC_AUTH_TOKEN", "TRACE_HUNTER_AGENT_WORKER_TOKEN"):
        if os.environ[name].startswith("REPLACE_"):
            raise RuntimeError("Agent worker requires a non-placeholder " + name)
    binary = os.environ.get("TRACE_HUNTER_CLAUDE_BIN", "claude")
    version = subprocess.run([binary, "--version"], capture_output=True, text=True,
                             check=True, timeout=15).stdout.strip()
    if not version.startswith("2.1.274 "):
        raise RuntimeError("Agent worker requires verified Claude Code 2.1.274")
    work_root = Path(os.environ["TRACE_HUNTER_AGENT_WORK_DIR"])
    work_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    worker_lock = (work_root / "worker.lock").open("a+b")
    fcntl.flock(worker_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    url = os.environ.get("TRACE_HUNTER_AGENT_API_URL", "http://127.0.0.1:8767")
    with httpx.Client(base_url=url) as client:
        sessions = JournalClient(client)
        for interrupted in sessions.recover():
            try:
                recover_turn(sessions, interrupted, client)
            except (httpx.HTTPError, ValueError, KeyError):
                pass  # The failed turn remains pending for the next worker restart.
        while True:
            turn = sessions.claim()
            if turn is None:
                time.sleep(0.5)
                continue
            try:
                if turn.get("task_id"):
                    task = api(client, "GET", f"/api/v1/projects/{quote(turn['project_id'], safe='')}/tasks/"
                               f"{quote(turn['task_id'], safe='')}")
                    if task["state"] not in ("queued", "running"):
                        raise ValueError("Agent import task is no longer active")
                else:
                    task = api(client, "POST", f"/api/v1/projects/{quote(turn['project_id'], safe='')}/tasks",
                               payload={"kind": "custom", "title": "Agent · Trace Hunter",
                                        "request_key": "agent-turn:" + turn["turn_id"],
                                        "steps": [{"id": "execute", "label": "Agent 执行"},
                                                  {"id": "capture", "label": "Trace 归档"}]})
                    sessions.attach_task(turn["turn_id"], task["task_id"])
                    turn["task_id"] = task["task_id"]
                stage = "format_inspect" if task.get("source", {}).get("type") == "auto_import" else "execute"
                turn["auto_import"] = task.get("source", {}).get("type") == "auto_import"
                publish_task(client, turn, "running", current_stage=stage, message="Agent started")
                run_turn(sessions, turn, client)
            except BaseException as error:
                sessions.finish(turn["turn_id"], "failed", error=str(error))
                publish_task(client, turn, "failed", message="Agent failed",
                             error={"code": "AGENT_FAILED", "message": str(error), "issues": []})


if __name__ == "__main__":
    run_forever()
