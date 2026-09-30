"""Automatic upload-to-Agent handoff and restart-safe progress contract."""

import hashlib
import os
import tempfile
import unittest
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from trace_hunter.storage import Store
from trace_hunter_api.app import create_app
from scripts.trace_hunter_agent_worker import JournalClient, auto_import_stage, run_turn, verify_auto_import


class AutoAgentImportTests(unittest.TestCase):
    def test_agent_progress_requires_an_explicit_monotonic_marker(self):
        message = lambda value: {"type": "assistant", "message": {"content": [
            {"type": "text", "text": value}]}}
        self.assertEqual(auto_import_stage(message("TRACE_HUNTER_IMPORT_STAGE: adapter_select"), 0.1),
                         ("adapter_select", 0.3))
        self.assertIsNone(auto_import_stage(message("TRACE_HUNTER_IMPORT_STAGE: adapter_select"), 0.5))
        self.assertIsNone(auto_import_stage(message("source says TRACE_HUNTER_IMPORT_STAGE: trace_import"), 0.1))
        self.assertIsNone(auto_import_stage({"type": "user", "message": {"content": [
            {"type": "text", "text": "TRACE_HUNTER_IMPORT_STAGE: trace_import"}]}}, 0.1))

    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.dict(os.environ, {
            "TRACE_HUNTER_AGENT_ENABLED": "1",
            "TRACE_HUNTER_AGENT_WORKER_TOKEN": "synthetic-worker-token",
            "TRACE_HUNTER_AGENT_STATE_DIR": str(self.root / "agent-state"),
        }))
        self.store = Store(self.root / "db.sqlite")
        self.addCleanup(self.store.close)
        self.app = create_app(self.store)
        self.app.state.projects.create_project("synthetic", "Synthetic")
        self.client = self.enterContext(TestClient(self.app, client=("127.0.0.1", 49000)))
        self.base = "/api/v1/projects/synthetic/imports/uploads"

    def test_upload_queues_one_agent_and_restart_retry_keeps_source(self):
        raw = b'{"synthetic":true}'
        digest = hashlib.sha256(raw).hexdigest()
        self.assertTrue(self.client.get("/api/v1/agent/capabilities").json()["enabled"])
        body = {"request_key": "auto-synthetic", "source_format": "auto", "size_bytes": len(raw),
                "sha256": digest, "part_size": 1024 * 1024, "source_name": "synthetic.json"}
        created = self.client.post(self.base, json=body)
        self.assertEqual(created.status_code, 201, created.text)
        upload_id = created.json()["upload_id"]
        part = self.client.put(self.base + f"/{upload_id}/parts/0", content=raw,
                               headers={"X-Chunk-SHA256": digest})
        self.assertEqual(part.status_code, 200, part.text)
        completed = self.client.post(self.base + f"/{upload_id}/complete")
        self.assertEqual(completed.status_code, 200, completed.text)
        first = completed.json()
        self.assertEqual(first["state"], "completed")
        self.assertEqual(self.client.post(self.base + f"/{upload_id}/complete").json(), first)
        task = self.client.get(f"/api/v1/projects/synthetic/tasks/{first['job_id']}").json()
        self.assertEqual((task["state"], task["source"]["type"]), ("running", "auto_import"))
        catalog = self.client.get("/api/v1/agent/sessions")
        self.assertEqual(catalog.status_code, 200, catalog.text)
        self.assertEqual(len(catalog.json()["items"]), 1)
        self.assertEqual(catalog.json()["items"][0]["session_id"], first["agent_session_id"])
        self.assertEqual(catalog.json()["items"][0]["task_id"], first["job_id"])
        self.assertEqual(catalog.json()["items"][0]["source_name"], "synthetic.json")
        self.assertEqual(catalog.json()["items"][0]["kind"], "auto_import")
        self.assertEqual(self.client.patch(f"/api/v1/projects/synthetic/tasks/{first['job_id']}",
                                           json={"state": "succeeded"}).status_code, 403)
        session = self.client.get(f"/api/v1/agent/sessions/{first['agent_session_id']}").json()
        self.assertEqual(len(session["turns"]), 1)
        self.assertEqual(session["turns"][0]["turn_id"], first["agent_turn_id"])
        self.assertIn("不能仅原样导入", session["turns"][0]["prompt"])
        self.assertEqual(session["attachments"][0]["content_ref"]["digest"], "sha256:" + digest)
        self.assertEqual(self.store.repository.rows("SELECT count(*) AS total FROM traces")[0]["total"], 0)

        self.app.state.uploads.close()
        restarted = create_app(self.store)
        with TestClient(restarted, client=("127.0.0.1", 49000)) as client:
            client.cookies.update(self.client.cookies)
            client.get("/api/v1/agent/capabilities")
            failed = client.get(f"/api/v1/projects/synthetic/tasks/{first['job_id']}").json()
            self.assertEqual(failed["state"], "failed")
            stale = client.get(f"/api/v1/agent/sessions/{first['agent_session_id']}").json()
            self.assertEqual(stale["turns"][0]["state"], "cancelled")
            retried = client.post(f"/api/v1/projects/synthetic/tasks/{first['job_id']}/retry")
            self.assertEqual(retried.status_code, 200, retried.text)
            self.assertEqual(retried.json()["task_id"], first["job_id"])
            second = client.get(self.base + f"/{upload_id}").json()
            self.assertEqual(second["job_id"], first["job_id"])
            self.assertNotEqual(second["agent_session_id"], first["agent_session_id"])
            restored = client.get("/api/v1/agent/sessions").json()["items"]
            self.assertEqual({item["session_id"] for item in restored},
                             {first["agent_session_id"], second["agent_session_id"]})
            self.assertEqual(client.get(f"/api/v1/projects/synthetic/tasks/{second['job_id']}").json()["state"],
                             "running")
            cancelled = client.post(f"/api/v1/projects/synthetic/tasks/{second['job_id']}/cancel")
            self.assertEqual(cancelled.status_code, 200, cancelled.text)
            self.assertEqual(cancelled.json()["state"], "cancelled")
            self.assertEqual(cancelled.json()["task_id"], second["job_id"])
        restarted.state.uploads.close()

    def test_duplicate_upload_notice_distinguishes_imported_from_pending(self):
        raw = b'{"synthetic":true}'
        digest = hashlib.sha256(raw).hexdigest()
        self.client.get("/api/v1/agent/capabilities")
        body = {"request_key": "web-auto:synthetic:same-file", "source_format": "auto",
                "size_bytes": len(raw), "sha256": digest, "part_size": 1024 * 1024,
                "source_name": "same-file.json", "binding": {}, "expected_previous": 0}
        created = self.client.post(self.base, json=body)
        self.assertEqual(created.status_code, 201, created.text)
        self.assertFalse(created.json()["reused"])
        upload_id = created.json()["upload_id"]
        self.assertEqual(self.client.put(self.base + f"/{upload_id}/parts/0", content=raw,
                                         headers={"X-Chunk-SHA256": digest}).status_code, 200)
        completed = self.client.post(self.base + f"/{upload_id}/complete").json()

        with TestClient(self.app, client=("127.0.0.1", 49001)) as other:
            other.get("/api/v1/agent/capabilities")
            pending = other.post(self.base, json=body)
            self.assertEqual(pending.status_code, 409, pending.text)
            self.assertEqual(pending.json()["code"], "UPLOAD_ALREADY_SUBMITTED")
            self.assertEqual(other.get("/api/v1/agent/sessions").json()["items"], [])

            task = self.app.state.tasks.get("synthetic", completed["job_id"])
            self.app.state.tasks.publish({**task, "state": "succeeded", "progress": 1.0,
                                          "result": {"run_id": "synthetic-run", "revision": 1}})
            imported = other.post(self.base, json=body)
            self.assertEqual(imported.status_code, 409, imported.text)
            self.assertEqual(imported.json()["code"], "UPLOAD_ALREADY_IMPORTED")
            changed = other.post(self.base, json={**body, "source_name": "different.json"})
            self.assertEqual(changed.status_code, 409, changed.text)
            self.assertNotIn("code", changed.json())

        replay = self.client.post(self.base, json=body)
        self.assertEqual(replay.status_code, 201, replay.text)
        self.assertTrue(replay.json()["reused"])
        self.assertEqual(replay.json()["upload_id"], upload_id)
        self.assertEqual(replay.json()["job_id"], completed["job_id"])

    def test_adapter_script_is_archived_and_retrievable_only_for_project(self):
        raw = b'{"synthetic":true}'
        digest = hashlib.sha256(raw).hexdigest()
        self.client.get("/api/v1/agent/capabilities")
        created = self.client.post(self.base, json={
            "request_key": "script-synthetic", "source_format": "auto", "size_bytes": len(raw),
            "sha256": digest, "part_size": 1024 * 1024, "source_name": "synthetic.json"}).json()
        upload_id = created["upload_id"]
        self.client.put(self.base + f"/{upload_id}/parts/0", content=raw,
                        headers={"X-Chunk-SHA256": digest})
        completed = self.client.post(self.base + f"/{upload_id}/complete").json()
        script = b"# synthetic adapter\nprint('ok')\n"
        archived = self.client.post(
            f"/api/v1/agent/worker/turns/{completed['agent_turn_id']}/adapter-artifact?name=synthetic-v1",
            content=script, headers={"X-Agent-Worker-Token": "synthetic-worker-token"})
        self.assertEqual(archived.status_code, 200, archived.text)
        ref = archived.json()["content_ref"]
        self.app.state.tasks.update("synthetic", completed["job_id"], state="succeeded",
                                    artifacts=[{"kind": "adapter_script", "name": "synthetic-v1", "ref": ref}])
        listing = self.client.get("/api/v1/projects/synthetic/import-adapters")
        self.assertEqual(len(listing.json()["items"]), 1)
        fetched = self.client.get("/api/v1/projects/synthetic/import-adapters/" +
                                  ref["digest"].removeprefix("sha256:") + "/content")
        self.assertEqual(fetched.content, script)
        self.assertEqual(self.client.get("/api/v1/projects/other/import-adapters/" +
                                        ref["digest"].removeprefix("sha256:") + "/content").status_code, 404)

    def test_worker_receipt_requires_original_source_in_stored_trace(self):
        digest = hashlib.sha256(b"synthetic source").hexdigest()
        workspace = self.root / "workspace"
        workspace.mkdir()
        (workspace / "import-result.json").write_text(json.dumps({
            "status": "succeeded", "adapter": "synthetic-v1", "run_id": "synthetic-run",
            "revision": 1, "source_sha256": digest}))
        turn = {"project_id": "synthetic", "turn_id": "synthetic-turn"}
        source = {"source_sha256": digest}

        class Client:
            def __init__(self, observed): self.observed = observed
            def request(self, method, path, **kwargs):
                del method, kwargs
                class Reply:
                    def __init__(self, value): self.value = value
                    def raise_for_status(self): pass
                    def json(self): return self.value
                return Reply({"sources": [{"sha256": self.observed}]} if path.endswith("/content")
                             else {"index": {"state": "complete"}})

        self.assertEqual(verify_auto_import(Client(digest), turn, workspace, source)["run_id"], "synthetic-run")
        with self.assertRaisesRegex(ValueError, "does not reference"):
            verify_auto_import(Client("0" * 64), turn, workspace, source)

    def test_mock_worker_completes_the_preassigned_import_task_after_readback(self):
        raw = b'{"synthetic":"uploaded fixture"}'
        digest = hashlib.sha256(raw).hexdigest()
        self.client.get("/api/v1/agent/capabilities")
        created = self.client.post(self.base, json={
            "request_key": "end-to-end-synthetic", "source_format": "auto", "size_bytes": len(raw),
            "sha256": digest, "part_size": 1024 * 1024, "source_name": "synthetic.json"})
        self.assertEqual(created.status_code, 201, created.text)
        upload_id = created.json()["upload_id"]
        self.assertEqual(self.client.put(self.base + f"/{upload_id}/parts/0", content=raw,
                                         headers={"X-Chunk-SHA256": digest}).status_code, 200)
        upload = self.client.post(self.base + f"/{upload_id}/complete").json()

        # A mock Agent imports a synthetic canonical document; the worker must still
        # verify the actual revision and the original uploaded source digest.
        fixture = Path(__file__).resolve().parents[1] / "examples/drafts/trace-v2/multiturn-resume.json"
        document = json.loads(fixture.read_text())
        document["run"]["id"] = "synthetic-auto-run"
        document["sources"][0]["sha256"] = digest
        document["sources"][0]["locator"] = "content:sha256:" + digest
        imported = self.client.post("/api/v1/projects/synthetic/traces", content=json.dumps(document).encode(),
                                    headers={"Content-Type": "application/json",
                                             "Idempotency-Key": "synthetic-auto-import"})
        self.assertEqual(imported.status_code, 201, imported.text)
        self.assertEqual(imported.json()["index"]["state"], "complete")

        receipt = {"status": "succeeded", "adapter": "synthetic-fixture-v1",
                   "run_id": "synthetic-auto-run", "revision": 1, "source_sha256": digest}
        event_time = datetime.now(timezone.utc).isoformat()
        transcript = json.dumps({"type": "assistant", "timestamp": event_time,
                                 "message": {"id": "synthetic-model", "content": [
                                     {"type": "text", "text": "Synthetic import verified"}]}}).encode() + b"\n"
        hook = json.dumps({"at": event_time, "state": "complete",
                           "value": {"hook_event_name": "Stop"}}).encode() + b"\n"

        def transport(request):
            response = self.client.request(request.method, request.url.path +
                                           ("?" + request.url.query.decode() if request.url.query else ""),
                                           content=request.content, headers=dict(request.headers))
            if response.status_code >= 400:
                raise AssertionError(f"{request.url.path}: {response.status_code} {response.text[:1000]}")
            return httpx.Response(response.status_code, content=response.content,
                                  headers=dict(response.headers))

        def native_transport(request):
            path = request.url.path
            if request.method == "PUT":
                self.assertEqual(request.content, raw)
                self.assertEqual(request.headers["x-content-sha256"], digest)
                return httpx.Response(200, json={"path": "/private/attachments/synthetic.json", "sha256": digest})
            if path.endswith("/start"):
                self.assertEqual(json.loads(request.content)["sourceSha256"], digest)
                return httpx.Response(201, json={"cwd": "/private/native-import", "reused": False})
            if path.endswith("/status"):
                return httpx.Response(200, json={"stopped": True, "receipt": True, "transcript": True})
            if path.endswith("/files/transcript"):
                return httpx.Response(416 if "range" in request.headers else 200,
                                      content=b"" if "range" in request.headers else transcript)
            if path.endswith("/files/hooks"):
                return httpx.Response(200, content=hook)
            if path.endswith("/files/receipt"):
                return httpx.Response(200, content=json.dumps(receipt).encode())
            if path.endswith("/files/body-index"):
                return httpx.Response(404)
            if path.endswith("/body"):
                return httpx.Response(200, json={"names": []})
            raise AssertionError(path)

        def native_client():
            return httpx.Client(base_url="http://native", transport=httpx.MockTransport(native_transport))

        with patch.dict(os.environ, {"TRACE_HUNTER_AGENT_WORK_DIR": str(self.root / "worker"),
                                  "TRACE_HUNTER_AGENT_API_URL": "http://testserver",
                                  "TRACE_HUNTER_IMPORT_BRIDGE_URL": "http://127.0.0.1:34567",
                                  "TRACE_HUNTER_IMPORT_BRIDGE_TOKEN": "synthetic-native-bridge-token-0123456789"}):
            with (patch("scripts.trace_hunter_agent_worker.native_import_bridge", side_effect=native_client),
                  patch("scripts.trace_hunter_agent_worker.subprocess.Popen", side_effect=AssertionError("print mode must not run"))):
                with httpx.Client(base_url="http://testserver", transport=httpx.MockTransport(transport)) as client:
                    journal = JournalClient(client)
                    turn = journal.claim()
                    self.assertEqual(turn["task_id"], upload["job_id"])
                    run_turn(journal, turn, client)

        task = self.client.get(f"/api/v1/projects/synthetic/tasks/{upload['job_id']}").json()
        self.assertEqual(task["state"], "succeeded", task["error"])
        self.assertEqual(task["result"]["run_id"], "synthetic-auto-run")
        self.assertEqual(task["result"]["source_sha256"], digest)
        self.assertEqual(len(self.client.get("/api/v1/projects/synthetic/tasks").json()["items"]), 1)
        self.assertEqual(self.client.get(f"/api/v1/agent/sessions/{upload['agent_session_id']}").json()
                         ["turns"][0]["state"], "succeeded")


if __name__ == "__main__":
    unittest.main()
