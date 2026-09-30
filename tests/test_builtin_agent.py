"""Built-in Agent persistence, API transport and canonical Trace capture."""

import json
import os
import tempfile
import unittest
import hashlib
import sys
import threading
import time
import subprocess
import shlex
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
import httpx

from trace_hunter.agent_sessions import AgentSessions
from trace_hunter.agent_recorder import body_source_id
from trace_hunter.claude_session_capture import _attach_telemetry, convert as convert_agent_turn, prepare as prepare_agent_turn
from trace_hunter.interop.native_common import claude_stream_usage
from trace_hunter.content import ContentRef
from trace_hunter.storage import Store
from trace_hunter.traces.formats import inspect_document
from trace_hunter_api.app import create_app
from scripts.trace_hunter_agent_worker import (JournalClient, child_environment, recover_turn,
                                                run_native_terminal_turn, run_turn)


class AgentSessionStoreTests(unittest.TestCase):
    def test_cli_pipefail_hook_changes_only_trace_hunter_pipeline(self):
        script = Path(__file__).resolve().parents[1] / "plugins/agent-trace-recorder/hooks/cli_pipefail.py"
        event = {"hook_event_name": "PreToolUse", "tool_name": "Bash",
                 "tool_input": {"command": "python scripts/trace_hunter_cli.py trace query 2>&1 | head -n1",
                                "description": "synthetic"}}
        result = subprocess.run([sys.executable, str(script)], input=json.dumps(event), text=True,
                                capture_output=True, check=True)
        updated = json.loads(result.stdout)["hookSpecificOutput"]["updatedInput"]
        self.assertEqual(updated["command"], "set -o pipefail; " + event["tool_input"]["command"])
        self.assertEqual(updated["description"], "synthetic")
        event["tool_input"]["command"] = "python scripts/trace_hunter_cli.py trace query"
        result = subprocess.run([sys.executable, str(script)], input=json.dumps(event), text=True,
                                capture_output=True, check=True)
        self.assertEqual(result.stdout, "")

    def test_cli_error_envelope_marks_tool_error_despite_successful_shell_hook(self):
        at = datetime.now(timezone.utc).isoformat()
        error = json.dumps({"error": "invalid query", "status": 422, "details": []})
        def build(command, output=error + "\nEXIT_CODE=0"):
            raw = {"session_id": "session-synthetic", "turn_id": "turn-synthetic", "started_at": at,
                   "ended_at": at, "state": "succeeded", "prompt": "synthetic", "events": [
                       {"at": at, "value": {"type": "assistant", "message": {"id": "model-one", "content": [
                           {"type": "tool_use", "id": "tool-one", "name": "Bash",
                            "input": {"command": command}}]}}},
                       {"at": at, "value": {"type": "user", "message": {"content": [
                           {"type": "tool_result", "tool_use_id": "tool-one",
                            "content": output, "is_error": False}]}}}],
                   "hooks": [{"at": at, "value": {"hook_event_name": "PostToolUse",
                                                  "tool_use_id": "tool-one", "tool_name": "Bash"}}]}
            prepared = prepare_agent_turn(json.dumps(raw).encode())
            return convert_agent_turn(prepared, ContentRef(
                "sha256:" + hashlib.sha256(prepared).hexdigest(), len(prepared), "application/json"))
        result = build("python scripts/trace_hunter_cli.py trace query --limit -1 | head")
        tool = next(span for span in result["spans"] if span["kind"] == "tool")
        self.assertEqual(tool["status"], "error")
        self.assertEqual(tool["attributes"]["trace_hunter.cli_http_status"], 422)
        inspect_document(json.dumps(result).encode())
        wrapped = build("python scripts/trace_hunter_cli.py trace query --limit -1 | head",
                        "Exit code 3\n" + error)
        wrapped_tool = next(span for span in wrapped["spans"] if span["kind"] == "tool")
        self.assertEqual(wrapped_tool["attributes"]["trace_hunter.cli_http_status"], 422)
        unrelated = build("printf '{error}'")
        self.assertEqual(next(span for span in unrelated["spans"] if span["kind"] == "tool")["status"], "ok")

    def test_native_import_does_not_copy_worker_skills(self):
        class Sessions:
            def append(self, *_args):
                pass

        turn = {"session_id": "synthetic-session", "turn_id": "synthetic-turn",
                "task_id": "synthetic-task", "project_id": "synthetic-project"}
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"TRACE_HUNTER_AGENT_WORK_DIR": directory}), \
                 patch("scripts.trace_hunter_agent_worker.api", return_value={
                     "source": {"type": "auto_import"}}), \
                 patch("scripts.trace_hunter_agent_worker.stage_attachments",
                       side_effect=RuntimeError("staging reached")):
                with self.assertRaisesRegex(RuntimeError, "staging reached"):
                    run_native_terminal_turn(Sessions(), turn, object())
            workspace = Path(directory) / "synthetic-session" / "workspace"
            self.assertTrue(workspace.is_dir())
            self.assertFalse((workspace / ".claude" / "skills").exists())

    def test_agent_turn_uses_native_cli_even_before_a_task_is_created(self):
        turn = {"session_id": "11111111-1111-4111-8111-111111111111",
                "turn_id": "22222222-2222-4222-8222-222222222222", "project_id": "synthetic"}
        with tempfile.TemporaryDirectory() as directory, \
             patch.dict(os.environ, {"TRACE_HUNTER_AGENT_WORK_DIR": directory}), \
             patch("scripts.trace_hunter_agent_worker.run_native_terminal_turn", return_value="native") as native:
            self.assertEqual(run_turn(object(), turn, object()), "native")
            native.assert_called_once()
            self.assertFalse((Path(directory) / turn["session_id"] / "workspace" / ".claude").exists())

    def test_bash_pipeline_preserves_cli_parse_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            environment = child_environment(home, "synthetic", "turn-1", "http://127.0.0.1:8767", "token",
                                            session_id="11111111-1111-4111-8111-111111111111")
            self.assertEqual(environment["TRACE_HUNTER_AGENT_SESSION_ID"],
                             "11111111-1111-4111-8111-111111111111")
            cli = Path(__file__).resolve().parents[1] / "scripts/trace_hunter_cli.py"
            command = f"{shlex.quote(sys.executable)} {shlex.quote(str(cli))} trace query --page-size 20 2>&1 | head -n 1"
            result = subprocess.run(["bash", "-c", command], env=environment, text=True, capture_output=True)
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertIn("page-size", json.loads(result.stdout)["error"])

    def test_root_model_and_tool_use_share_observed_sequence(self):
        at = datetime.now(timezone.utc).isoformat()
        raw = {"session_id": "session-synthetic", "turn_id": "turn-synthetic", "started_at": at,
               "ended_at": at, "state": "succeeded", "prompt": "synthetic", "events": [
                   {"at": at, "value": {"type": "assistant", "message": {"id": "model-one",
                       "usage": {"input_tokens": 1, "output_tokens": 2,
                                 "cache_read_input_tokens": 10, "cache_creation_input_tokens": 0}, "content": [
                       {"type": "tool_use", "id": "tool-one", "name": "Bash", "input": {"command": "true"}}]}}},
                   {"at": at, "value": {"type": "user", "message": {"content": [
                       {"type": "tool_result", "tool_use_id": "tool-one", "content": "ok"}]}}},
                   {"at": at, "value": {"type": "assistant", "message": {"id": "model-two", "content": [
                       {"type": "text", "text": "done"}]}}}]}
        prepared = prepare_agent_turn(json.dumps(raw).encode())
        digest = "sha256:" + hashlib.sha256(prepared).hexdigest()
        document = convert_agent_turn(prepared, ContentRef(digest, len(prepared), "application/json"))
        inspect_document(json.dumps(document).encode())
        root = [span for span in document["spans"] if span["agent_id"] == "root"]
        self.assertEqual([span["kind"] for span in root], ["model", "tool", "model"])
        self.assertEqual([span["order"]["sequence"] for span in root], [0, 1, 2])
        self.assertEqual(root[0]["model"]["usage"]["total"]["input_tokens"], 11)

    def test_zero_stream_usage_is_unknown_and_unique_telemetry_recovers_it(self):
        self.assertIsNone(claude_stream_usage({"input_tokens": 0, "output_tokens": 0}, "/timeline/1/usage"))
        self.assertEqual(claude_stream_usage({"input_tokens": 3, "output_tokens": 1},
                                             "/timeline/1/usage")["completeness"], "complete")
        cached = claude_stream_usage({"input_tokens": 596, "output_tokens": 26,
                                      "cache_read_input_tokens": 19264,
                                      "cache_creation_input_tokens": 0}, "/timeline/1/usage")
        self.assertEqual(cached["total"]["input_tokens"], 19860)
        self.assertEqual(cached["total"]["cache_read_tokens"], 19264)
        origin = datetime.now(timezone.utc).replace(microsecond=0)
        at = str(int(origin.timestamp() * 1_000_000_000) + 100_000_000)
        def record(name, **values):
            return {"timeUnixNano": at, "attributes": [
                {"key": key, "value": {"stringValue": str(value)}}
                for key, value in {"event.name": name, **values}.items()]}
        document = {"spans": [{"kind": "model", "call": {"provider_request_id": "request-one"},
                                "source_refs": [], "model": {"usage": None}}],
                    "capture": {"coverage": {"timing": "partial"}}}
        raw = {"otlp": [{"value": {"resourceLogs": [{"scopeLogs": [{"logRecords": [
            record("api_request", input_tokens=17, output_tokens=4, cache_creation_tokens=2),
            record("api_response_body", **{"message.id": "request-one"}),
        ]}]}]}}]}
        _attach_telemetry(document, raw, origin)
        usage = document["spans"][0]["model"]["usage"]
        self.assertEqual(usage["completeness"], "complete")
        self.assertEqual(usage["total"]["input_tokens"], 19)
        self.assertEqual(usage["total"]["output_tokens"], 4)
        self.assertEqual(usage["total"]["cache_write_tokens"], 2)
        ambiguous = json.loads(json.dumps(raw))
        ambiguous["otlp"][0]["value"]["resourceLogs"][0]["scopeLogs"][0]["logRecords"].append(
            record("api_response_body", **{"message.id": "other-request"}))
        document["spans"][0]["model"]["usage"] = None
        _attach_telemetry(document, ambiguous, origin)
        self.assertIsNone(document["spans"][0]["model"]["usage"])

    def test_response_identity_pairs_with_unique_llm_span_time(self):
        origin = datetime.now(timezone.utc).replace(microsecond=0)
        base = int(origin.timestamp() * 1_000_000_000)
        document = {"spans": [{"kind": "model", "call": {"provider_request_id": "synthetic-message"},
                                "source_refs": [], "model": {"usage": None}}],
                    "capture": {"coverage": {"timing": "partial"}}}
        raw = {"otlp": [
            {"value": {"resourceLogs": [{"scopeLogs": [{"logRecords": [{
                "timeUnixNano": str(base + 100_000_000), "attributes": [
                    {"key": "event.name", "value": {"stringValue": "api_response_body"}},
                    {"key": "message.id", "value": {"stringValue": "synthetic-message"}},
                ]}]}]}]}},
            {"value": {"resourceSpans": [{"scopeSpans": [{"spans": [{
                "startTimeUnixNano": str(base + 50_000_000),
                "endTimeUnixNano": str(base + 100_200_000),
                "attributes": [{"key": "span.type", "value": {"stringValue": "llm_request"}}],
            }]}]}]}}
        ]}
        _attach_telemetry(document, raw, origin)
        self.assertAlmostEqual(document["spans"][0]["timing"]["start_ms"], 50, delta=1)
        self.assertAlmostEqual(document["spans"][0]["timing"]["end_ms"], 100.2, delta=1)
        self.assertEqual(document["capture"]["coverage"]["timing"], "complete")
        self.assertEqual(len(document["spans"][0]["source_refs"]), 2)
        ambiguous = json.loads(json.dumps(raw))
        ambiguous["otlp"][1]["value"]["resourceSpans"][0]["scopeSpans"][0]["spans"].append({
            "startTimeUnixNano": str(base + 60_000_000), "endTimeUnixNano": str(base + 105_000_000),
            "attributes": [{"key": "span.type", "value": {"stringValue": "llm_request"}}]})
        document["spans"][0].pop("timing")
        document["spans"][0]["source_refs"] = []
        document["capture"]["coverage"]["timing"] = "partial"
        _attach_telemetry(document, ambiguous, origin)
        self.assertNotIn("timing", document["spans"][0])
        self.assertEqual(document["capture"]["coverage"]["timing"], "partial")

    def test_usage_telemetry_tolerates_only_unique_bounded_timestamp_jitter(self):
        origin = datetime.now(timezone.utc).replace(microsecond=0)
        base = int(origin.timestamp() * 1_000_000_000) + 100_000_000

        def record(name, offset_ms, **values):
            return {"timeUnixNano": str(base + offset_ms * 1_000_000), "attributes": [
                {"key": key, "value": {"stringValue": str(value)}}
                for key, value in {"event.name": name, **values}.items()]}

        def attach(records):
            document = {"spans": [{"kind": "model", "call": {"provider_request_id": "message-one"},
                                    "source_refs": [], "model": {"usage": None}}],
                        "capture": {"coverage": {"timing": "partial"}}}
            raw = {"otlp": [{"value": {"resourceLogs": [{"scopeLogs": [
                {"logRecords": records}]}]}}]}
            _attach_telemetry(document, raw, origin)
            return document["spans"][0]

        usage = record("api_request", 0, input_tokens=17, output_tokens=4)
        for offset_ms in (0, 1, 5):
            with self.subTest(offset_ms=offset_ms):
                span = attach([usage, record("api_response_body", offset_ms,
                                             **{"message.id": "message-one"})])
                self.assertEqual(span["model"]["usage"]["total"]["input_tokens"], 17)
                self.assertEqual(len(span["source_refs"]), 2)

        response = record("api_response_body", 6, **{"message.id": "message-one"})
        self.assertIsNone(attach([usage, response])["model"]["usage"])

        response = record("api_response_body", 1, **{"message.id": "message-one"})
        near_usage = record("api_request", 2, input_tokens=20, output_tokens=5)
        self.assertIsNone(attach([usage, near_usage, response])["model"]["usage"])

        other_response = record("api_response_body", 2, **{"message.id": "message-two"})
        self.assertIsNone(attach([usage, response, other_response])["model"]["usage"])

        claimed_usage = record("api_request", 0, request_id="message-one",
                               input_tokens=17, output_tokens=4)
        span = attach([claimed_usage, response])
        self.assertEqual(span["model"]["usage"]["total"]["input_tokens"], 17)
        self.assertEqual(len(span["source_refs"]), 1)

        malformed_direct = record("api_request", 0, request_id="message-one",
                                  input_tokens=0, output_tokens=0)
        del malformed_direct["timeUnixNano"]
        span = attach([usage, malformed_direct, response])
        self.assertEqual(span["model"]["usage"]["total"]["input_tokens"], 17)

    def test_stream_snapshot_uses_final_usage_without_duplicate_model(self):
        at = datetime.now(timezone.utc).isoformat()
        raw = {"session_id": "synthetic-session", "turn_id": "synthetic-turn",
               "prompt": "synthetic", "started_at": at, "ended_at": at, "events": [
                   {"at": at, "value": {"type": "assistant", "uuid": "first",
                    "message": {"id": "message-1", "content": [{"type": "text", "text": "hel"}],
                                "usage": {"output_tokens": 2}}}},
                   {"at": at, "value": {"type": "assistant", "uuid": "final",
                    "message": {"id": "message-1", "content": [{"type": "text", "text": "hello"}],
                                "usage": {"output_tokens": 5}}}}]}
        prepared = json.loads(prepare_agent_turn(json.dumps(raw).encode()))
        self.assertEqual(len(prepared["timeline"]), 2)
        self.assertEqual(prepared["timeline"][1]["text"], "hello")
        self.assertEqual(prepared["timeline"][1]["usage"]["output_tokens"], 5)

    def test_recorder_plugin_hook_is_observational(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "hooks.jsonl"
            script = Path(__file__).resolve().parents[1] / "plugins/agent-trace-recorder/hooks/capture.py"
            result = subprocess.run([sys.executable, str(script)],
                                    input=json.dumps({"hook_event_name": "PostToolUseFailure",
                                                      "tool_use_id": "call-1"}), text=True,
                                    capture_output=True, check=True,
                                    env={**os.environ, "TRACE_HUNTER_AGENT_HOOK_PATH": str(destination)})
            self.assertEqual(result.stdout, "")
            saved = json.loads(destination.read_text())
            self.assertEqual(saved["value"]["tool_use_id"], "call-1")
            self.assertEqual(saved["state"], "complete")

    def test_hook_journal_records_without_changing_claude_output(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "hooks.jsonl"
            script = Path(__file__).resolve().parents[1] / "scripts" / "trace_hunter_claude_hook.py"
            result = subprocess.run([sys.executable, str(script)],
                                    input=json.dumps({"hook_event_name": "SubagentStart",
                                                      "agent_id": "child-1"}), text=True,
                                    capture_output=True, check=True,
                                    env={**os.environ, "TRACE_HUNTER_AGENT_HOOK_PATH": str(destination)})
            self.assertEqual(result.stdout, "")
            self.assertEqual(json.loads(destination.read_text())["value"]["agent_id"], "child-1")

    def test_owner_event_replay_and_restart_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            store = AgentSessions(Path(directory))
            item = store.create("alice", "shared", "agent-alice")
            with self.assertRaises(KeyError):
                store.get(item["session_id"], "bob")
            turn = store.enqueue(item["session_id"], "alice", "inspect trace")
            self.assertEqual(store.claim()["turn_id"], turn["turn_id"])
            first = store.append(item["session_id"], turn["turn_id"], "delta", {"text": "hello"})
            self.assertEqual([event["id"] for event in store.events(item["session_id"], "alice", first["id"] - 1)], [first["id"]])
            store.recover()
            self.assertEqual(store.get(item["session_id"], "alice")["turns"][0]["state"], "failed")
            self.assertEqual(store.enqueue(item["session_id"], "alice", "continue")["ordinal"], 2)

    def test_trace_labels_use_prompt_and_exact_source_project(self):
        with tempfile.TemporaryDirectory() as directory:
            store = AgentSessions(Path(directory))
            first = store.create("alice", "shared", "agent-alice")
            second = store.create("bob", "shared", "agent-bob")
            turn = store.enqueue(first["session_id"], "alice", "  Find  the\nerror  ")
            run_id = "agent-" + turn["turn_id"]
            store.finish(turn["turn_id"], "succeeded", run_id=run_id)
            other = store.enqueue(second["session_id"], "bob", "private query")
            store.finish(other["turn_id"], "succeeded", run_id="agent-" + other["turn_id"])
            self.assertEqual(store.trace_labels([
                ("agent-alice", run_id), ("agent-bob", run_id),
                ("agent-alice", "agent-" + other["turn_id"]),
            ]), [{"project_id": "agent-alice", "run_id": run_id,
                   "title": "Find the error"}])

    def test_session_catalog_is_owner_scoped_and_survives_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            store = AgentSessions(Path(directory))
            own = store.create("alice", "shared", "agent-alice")
            foreign = store.create("bob", "shared", "agent-bob")
            own_turn = store.enqueue(own["session_id"], "alice", "synthetic private prompt",
                                     task_id="task-synthetic")
            store.add_attachment(own["session_id"], "alice", "synthetic.json",
                                 {"digest": "sha256:" + "0" * 64, "size_bytes": 1,
                                  "media_type": "application/json"})
            foreign_turn = store.enqueue(foreign["session_id"], "bob", "foreign prompt")
            store.finish(foreign_turn["turn_id"], "failed", error="Worker interrupted; resume the conversation explicitly")
            catalog = AgentSessions(Path(directory)).list("alice")
            self.assertEqual(len(catalog), 1)
            self.assertEqual(catalog[0]["session_id"], own["session_id"])
            self.assertEqual(catalog[0]["kind"], "auto_import")
            self.assertEqual(catalog[0]["task_id"], "task-synthetic")
            self.assertEqual(catalog[0]["turn_id"], own_turn["turn_id"])
            self.assertEqual(catalog[0]["source_name"], "synthetic.json")
            self.assertEqual(catalog[0]["state"], "queued")
            self.assertNotIn("prompt", catalog[0])
            self.assertEqual(AgentSessions(Path(directory)).list("bob")[0]["kind"], "agent")
            store.finish(own_turn["turn_id"], "succeeded", claude_session_id=own["session_id"])
            self.assertFalse(store.is_native_terminal(own["session_id"], "alice"))
            self.assertFalse(store.list("alice")[0]["native_terminal"])
            native = store.create("alice", "shared", "agent-alice")
            native_turn = store.enqueue(native["session_id"], "alice", "native import", task_id="task-native")
            store.attach_native_terminal(native_turn["turn_id"], native["session_id"])
            store.append(native["session_id"], native_turn["turn_id"], "native_terminal_ready", {})
            self.assertTrue(store.is_native_terminal(native["session_id"], "alice"))
            self.assertFalse(store.is_native_terminal(native["session_id"], "bob"))
            self.assertTrue(store.list("alice")[0]["native_terminal"])


class BuiltinAgentApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "test.sqlite")
        self.addCleanup(self.store.close)
        self.env = patch.dict(os.environ, {
            "TRACE_HUNTER_AGENT_ENABLED": "1", "TRACE_HUNTER_AGENT_WORKER_TOKEN": "synthetic-worker-token",
            "TRACE_HUNTER_AGENT_STATE_DIR": str(Path(self.temp.name) / "agent-state"),
        })
        self.env.start(); self.addCleanup(self.env.stop)
        self.app = create_app(self.store)
        self.app.state.projects.create_project("shared", "Shared")
        self.client = self.enterContext(TestClient(self.app, client=("127.0.0.1", 47000)))
        self.assertEqual(self.client.get("/api/v1/agent/capabilities").status_code, 200)

    def test_trace_labels_are_project_bound(self):
        session = self.client.post("/api/v1/agent/sessions", json={"project_id": "shared"}).json()
        turn = self.client.post(f"/api/v1/agent/sessions/{session['session_id']}/messages",
                                json={"text": "Locate the failing span"}).json()
        run_id = "agent-" + turn["turn_id"]
        self.app.state.agent_sessions.finish(turn["turn_id"], "succeeded", run_id=run_id)
        result = self.client.post("/api/v1/agent/trace-labels", json={"items": [
            {"project_id": session["trace_project_id"], "run_id": run_id},
            {"project_id": "shared", "run_id": run_id},
        ]})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json(), {"items": [{"project_id": session["trace_project_id"],
                                                     "run_id": run_id, "title": "Locate the failing span"}]})

    def test_turn_import_and_attachment(self):
        session = self.client.post("/api/v1/agent/sessions", json={"project_id": "shared"})
        self.assertEqual(session.status_code, 201, session.text)
        item = session.json()
        sid = item["session_id"]
        self.assertTrue(item["trace_project_id"].startswith("agent-"))
        attached = self.client.put(f"/api/v1/agent/sessions/{sid}/attachments/sample.json",
                                   content=b'{"hello":"world"}')
        self.assertEqual(attached.status_code, 201, attached.text)
        content = self.client.get(f"/api/v1/agent/sessions/{sid}/attachments/"
                                  f"{attached.json()['attachment_id']}/content",
                                  headers={"X-Agent-Worker-Token": "synthetic-worker-token"})
        self.assertEqual(content.content, b'{"hello":"world"}')
        turn = self.client.post(f"/api/v1/agent/sessions/{sid}/messages",
                                json={"text": "find the tool error"})
        self.assertEqual(turn.status_code, 202, turn.text)
        tid = turn.json()["turn_id"]
        at = datetime.now(timezone.utc).isoformat()
        raw = {"session_id": sid, "turn_id": tid, "prompt": "find the tool error",
               "started_at": at, "ended_at": at, "state": "succeeded", "model": "model-test",
               "events": [
                   {"at": at, "value": {"type": "system", "subtype": "init", "model": "model-test"}},
                   {"at": at, "value": {"type": "assistant", "uuid": "assistant-1",
                    "message": {"id": "request-1", "content": [
                        {"type": "text", "text": "I will inspect it"},
                        {"type": "tool_use", "id": "tool-1", "name": "Bash", "input": {"command": "true"}}]}}},
                   {"at": at, "value": {"type": "user", "message": {"content": [
                       {"type": "tool_result", "tool_use_id": "tool-1", "content": "ok"}]}}},
               ], "hooks": [
                   {"at": at, "value": {"hook_event_name": "SubagentStart", "agent_id": "child-1",
                                          "agent_type": "Explore"}},
                   {"at": at, "value": {"hook_event_name": "SubagentStop", "agent_id": "child-1",
                                          "agent_type": "Explore"}},
               ], "hook_coverage": {"state": "complete", "count": 2},
               "subagent_transcripts": [{"agent_id": "child-1", "state": "complete", "records": [
                   {"type": "assistant", "timestamp": at, "message": {"content": [
                       {"type": "text", "text": "Checking the files"},
                       {"type": "tool_use", "id": "child-tool-1", "name": "Read",
                        "input": {"file_path": "sample.json"}}]}},
                   {"type": "user", "timestamp": at, "message": {"content": [
                       {"type": "tool_result", "tool_use_id": "child-tool-1", "content": "found"}]}},
               ]}]}
        url = f"/api/v1/agent/sessions/{sid}/turns/{tid}/complete"
        self.assertEqual(self.client.post(url, content=json.dumps(raw).encode()).status_code, 403)
        result = self.client.post(url, content=json.dumps(raw).encode(),
                                  headers={"X-Agent-Worker-Token": "synthetic-worker-token"})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()["run_id"], "agent-" + tid)
        public_content = self.client.get(
            f"/api/v1/projects/{item['trace_project_id']}/traces/agent-{tid}/revisions/1/content")
        self.assertEqual(public_content.status_code, 200, public_content.text)
        self.assertEqual(public_content.json()["run"]["status"], "completed")
        descriptor = self.store.revisions.get(item["trace_project_id"], "agent-" + tid)
        document = json.loads(self.store.revisions.read(item["trace_project_id"],
                                                       "agent-" + tid, descriptor["revision"]))
        self.assertEqual(document["run"]["status"], "completed")
        self.assertEqual(document["segments"][0]["session"]["id"], sid)
        self.assertEqual(len(document["tool_calls"]), 2)
        self.assertEqual(document["spans"][1]["kind"], "tool")
        self.assertEqual(document["spans"][2]["kind"], "agent")
        self.assertEqual(document["spans"][2]["agent_id"], "child-1")
        self.assertEqual(document["spans"][3]["kind"], "tool")
        self.assertEqual(document["spans"][3]["parent_id"], document["spans"][2]["id"])
        self.assertTrue(any(message.get("attributes", {}).get("agent_id") == "child-1"
                            for message in document["messages"]))
        archive_ref = ContentRef(**result.json()["raw_ref"])
        with self.store.content.open_verified(archive_ref) as source:
            archived = source.read()
        self.assertEqual(hashlib.sha256(archived).hexdigest(), document["sources"][0]["sha256"])
        self.assertEqual(document["sources"][0]["locator"], "content:" + archive_ref.digest)
        prepared = json.loads(archived)
        self.assertEqual(prepared["timeline"][0]["text"], "find the tool error")
        self.assertEqual(prepared["tool_calls"][0]["result"], "ok")
        for collection in ("segments", "phases", "spans", "messages", "tool_calls", "links"):
            for fact in document.get(collection, []):
                for reference in fact.get("source_refs", []):
                    self.assertEqual(reference["source_id"], "input")
                    value = prepared
                    for part in reference["pointer"].split("/")[1:] if reference["pointer"] else []:
                        part = part.replace("~1", "/").replace("~0", "~")
                        value = value[int(part)] if isinstance(value, list) else value[part]
                        self.assertIsNotNone(value)

    def test_recorder_body_source_and_actual_context_round_trip(self):
        session = self.client.post("/api/v1/agent/sessions", json={"project_id": "shared"}).json()
        sid = session["session_id"]
        tid = self.client.post(f"/api/v1/agent/sessions/{sid}/messages",
                               json={"text": "synthetic prompt"}).json()["turn_id"]
        name = "request-uuid.request.json"
        source_id = body_source_id(Path(name))
        body = json.dumps({"system": "synthetic system", "messages": [{"role": "user", "content": "synthetic prompt"}]}).encode()
        upload = f"/api/v1/agent/worker/turns/{tid}/sources/{source_id}"
        self.assertEqual(self.client.post(upload, content=body).status_code, 403)
        source = self.client.post(upload, content=body,
                                  headers={"X-Agent-Worker-Token": "synthetic-worker-token"})
        self.assertEqual(source.status_code, 200, source.text)
        response_name = "response-uuid.response.json"
        response_id = body_source_id(Path(response_name))
        response_body = json.dumps({"id": "message-1", "content": [{"type": "text", "text": "synthetic answer"}]}).encode()
        response_source = self.client.post(
            f"/api/v1/agent/worker/turns/{tid}/sources/{response_id}", content=response_body,
            headers={"X-Agent-Worker-Token": "synthetic-worker-token"})
        self.assertEqual(response_source.status_code, 200, response_source.text)
        at = datetime.now(timezone.utc).isoformat()
        raw = {"session_id": sid, "turn_id": tid, "prompt": "synthetic prompt",
               "started_at": at, "ended_at": at, "state": "succeeded", "events": [
                   {"at": at, "value": {"type": "system", "subtype": "api_retry",
                    "attempt": 2, "error": "synthetic transient failure"}},
                   {"at": at, "value": {"type": "assistant", "uuid": "assistant-uuid",
                    "message": {"id": "message-1", "content": [{"type": "text", "text": "synthetic answer"},
                    {"type": "tool_use", "id": "skill-tool", "name": "Skill",
                     "input": {"skill": "trace-deep-dive"}}]}}},
                   {"at": at, "value": {"type": "user", "message": {"content": [
                       {"type": "tool_result", "tool_use_id": "skill-tool", "is_error": True,
                        "content": "synthetic error"}]}}}],
               "hooks": [
                   {"at": at, "value": {"hook_event_name": "PreToolUse", "tool_use_id": "skill-tool",
                    "tool_name": "Skill", "tool_input": {"skill": "trace-deep-dive"}}},
                   {"at": at, "value": {"hook_event_name": "PostToolUseFailure", "tool_use_id": "skill-tool",
                    "tool_name": "Skill", "tool_input": {"skill": "trace-deep-dive"}}},
                   {"at": at, "value": {"hook_event_name": "PreCompact"}},
                   {"at": at, "value": {"hook_event_name": "PostCompact"}}],
               "recorder": {"version": "0.1.0", "issues": []},
               "recorder_sources": [{"id": source_id, "kind": "request", "filename": name,
                                     "content_ref": source.json()["content_ref"]},
                                    {"id": response_id, "kind": "response", "filename": response_name,
                                     "content_ref": response_source.json()["content_ref"]}],
               "body_index": [{"message_id": "message-1", "request_source_id": source_id,
                               "response_source_id": response_id, "request_id": "api-request-1"}],
               "otlp": [{"at": at, "signal": "logs", "value": {"resourceLogs": [{
                   "scopeLogs": [{"logRecords": [{
                       "timeUnixNano": str(int(datetime.fromisoformat(at).timestamp() * 1_000_000_000) + 100_000_000),
                       "attributes": [
                           {"key": "event.name", "value": {"stringValue": "api_request"}},
                           {"key": "request_id", "value": {"stringValue": "api-request-1"}},
                           {"key": "duration_ms", "value": {"doubleValue": 50.0}},
                       ]}]}]}]}}]}
        complete = self.client.post(f"/api/v1/agent/sessions/{sid}/turns/{tid}/complete",
                                    content=json.dumps(raw).encode(),
                                    headers={"X-Agent-Worker-Token": "synthetic-worker-token"})
        self.assertEqual(complete.status_code, 200, complete.text)
        retry = self.client.post(f"/api/v1/agent/sessions/{sid}/turns/{tid}/complete",
                                 content=b'{"interrupted":true}',
                                 headers={"X-Agent-Worker-Token": "synthetic-worker-token"})
        self.assertEqual(retry.status_code, 200, retry.text)
        self.assertEqual(retry.json()["revision"], 1)
        self.app.state.agent_sessions.finish(tid, "succeeded", run_id="agent-" + tid)
        document = json.loads(self.store.revisions.read(session["trace_project_id"], "agent-" + tid, 1))
        with self.store.content.open_verified(ContentRef(**document["extensions"]["trace_hunter.agent"]["raw_stream_ref"])) as source:
            archived = json.load(source)
        self.assertTrue(any(item["value"].get("subtype") == "api_retry" for item in archived["events"]))
        model = next(span for span in document["spans"] if span["kind"] == "model")
        context = next(item for item in document["contexts"] if item["id"] == model["model"]["context_id"])
        self.assertEqual(context["request"]["ref"]["source_id"], source_id)
        self.assertEqual(document["capture"]["coverage"]["contexts"], "complete")
        self.assertIn({"source_id": response_id, "pointer": ""}, model["source_refs"])
        self.assertEqual(model["call"]["provider_request_id"], "api-request-1")
        self.assertAlmostEqual(model["timing"]["start_ms"], 50, delta=1)
        self.assertAlmostEqual(model["timing"]["end_ms"], 100, delta=1)
        tool_spans = [span for span in document["spans"] if span["kind"] == "tool"]
        self.assertEqual(len(tool_spans), 1)
        self.assertEqual(tool_spans[0]["status"], "error")
        self.assertEqual(tool_spans[0]["tool"]["skill"], {"name": "trace-deep-dive", "action": "invoke"})
        self.assertEqual(sum(event["type"] == "compaction" for event in document["events"]), 2)
        url = (f"/api/v1/projects/{session['trace_project_id']}/traces/agent-{tid}/revisions/1"
               f"/sources/{source_id}/content")
        self.assertEqual(self.client.get(url).content, body)
        self.assertEqual(self.client.get(url.replace(source_id, response_id)).content, response_body)
        self.assertEqual(self.client.get(url.replace(session["trace_project_id"], "shared", 1)).status_code, 404)
        events = self.client.get(f"/api/v1/agent/sessions/{sid}/events/page?after=0")
        self.assertEqual(events.status_code, 200)
        self.assertEqual(events.json()["items"][0]["type"], "queued")

    def test_turn_scoped_otlp_on_api_loopback(self):
        session = self.client.post("/api/v1/agent/sessions", json={"project_id": "shared"}).json()
        sid = session["session_id"]
        tid = self.client.post(f"/api/v1/agent/sessions/{sid}/messages",
                               json={"text": "synthetic"}).json()["turn_id"]
        self.app.state.agent_sessions.claim()
        base = f"/api/v1/agent/worker/turns/{tid}/otel"
        worker = {"X-Agent-Worker-Token": "synthetic-worker-token"}
        token = "synthetic-telemetry-token-" + "x" * 32
        self.assertEqual(self.client.post(base + "/authorize", json={"token": token}).status_code, 403)
        self.assertEqual(self.client.post(base + "/authorize", json={"token": token},
                                          headers=worker).status_code, 200)
        self.assertEqual(self.client.post(base + "/authorize", json={"token": token},
                                          headers=worker).status_code, 422)
        body = {"resourceLogs": [{"scopeLogs": [{"logRecords": [{"body": {"stringValue": "synthetic"}}]}]}]}
        self.assertEqual(self.client.post(base + "/v1/logs", json=body).status_code, 403)
        self.assertEqual(self.client.post(base + "/v1/logs", json=body,
                                          headers={"X-Trace-Hunter-Recorder-Token": token}).status_code, 200)
        self.assertEqual(self.client.get(base).status_code, 403)
        fetched = self.client.get(base, headers=worker)
        self.assertEqual(fetched.status_code, 200, fetched.text)
        self.assertEqual(fetched.json()["items"][0]["value"], body)
        ref = ContentRef(**fetched.json()["items"][0]["content_ref"])
        with self.store.content.open_verified(ref) as source:
            self.assertEqual(json.load(source), body)
        self.app.state.agent_sessions.finish(tid, "succeeded")
        self.assertEqual(self.client.post(base + "/v1/logs", json=body,
                                          headers={"X-Trace-Hunter-Recorder-Token": token}).status_code, 409)
        self.assertEqual(self.app.state.agent_sessions.events(sid, session["owner_id"])[-1]["type"],
                         "recorder_telemetry_late")

    def test_multiturn_session_creates_one_immutable_trace_per_turn(self):
        session = self.client.post("/api/v1/agent/sessions", json={"project_id": "shared"}).json()
        sid = session["session_id"]
        runs = []
        for ordinal in (1, 2):
            prompt = f"synthetic turn {ordinal}"
            turn = self.client.post(f"/api/v1/agent/sessions/{sid}/messages",
                                    json={"text": prompt}).json()
            self.app.state.agent_sessions.claim()
            tid = turn["turn_id"]
            at = datetime.now(timezone.utc).isoformat()
            raw = {"session_id": sid, "turn_id": tid, "prompt": prompt, "started_at": at,
                   "ended_at": at, "state": "succeeded", "events": [{"at": at, "value": {
                       "type": "assistant", "message": {"id": f"message-{ordinal}",
                       "content": [{"type": "text", "text": f"answer {ordinal}"}]}}}]}
            endpoint = f"/api/v1/agent/sessions/{sid}/turns/{tid}/complete"
            saved = self.client.post(endpoint, content=json.dumps(raw).encode(),
                                     headers={"X-Agent-Worker-Token": "synthetic-worker-token"})
            self.assertEqual(saved.status_code, 200, saved.text)
            self.app.state.agent_sessions.finish(tid, "succeeded", run_id=saved.json()["run_id"])
            runs.append(saved.json()["run_id"])
            self.assertEqual(saved.json()["revision"], 1)
        self.assertEqual(len(set(runs)), 2)
        self.assertEqual(len(self.app.state.agent_sessions.get(sid, session["owner_id"])["turns"]), 2)

    def test_mock_claude_worker_through_internal_api(self):
        self.assertEqual(self.client.post("/api/v1/agent/worker/claim").status_code, 403)
        session = self.client.post("/api/v1/agent/sessions", json={"project_id": "shared"}).json()
        sid = session["session_id"]
        turn = self.client.post(f"/api/v1/agent/sessions/{sid}/messages",
                                json={"text": "inspect the trace"}).json()
        mock_path = Path(self.temp.name) / "claude-mock"
        mock_path.write_text(f"#!{sys.executable}\n"
            "import json, sys\n"
            "sys.stdin.read()\n"
            "for event in [\n"
            " {'type':'system','subtype':'init','model':'mock-model'},\n"
            " {'type':'stream_event','event':{'delta':{'type':'text_delta','text':'Found a '}}},\n"
            " {'type':'assistant','uuid':'a1','message':{'id':'request1','content':[{'type':'text','text':'Found a tool'}]}},\n"
            " {'type':'result','result':'Found a tool'}]:\n"
            " print(json.dumps(event), flush=True)\n")
        mock_path.chmod(0o700)

        def transport(request):
            response = self.client.request(request.method, request.url.path +
                                           ("?" + request.url.query.decode() if request.url.query else ""),
                                           content=request.content, headers=dict(request.headers))
            return httpx.Response(response.status_code, content=response.content,
                                  headers=dict(response.headers))

        with patch.dict(os.environ, {"TRACE_HUNTER_CLAUDE_BIN": str(mock_path),
                                  "TRACE_HUNTER_AGENT_WORK_DIR": str(Path(self.temp.name) / "worker"),
                                  "TRACE_HUNTER_AGENT_API_URL": "http://testserver"}):
            with httpx.Client(base_url="http://testserver", transport=httpx.MockTransport(transport)) as client:
                journal = JournalClient(client)
                claimed = journal.claim()
                self.assertEqual(claimed["turn_id"], turn["turn_id"])
                task = self.client.post("/api/v1/projects/shared/tasks", json={
                    "kind": "custom", "title": "Agent test", "request_key": "agent-test:" + turn["turn_id"],
                    "steps": [{"id": "execute", "label": "Run"}, {"id": "capture", "label": "Capture"}]})
                self.assertEqual(task.status_code, 202, task.text)
                journal.attach_task(turn["turn_id"], task.json()["task_id"])
                claimed["task_id"] = task.json()["task_id"]
                run_turn(journal, claimed, client, native_terminal=False)
        completed = self.client.get(f"/api/v1/agent/sessions/{sid}").json()["turns"][0]
        self.assertEqual(completed["state"], "succeeded", completed["error"])
        self.assertEqual(completed["run_id"], "agent-" + turn["turn_id"])
        self.assertEqual(self.store.revisions.get(session["trace_project_id"], completed["run_id"])["revision"], 1)
        events = self.client.get(f"/api/v1/agent/sessions/{sid}/events/page").json()["items"]
        self.assertEqual(events[-1]["type"], "succeeded")
        self.assertIn("trace_ready", [item["type"] for item in events])
        self.assertIn("delta", [item["type"] for item in events])
        task_result = self.client.get("/api/v1/projects/shared/tasks/" + task.json()["task_id"]).json()
        self.assertEqual(task_result["state"], "succeeded")
        self.assertEqual(task_result["result"]["trace_project_id"], session["trace_project_id"])
        workspace = Path(self.temp.name) / "worker" / sid / "workspace"
        self.assertTrue((workspace / "scripts" / "trace_hunter_cli.py").is_file())
        self.assertTrue((workspace / ".claude" / "skills" / "trace-hunter-cli" /
                         "scripts" / "trace_hunter_cli.py").is_file())
        profile = json.loads((Path(self.temp.name) / "worker" / sid / ".config" /
                              "trace-hunter" / "config.json").read_text())
        self.assertEqual(profile["current"], "agent")
        self.assertEqual(profile["profiles"]["agent"]["project"], "shared")

    def test_guest_cookie_separates_conversation_journal(self):
        created = self.client.post("/api/v1/agent/sessions", json={"project_id": "shared"}).json()
        own_items = self.client.get("/api/v1/agent/sessions").json()["items"]
        self.assertEqual([item["session_id"] for item in own_items], [created["session_id"]])
        with TestClient(self.app, client=("127.0.0.1", 47001)) as other:
            self.assertEqual(other.get("/api/v1/agent/sessions").status_code, 401)
            capability = other.get("/api/v1/agent/capabilities").json()
            self.assertFalse(capability["private_project_isolation"])
            self.assertEqual(other.get("/api/v1/agent/sessions").json()["items"], [])
            self.assertEqual(other.get("/api/v1/agent/sessions/" + created["session_id"]).status_code, 404)

    def test_worker_task_link_is_readable_to_project_viewer_but_not_writable(self):
        session = self.client.post("/api/v1/agent/sessions", json={"project_id": "shared"}).json()
        sid = session["session_id"]
        turn = self.client.post(f"/api/v1/agent/sessions/{sid}/messages",
                                json={"text": "Inspect a synthetic trace"}).json()
        task = self.client.post("/api/v1/projects/shared/tasks", json={
            "kind": "custom", "title": "Worker task", "request_key": "worker-link-1"}).json()
        tid = task["task_id"]
        self.app.state.agent_sessions.attach_task(turn["turn_id"], tid)
        linked = self.client.get(f"/api/v1/projects/shared/tasks/{tid}").json()
        self.assertEqual(linked["source"]["agent_session_id"], sid)
        self.assertEqual(linked["source"]["agent_session_kind"], "background")
        listed = self.client.get("/api/v1/projects/shared/tasks").json()["items"]
        self.assertEqual(listed[0]["source"]["agent_session_id"], sid)
        native = self.client.post("/api/v1/agent/sessions", json={"project_id": "shared"}).json()
        native_id = native["session_id"]
        native_turn = self.client.post(f"/api/v1/agent/sessions/{native_id}/messages",
                                       json={"text": "Import synthetic data"}).json()
        native_task, _ = self.app.state.tasks.create("shared", request_key="native-link-1",
            kind="custom", title="Native import", source={"type": "auto_import"})
        self.app.state.agent_sessions.attach_task(native_turn["turn_id"], native_task["task_id"])
        self.app.state.agent_sessions.attach_native_terminal(native_turn["turn_id"], native_id)
        self.app.state.agent_sessions.append(native_id, native_turn["turn_id"], "native_terminal_ready", {})
        native_link = self.client.get(f"/api/v1/projects/shared/tasks/{native_task['task_id']}").json()
        self.assertEqual(native_link["source"]["agent_session_id"], native_id)
        self.assertEqual(native_link["source"]["agent_session_kind"], "native_import")
        with TestClient(self.app, client=("127.0.0.1", 47001)) as other:
            self.assertEqual(other.get(f"/api/v1/projects/shared/tasks/{tid}").json()
                             ["source"]["agent_session_id"], sid)
            self.assertEqual(other.get(f"/api/v1/agent/sessions/{sid}").status_code, 200)
            self.assertEqual(other.get(f"/api/v1/agent/sessions/{sid}/events/page").status_code, 200)
            other.get("/api/v1/agent/capabilities")
            self.assertEqual(other.post(f"/api/v1/agent/sessions/{sid}/messages",
                                        json={"text": "not the owner"}).status_code, 404)
            self.assertEqual(other.post(f"/api/v1/agent/sessions/{sid}/cancel").status_code, 404)
            self.assertEqual(other.get("/api/v1/agent/sessions").json()["items"], [])

    def test_claude_creates_task_after_native_terminal_is_ready(self):
        session = self.client.post("/api/v1/agent/sessions", json={"project_id": "shared"}).json()
        sid = session["session_id"]
        turn = self.client.post(f"/api/v1/agent/sessions/{sid}/messages",
                                json={"text": "Plan a synthetic evaluation"}).json()
        claimed = self.client.post("/api/v1/agent/worker/claim",
                                   headers={"X-Agent-Worker-Token": "synthetic-worker-token"}).json()["turn"]
        self.assertEqual(claimed["turn_id"], turn["turn_id"])
        bound = self.client.post(f"/api/v1/agent/worker/turns/{turn['turn_id']}/native-terminal",
                                 headers={"X-Agent-Worker-Token": "synthetic-worker-token"},
                                 json={"terminal_session_id": sid})
        self.assertEqual(bound.status_code, 200, bound.text)
        self.app.state.agent_sessions.append(sid, turn["turn_id"], "native_terminal_ready", {})
        task = self.client.post("/api/v1/projects/shared/tasks", json={
            "kind": "evaluation", "title": "Synthetic evaluation", "request_key": "native-task-after-ready",
            "agent_session_id": sid})
        self.assertEqual(task.status_code, 202, task.text)
        self.assertEqual(task.json()["source"]["agent_session_kind"], "native_import")
        self.assertEqual(task.json()["source"]["agent_session_id"], sid)

    def test_worker_archives_native_claude_turn_without_precreated_task(self):
        session = self.client.post("/api/v1/agent/sessions", json={"project_id": "shared"}).json()
        sid = session["session_id"]
        queued = self.client.post(f"/api/v1/agent/sessions/{sid}/messages",
                                  json={"text": "Inspect a synthetic trace"}).json()

        def transport(request):
            response = self.client.request(request.method, request.url.path,
                                           content=request.content, headers=dict(request.headers))
            return httpx.Response(response.status_code, content=response.content,
                                  headers=dict(response.headers))

        class Bridge:
            def __init__(self):
                self.started = None
                self.status_reads = 0

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                pass

            @staticmethod
            def response(path, status, **kwargs):
                return httpx.Response(status, request=httpx.Request("GET", "http://testserver" + path),
                                      **kwargs)

            def get(self, path, headers=None):
                if path.endswith("/status"):
                    self.status_reads += 1
                    return self.response(path, 404) if self.status_reads == 1 else self.response(
                        path, 200, json={"stopped": True, "receipt": False, "live": True})
                if path.endswith("/files/transcript"):
                    event = {"type": "assistant", "timestamp": datetime.now(timezone.utc).isoformat(),
                             "message": {"id": "synthetic-model", "content": [
                                 {"type": "text", "text": "Synthetic answer"}]}}
                    return self.response(path, 200, content=(json.dumps(event) + "\n").encode())
                if path.endswith("/body"):
                    return self.response(path, 200, json={"names": []})
                return self.response(path, 404)

            def post(self, path, json=None):
                if path.endswith("/start"):
                    self.started = json
                    return self.response(path, 201, json={"cwd": "/synthetic/native-terminal"})
                raise AssertionError(path)

        bridge = Bridge()
        def files(_bridge, path, destination, **_kwargs):
            if path.endswith("/files/hooks"):
                destination.write_text(json.dumps({"at": datetime.now(timezone.utc).isoformat(),
                                                    "value": {"hook_event_name": "Stop"}}) + "\n")

        with tempfile.TemporaryDirectory() as directory, \
             patch.dict(os.environ, {"TRACE_HUNTER_AGENT_WORK_DIR": directory}), \
             patch("scripts.trace_hunter_agent_worker.native_import_bridge", return_value=bridge), \
             patch("scripts.trace_hunter_agent_worker.bridge_file", side_effect=files), \
             httpx.Client(base_url="http://testserver", transport=httpx.MockTransport(transport)) as client:
            journal = JournalClient(client)
            claimed = journal.claim()
            self.assertEqual(claimed["turn_id"], queued["turn_id"])
            run_turn(journal, claimed, client)
        self.assertEqual(bridge.started["kind"], "agent")
        self.assertNotIn("taskId", bridge.started)
        self.assertNotIn("sourceSha256", bridge.started)
        saved = self.client.get(f"/api/v1/agent/sessions/{sid}").json()["turns"][0]
        self.assertEqual(saved["state"], "succeeded")
        self.assertEqual(saved["run_id"], "agent-" + queued["turn_id"])
        listed = self.client.get("/api/v1/agent/sessions").json()["items"]
        self.assertTrue(listed[0]["native_terminal"])
        self.assertEqual(listed[0]["claude_session_id"], sid)
        task = self.client.post("/api/v1/projects/shared/tasks", json={
            "kind": "evaluation", "title": "Synthetic linked task",
            "request_key": "native-task-after-archiving", "agent_session_id": sid})
        self.assertEqual(task.status_code, 202, task.text)
        self.assertEqual(task.json()["source"]["agent_session_kind"], "native_import")
    def test_unlinked_foreign_session_stays_private(self):
        session = self.client.post("/api/v1/agent/sessions", json={"project_id": "shared"}).json()
        sid = session["session_id"]
        with TestClient(self.app, client=("127.0.0.1", 47002)) as other:
            other.get("/api/v1/agent/capabilities")
            self.assertEqual(other.get(f"/api/v1/agent/sessions/{sid}").status_code, 404)
            self.assertEqual(other.get(f"/api/v1/agent/sessions/{sid}/events/page").status_code, 404)

    def test_interrupted_worker_archives_partial_trace(self):
        session = self.client.post("/api/v1/agent/sessions", json={"project_id": "shared"}).json()
        sid = session["session_id"]
        turn = self.client.post(f"/api/v1/agent/sessions/{sid}/messages",
                                json={"text": "check partial work"}).json()
        home = Path(self.temp.name) / "worker" / sid
        home.mkdir(parents=True)
        at = datetime.now(timezone.utc).isoformat()
        (home / ("turn-" + turn["turn_id"] + ".jsonl")).write_text(json.dumps({
            "at": at, "value": {"type": "assistant", "uuid": "partial-1", "message": {
                "id": "partial-request", "content": [{"type": "text", "text": "partial answer"}]}}}) +
            '\n{"state":"partial","reason":"stream_budget"}\n')

        def transport(request):
            response = self.client.request(request.method, request.url.path,
                                           content=request.content, headers=dict(request.headers))
            return httpx.Response(response.status_code, content=response.content,
                                  headers=dict(response.headers))

        with patch.dict(os.environ, {"TRACE_HUNTER_AGENT_WORK_DIR": str(Path(self.temp.name) / "worker")}):
            with httpx.Client(base_url="http://testserver", transport=httpx.MockTransport(transport)) as client:
                journal = JournalClient(client)
                journal.claim()
                interrupted = journal.recover()
                self.assertEqual(len(interrupted), 1)
                recover_turn(journal, interrupted[0], client)
                self.assertEqual(journal.recover(), [])
        saved = self.client.get(f"/api/v1/agent/sessions/{sid}").json()["turns"][0]
        self.assertEqual(saved["state"], "failed")
        self.assertEqual(saved["run_id"], "agent-" + turn["turn_id"])
        archived = json.loads(self.store.revisions.read(session["trace_project_id"], saved["run_id"], 1))
        self.assertEqual(archived["run"]["status"], "failed")
        self.assertTrue(any(message["content"].get("search_text") == "partial answer"
                            for message in archived["messages"]))
        self.assertEqual(archived["extensions"]["trace_hunter.agent"]["stream_coverage"]["state"], "partial")

    def test_running_turn_cancel_archives_cancelled_trace(self):
        session = self.client.post("/api/v1/agent/sessions", json={"project_id": "shared"}).json()
        sid = session["session_id"]
        turn = self.client.post(f"/api/v1/agent/sessions/{sid}/messages",
                                json={"text": "wait for cancellation"}).json()
        mock_path = Path(self.temp.name) / "claude-slow"
        mock_path.write_text(f"#!{sys.executable}\n"
                             "import json, sys, time\n"
                             "sys.stdin.read()\n"
                             "print(json.dumps({'type':'system','subtype':'init','model':'mock-model'}), flush=True)\n"
                             "time.sleep(30)\n")
        mock_path.chmod(0o700)

        def transport(request):
            response = self.client.request(request.method, request.url.path,
                                           content=request.content, headers=dict(request.headers))
            return httpx.Response(response.status_code, content=response.content,
                                  headers=dict(response.headers))

        with patch.dict(os.environ, {"TRACE_HUNTER_CLAUDE_BIN": str(mock_path),
                                  "TRACE_HUNTER_AGENT_WORK_DIR": str(Path(self.temp.name) / "worker")}):
            with httpx.Client(base_url="http://testserver", transport=httpx.MockTransport(transport)) as client:
                journal = JournalClient(client)
                claimed = journal.claim()
                failure = []

                def run():
                    try:
                        run_turn(journal, claimed, client, native_terminal=False)
                    except BaseException as error:
                        failure.append(error)

                thread = threading.Thread(target=run)
                thread.start()
                time.sleep(0.5)
                self.assertEqual(self.client.post(f"/api/v1/agent/sessions/{sid}/cancel").status_code, 200)
                thread.join(timeout=8)
                self.assertFalse(thread.is_alive())
                self.assertEqual(failure, [])
        saved = self.client.get(f"/api/v1/agent/sessions/{sid}").json()["turns"][0]
        self.assertEqual(saved["state"], "cancelled")
        self.assertEqual(saved["run_id"], "agent-" + turn["turn_id"])
