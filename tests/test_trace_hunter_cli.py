"""The JSON CLI preserves API requests, pagination and failure semantics."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import threading
import unittest

from scripts import trace_hunter_cli


class Handler(BaseHTTPRequestHandler):
    requests = []
    event_state = "succeeded"
    query_status = 200

    def log_message(self, *_):
        pass

    def _record(self):
        size = int(self.headers.get("content-length", 0))
        raw = self.rfile.read(size)
        try: body = json.loads(raw) if raw else None
        except json.JSONDecodeError: body = raw
        self.requests.append({"method": self.command, "path": self.path,
                              "headers": dict(self.headers), "body": body, "raw": raw})
        return body

    def _reply(self, value, status=200):
        raw = json.dumps(value).encode()
        self.send_response(status); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)

    def do_GET(self):
        self._record()
        if self.path.endswith("/events"):
            value = {"task_id": "task-1", "state": self.event_state, "revision": 2}
            if self.event_state == "failed":
                value["error"] = {"message": "adapter rejected input", "issues": [{"code": "SOURCE_INVALID"}]}
            raw = ("id: 2\nevent: task\ndata: " + json.dumps(value) + "\n\n").encode()
            self.send_response(200); self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw); return
        if self.path.startswith("/api/v1/projects/p1/tasks?"):
            return self._reply({"items": [{"task_id": "task-1"}], "total": 1, "truncated": False})
        if "/tasks/" in self.path:
            return self._reply({"task_id": self.path.rsplit("/", 1)[-1], "state": "running", "revision": 1})
        if "capabilities" in self.path:
            return self._reply({"version": self.path})
        return self._reply({"project_id": "p1"})

    def do_POST(self):
        body = self._record()
        if self.path.endswith("/traces/query") and self.query_status != 200:
            return self._reply({"error": "invalid trace query", "details": [{"field": "limit"}]},
                               self.query_status)
        if self.path == "/api/v1/projects/p1/imports/uploads":
            return self._reply({"upload_id": "upload-" + "a" * 24, "missing_parts": [0],
                                "part_size": body["part_size"], "job_id": None}, 201)
        if self.path.endswith("/complete"):
            return self._reply({"upload_id": "upload-" + "a" * 24, "missing_parts": [],
                                "part_size": 1024 * 1024, "job_id": "import-1"})
        if self.path == "/api/v1/projects/p1/tasks":
            return self._reply({"task_id": "task-1", "state": "queued", "request": body}, 202)
        if self.path.endswith("/cancel"):
            return self._reply({"task_id": "task-1", "state": "cancelled"})
        if self.path.endswith("/search"):
            cursor = body.get("cursor")
            return self._reply({"items": [{"page": 2 if cursor else 1}],
                                "next_cursor": None if cursor else "next"})
        if "/traces?" in self.path:
            return self._reply({"created": True}, 201)
        if self.path.endswith("/spans/query"):
            return self._reply({"items": [], "next_cursor": None, "request": body})
        if self.path.endswith("/fail"):
            return self._reply({"error": "bad request", "details": [{"field": "x"}]}, 422)
        return self._reply({"request": body})

    def do_PUT(self):
        self._record()
        return self._reply({"upload_id": "upload-" + "a" * 24, "missing_parts": [],
                            "part_size": 1024 * 1024, "job_id": None})

    def do_PATCH(self):
        body = self._record()
        return self._reply({"task_id": "task-1", "state": body.get("state"), "request": body})


class TraceHunterCLITests(unittest.TestCase):
    def setUp(self):
        Handler.requests = []
        Handler.event_state = "succeeded"
        Handler.query_status = 200
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True); thread.start()
        self.addCleanup(self.server.server_close); self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def run_cli(self, *args, environ=None):
        stdout, stderr = io.StringIO(), io.StringIO()
        code = trace_hunter_cli.main(["--url", self.url, *args],
                                     environ=environ or {}, stdout=stdout, stderr=stderr)
        return code, json.loads(stdout.getvalue()) if stdout.getvalue() else None, \
            json.loads(stderr.getvalue()) if stderr.getvalue() else None

    def test_recent_trace_query_order_is_forwarded(self):
        code, result, error = self.run_cli("--project", "p1", "trace", "query",
                                           "--order", "created_at_desc", "--limit", "3")
        self.assertEqual((code, error), (0, None))
        self.assertEqual(result["request"]["order"], "created_at_desc")
        self.assertEqual(result["request"]["limit"], 3)

    def test_http_422_in_bash_pipeline_is_not_masked(self):
        Handler.query_status = 422
        root = Path(__file__).resolve().parents[1]
        cli = root / "scripts/trace_hunter_cli.py"
        command = (f"{shlex.quote(sys.executable)} {shlex.quote(str(cli))} --url "
                   f"{shlex.quote(self.url)} --project p1 trace query 2>&1 | head -n 1")
        result = subprocess.run(["bash", "-c", command], capture_output=True, text=True,
                                env={**os.environ, "BASH_ENV": str(root / "scripts/trace_hunter_agent_bash_env.sh")})
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], 422)

    def test_search_builds_filters_and_collects_all_pages(self):
        code, result, error = self.run_cli("--project", "p1", "search", "lark-cli",
            "--mode", "regex", "--span-id", "s1,s2", "--field", "input", "--all-pages")
        self.assertEqual((code, error), (0, None))
        self.assertEqual(result["items"], [{"page": 1}, {"page": 2}])
        self.assertTrue(result["pages_collected"])
        first, second = Handler.requests
        self.assertEqual(first["body"]["filters"], {"span_id": ["s1", "s2"], "field": ["input"]})
        self.assertEqual(second["body"]["cursor"], "next")

    def test_regex_syntax_is_forwarded_only_for_regex_mode(self):
        code, _, error = self.run_cli("--project", "p1", "search", r"\btool\b",
                                      "--mode", "regex", "--regex-syntax", "portable")
        self.assertEqual((code, error), (0, None))
        self.assertEqual(Handler.requests[-1]["body"]["regex_syntax"], "portable")
        code, _, error = self.run_cli("--project", "p1", "search", "tool",
                                      "--regex-syntax", "portable")
        self.assertNotEqual(code, 0)
        self.assertIn("--mode regex", error["error"])

    def test_evaluation_term_vocabulary_is_cli_first(self):
        code, result, error = self.run_cli("--project", "p1", "search", "tool", "--evaluation")
        self.assertEqual((code, error), (0, None))
        self.assertEqual(Handler.requests[-1]["body"]["purpose"], "evaluation")
        code, _, error = self.run_cli("--project", "p1", "search", "tool", "--mode", "regex", "--evaluation")
        self.assertNotEqual(code, 0)
        self.assertIn("--evaluation", error["error"])
        self.assertEqual(self.run_cli("--project", "p1", "search-terms")[0], 0)
        self.assertEqual(Handler.requests[-1]["path"], "/api/v1/projects/p1/search/terms")
        for command, action in (("search-term-promote", "promote"),
                                ("search-term-demote", "demote")):
            self.assertEqual(self.run_cli("--project", "p1", command, "tool")[0], 0)
            self.assertEqual(Handler.requests[-1]["path"], f"/api/v1/projects/p1/search/terms/{action}")
            self.assertEqual(Handler.requests[-1]["body"], {"term": "tool"})

    def test_import_preserves_bytes_and_has_stable_default_key(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "trace.json"; raw = b'{"schema_version":"test"}\n'; path.write_bytes(raw)
            code, result, _ = self.run_cli("--project", "p1", "import", str(path))
        self.assertEqual((code, result), (0, {"created": True}))
        request = Handler.requests[0]
        self.assertEqual(request["raw"], raw)
        self.assertTrue(request["headers"]["Idempotency-Key"].startswith("cli:"))
        self.assertIn("expected_previous=0", request["path"])

    def test_adapter_import_returns_frontend_visible_task(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source.json"; raw = b'{"schema_version":"ATIF-v1.8"}\n'; path.write_bytes(raw)
            code, result, _ = self.run_cli("--project", "p1", "adapter-import", str(path),
                                           "--source-format", "ATIF-v1.8", "--run-id", "r1")
        self.assertEqual((code, result["job_id"]), (0, "import-1"))
        self.assertEqual(Handler.requests[0]["body"]["source_format"], "ATIF-v1.8")
        self.assertEqual(Handler.requests[0]["body"]["binding"]["run_id"], "r1")
        self.assertIsNone(Handler.requests[0]["body"]["source_name"])
        self.assertEqual(Handler.requests[0]["body"]["request_key"],
                         "adapter-cli:" + hashlib.sha256(raw).hexdigest())
        self.assertEqual(Handler.requests[1]["raw"], raw)
        self.assertEqual([r["method"] for r in Handler.requests], ["POST", "PUT", "POST"])
        self.assertTrue(all("/imports/uploads" in r["path"] for r in Handler.requests))

    def test_adapter_import_wait_returns_nonzero_when_task_fails(self):
        Handler.event_state = "failed"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source.json"; path.write_text('{"schema_version":"ATIF-v1.8"}\n')
            stdout, stderr = io.StringIO(), io.StringIO()
            code = trace_hunter_cli.main([
                "--url", self.url, "--project", "p1", "adapter-import", str(path),
                "--source-format", "ATIF-v1.8", "--run-id", "r1", "--wait",
            ], environ={}, stdout=stdout, stderr=stderr)
        self.assertNotEqual(code, 0)
        self.assertEqual(json.loads(stderr.getvalue())["error"], "adapter rejected input")
        self.assertEqual(json.loads(stdout.getvalue().splitlines()[-1])["state"], "failed")

    def test_task_commands_use_unified_routes(self):
        code, result, _ = self.run_cli("--project", "p1", "task", "list", "--state", "running",
                                       "--cursor", "next-page")
        self.assertEqual((code, result["total"]), (0, 1))
        self.assertIn("state=running", Handler.requests[-1]["path"])
        self.assertIn("after=next-page", Handler.requests[-1]["path"])
        code, result, _ = self.run_cli("--project", "p1", "task", "create", "evaluation", "检查 Skill",
                                       "--request-key", "eval-1", "--body", '{"steps":[]}')
        self.assertEqual(result["request"]["kind"], "evaluation")
        code, result, _ = self.run_cli("--project", "p1", "task", "update", "task-1",
                                       "--body", '{"state":"running","progress":0.5}')
        self.assertEqual(result["state"], "running")

    def test_terminal_task_create_binds_its_native_claude_session(self):
        session_id = "11111111-1111-4111-8111-111111111111"
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.json"
            config.write_text(json.dumps({"version": 1, "current": "terminal", "profiles": {
                "terminal": {"url": self.url, "project": "p1"}}}))
            stdout, stderr = io.StringIO(), io.StringIO()
            code = trace_hunter_cli.main(["--profile", "terminal", "task", "create", "evaluation",
                                          "Bound task", "--request-key", "bound-1"],
                environ={"TRACE_HUNTER_CONFIG": str(config), "MULMOTERMINAL_SESSION_ID": session_id},
                stdout=stdout, stderr=stderr)
            explicit_request = Handler.requests[-1]["body"]
            implicit_stdout, implicit_stderr = io.StringIO(), io.StringIO()
            implicit_code = trace_hunter_cli.main(["task", "create", "evaluation",
                                                   "Implicit terminal task", "--request-key", "bound-2"],
                environ={"TRACE_HUNTER_CONFIG": str(config), "MULMOTERMINAL_SESSION_ID": session_id},
                stdout=implicit_stdout, stderr=implicit_stderr)
        self.assertEqual((code, stderr.getvalue()), (0, ""))
        self.assertEqual((implicit_code, implicit_stderr.getvalue()), (0, ""))
        self.assertEqual(explicit_request["agent_session_id"], session_id)
        self.assertEqual(Handler.requests[-1]["body"]["agent_session_id"], session_id)
        code, _, _ = self.run_cli("--project", "p1", "task", "create", "evaluation", "External task",
                                   environ={"MULMOTERMINAL_SESSION_ID": session_id})
        self.assertEqual(code, 0)
        self.assertNotIn("agent_session_id", Handler.requests[-1]["body"])

    def test_worker_agent_profile_binds_its_existing_session(self):
        session_id = "22222222-2222-4222-8222-222222222222"
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.json"
            config.write_text(json.dumps({"version": 1, "current": "agent", "profiles": {
                "agent": {"url": self.url, "project": "p1"}}}))
            stdout, stderr = io.StringIO(), io.StringIO()
            code = trace_hunter_cli.main(["task", "create", "evaluation", "Worker task",
                                          "--request-key", "worker-1"],
                environ={"TRACE_HUNTER_CONFIG": str(config), "TRACE_HUNTER_AGENT_SESSION_ID": session_id},
                stdout=stdout, stderr=stderr)
        self.assertEqual((code, stderr.getvalue()), (0, ""))
        self.assertEqual(Handler.requests[-1]["body"]["agent_session_id"], session_id)

    def test_task_watch_streams_ndjson_snapshots(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        code = trace_hunter_cli.main(["--url", self.url, "--project", "p1", "task", "watch", "task-1"],
                                     environ={}, stdout=stdout, stderr=stderr)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout.getvalue())["state"], "succeeded")

    def test_span_query_merges_shortcuts_with_json_body(self):
        code, result, _ = self.run_cli("--project", "p1", "span", "--skill-name", "lark-cli",
            "--min-duration-ms", "10", "--fields", "run_id,span_id,duration_ms",
            "--body", '{"filters":{"status":["ok"]}}')
        self.assertEqual(code, 0)
        request = result["request"]
        self.assertEqual(request["filters"], {"status": ["ok"], "skill_name": ["lark-cli"],
                                               "min_duration_ms": 10.0})
        self.assertEqual(request["fields"], ["run_id", "span_id", "duration_ms"])

    def test_span_window_uses_public_route(self):
        code, result, _ = self.run_cli("--project", "p1", "span-window", "run-1", "3", "span-9",
            "--before", "50", "--after", "2", "--include", "documents,related_objects,edges")
        self.assertEqual(code, 0)
        self.assertEqual(Handler.requests[-1]["path"], "/api/v1/projects/p1/spans/window")
        self.assertEqual(result["request"]["anchor"], {
            "run_id": "run-1", "revision": 3, "span_id": "span-9"})
        self.assertEqual(result["request"]["include"], ["documents", "related_objects", "edges"])
    def test_capabilities_reads_all_tool_contracts(self):
        code, result, _ = self.run_cli("capabilities")
        self.assertEqual(code, 0)
        self.assertEqual(set(result), {"trace", "span", "search", "advanced", "import", "task"})
        self.assertEqual(len(Handler.requests), 6)

    def test_http_error_is_machine_readable(self):
        client = trace_hunter_cli.Client(self.url, {})
        with self.assertRaises(trace_hunter_cli.CLIError) as caught:
            client.request("POST", "/fail", body={})
        self.assertEqual((caught.exception.code, caught.exception.status), (3, 422))
        self.assertEqual(caught.exception.details, [{"field": "x"}])

    def test_credentials_are_read_from_environment(self):
        code, _, _ = self.run_cli("capabilities", "trace", environ={
            "TRACE_HUNTER_USERNAME": "operator", "TRACE_HUNTER_PASSWORD": "secret"})
        self.assertEqual(code, 0)
        self.assertTrue(Handler.requests[0]["headers"]["Authorization"].startswith("Basic "))

    def test_cloud_profile_selects_url_project_and_nonsecret_username(self):
        with tempfile.TemporaryDirectory() as directory:
            config = str(Path(directory) / "config.json")
            environment = {"TRACE_HUNTER_CONFIG": config}
            stdout, stderr = io.StringIO(), io.StringIO()
            code = trace_hunter_cli.main(["profile", "set", "cloud", "--url", self.url,
                "--project", "p1", "--username", "operator"], environ=environment,
                stdout=stdout, stderr=stderr)
            self.assertEqual(code, 0)
            trace_hunter_cli.main(["profile", "use", "cloud"], environ=environment,
                                  stdout=io.StringIO(), stderr=io.StringIO())
            environment["TRACE_HUNTER_PASSWORD"] = "secret"
            stdout = io.StringIO()
            code = trace_hunter_cli.main(["project", "get", "p1"], environ=environment,
                                         stdout=stdout, stderr=io.StringIO())
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(stdout.getvalue())["project_id"], "p1")
            self.assertTrue(Handler.requests[-1]["headers"]["Authorization"].startswith("Basic "))
            self.assertEqual(Path(config).stat().st_mode & 0o777, 0o600)

    def test_resumable_upload_can_wait_for_visible_import_task(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "large.json"; path.write_bytes(b"{}")
            stdout, stderr = io.StringIO(), io.StringIO()
            code = trace_hunter_cli.main(["--url", self.url, "--project", "p1", "upload", str(path),
                "--source-format", "agent-benchmark/1", "--run-id", "r1", "--batch-id", "b1",
                "--batch-total", "1", "--wait"], environ={}, stdout=stdout, stderr=stderr)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout.getvalue().splitlines()[-1])["state"], "succeeded")
        self.assertEqual(Handler.requests[0]["body"]["batch_id"], "b1")


if __name__ == "__main__":
    unittest.main()
