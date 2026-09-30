"""Unified tasks expose import and agent progress through one durable contract."""

import json
import hashlib
import time
import sys
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "apps/api")]

from trace_hunter_api.app import create_app
from trace_hunter.storage import Store
from trace_hunter.tasks import MAX_TASKS, TaskRegistry


class TaskAPITests(unittest.TestCase):
    def setUp(self):
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.store = Store(Path(self.directory) / "tasks.sqlite")
        self.addCleanup(self.store.close)
        with self.store.repository.engine.begin() as db:
            db.exec_driver_sql("INSERT INTO projects(project_id,name,created_at) VALUES('one','One',CURRENT_TIMESTAMP)")
        self.client = self.enterContext(TestClient(create_app(self.store), client=("127.0.0.1", 44000)))

    def test_agent_task_create_update_list_watch_and_cancel(self):
        self.assertEqual(self.client.get("/api/v1/task-capabilities").json()["retention_limit"], MAX_TASKS)
        self.assertGreater(MAX_TASKS, 1000)
        created = self.client.post("/api/v1/projects/one/tasks", json={
            "kind": "evaluation", "title": "评测 lark-cli", "request_key": "eval-1",
            "steps": [{"id": "collect", "label": "读取证据"}, {"id": "score", "label": "评分"}],
        })
        self.assertEqual(created.status_code, 202, created.text)
        task_id = created.json()["task_id"]
        running = self.client.patch(f"/api/v1/projects/one/tasks/{task_id}", json={
            "state": "running", "current_stage": "collect", "progress": 0.5,
            "processed": 1, "total": 2, "message": "证据读取完成",
        })
        self.assertEqual(running.status_code, 200, running.text)
        self.assertEqual(running.json()["revision"], 2)
        self.assertEqual([step["state"] for step in running.json()["steps"]], ["running", "pending"])
        listing = self.client.get("/api/v1/projects/one/tasks?state=running").json()
        self.assertEqual([item["task_id"] for item in listing["items"]], [task_id])
        completed = self.client.patch(f"/api/v1/projects/one/tasks/{task_id}", json={
            "state": "succeeded", "current_stage": "score", "progress": 1,
            "processed": 2, "result": {"score": 0.9}, "message": "评测完成",
        })
        self.assertEqual(completed.json()["state"], "succeeded")
        self.assertEqual([step["state"] for step in completed.json()["steps"]], ["completed", "completed"])
        with self.client.stream("GET", f"/api/v1/projects/one/tasks/{task_id}/events") as response:
            self.assertEqual(response.status_code, 200)
            body = "".join(response.iter_text())
        self.assertIn("event: task", body)
        self.assertIn('"state":"succeeded"', body)

        cancel_created = self.client.post("/api/v1/projects/one/tasks", json={
            "kind": "custom", "title": "待取消", "request_key": "cancel-1"}).json()
        cancelled = self.client.post(f"/api/v1/projects/one/tasks/{cancel_created['task_id']}/cancel")
        self.assertEqual(cancelled.json()["state"], "cancelled")

    def test_adapter_import_appears_in_unified_task_list(self):
        source = {"schema_version": "ATIF-v1.7", "session_id": "s", "agent": {"name": "a"},
                  "steps": [{"step_id": 1, "source": "user", "message": "hello"}]}
        raw = json.dumps(source).encode(); digest = hashlib.sha256(raw).hexdigest()
        root = "/api/v1/projects/one/imports/uploads"
        created = self.client.post(root, json={"request_key": "import-1", "source_format": "ATIF-v1.7",
            "size_bytes": len(raw), "sha256": digest, "part_size": 1048576,
            "binding": {"run_id": "run-1", "query_id": "query-1", "env_id": "env-1"}})
        self.assertEqual(created.status_code, 201, created.text)
        url = root + "/" + created.json()["upload_id"]
        self.assertEqual(self.client.put(url + "/parts/0", content=raw,
            headers={"X-Chunk-SHA256": digest}).status_code, 200)
        response = self.client.post(url + "/complete")
        self.assertEqual(response.status_code, 200, response.text)
        task_id = response.json()["job_id"]
        for _ in range(200):
            task = self.client.get(f"/api/v1/projects/one/tasks/{task_id}").json()
            if task["state"] in ("succeeded", "failed"): break
            time.sleep(0.01)
        self.assertEqual(task["kind"], "adapter_import")
        self.assertEqual(task["state"], "succeeded")
        self.assertEqual(task["progress"], 1)
        self.assertEqual(task["source"]["job_id"], response.json()["job_id"])
        self.assertEqual(task["artifacts"][0]["ref"], {"run_id": "run-1", "revision": 1})
        self.assertEqual({key: task["result"][key] for key in ("job_id", "run_id", "revision", "index_state")},
                         {"job_id": response.json()["job_id"], "run_id": "run-1",
                          "revision": 1, "index_state": "complete"})
        self.assertTrue(task["result"]["content_digest"].startswith("sha256:"))
        self.assertIn("report", task["result"]["import_result"])
        self.assertIn("source_content", task["result"]["import_result"])
        summary = self.client.get("/api/v1/projects/one/tasks").json()["items"][0]
        self.assertIsNone(summary["result"])
        self.assertTrue(all(not step["details"] for step in summary["steps"]))

    def test_registry_marks_active_tasks_failed_after_restart(self):
        root = Path(self.directory) / "persistent-tasks"
        first = TaskRegistry(root)
        task, _ = first.create("one", request_key="restart-1", kind="analysis", title="分析")
        first.update("one", task["task_id"], state="running", progress=0.2)
        restored = TaskRegistry(root).get("one", task["task_id"])
        self.assertEqual(restored["state"], "failed")
        self.assertEqual(restored["error"]["code"], "SERVICE_RESTARTED")

    def test_registry_retry_keeps_completed_shard_checkpoint(self):
        registry = TaskRegistry(Path(self.directory) / "retry-tasks")
        task, _ = registry.create("one", request_key="retry-1", kind="analysis", title="分析",
                                  source={"type": "analysis_batch"}, total=None)
        registry.checkpoint("one", task["task_id"], {"snapshot": "fixed", "shards": {"0": {"content": {}}}})
        registry.update("one", task["task_id"], state="failed", error={"code": "X", "message": "x"})
        retried = registry.retry("one", task["task_id"])
        self.assertEqual(retried["state"], "queued")
        self.assertNotIn("checkpoint", retried["source"])
        self.assertEqual(registry.get_checkpoint("one", task["task_id"])["snapshot"], "fixed")

    def test_task_creation_rejects_duplicate_step_identity(self):
        response = self.client.post("/api/v1/projects/one/tasks", json={
            "kind": "analysis", "title": "bad", "request_key": "bad-steps",
            "steps": [{"id": "same", "label": "A"}, {"id": "same", "label": "B"}],
        })
        self.assertEqual(response.status_code, 422)

    def test_task_create_binds_an_explicit_interactive_session(self):
        session_id = "11111111-1111-4111-8111-111111111111"
        created = self.client.post("/api/v1/projects/one/tasks", json={
            "kind": "evaluation", "title": "Bound evaluation", "request_key": "bound-1",
            "agent_session_id": session_id})
        self.assertEqual(created.status_code, 202, created.text)
        self.assertEqual(created.json()["source"], {"type": "agent", "agent_session_id": session_id,
                                                     "agent_session_kind": "interactive"})
        task_id = created.json()["task_id"]
        self.assertEqual(self.client.get(f"/api/v1/projects/one/tasks/{task_id}").json()["source"],
                         created.json()["source"])
        self.assertEqual(self.client.get("/api/v1/projects/one/tasks").json()["items"][0]["source"],
                         created.json()["source"])
        rejected = self.client.post("/api/v1/projects/one/tasks", json={
            "kind": "evaluation", "title": "Invalid session", "request_key": "bound-2",
            "agent_session_id": "not-a-session"})
        self.assertEqual(rejected.status_code, 422)

    def test_existing_worker_session_binds_as_background(self):
        session = self.client.app.state.agent_sessions.create("owner", "one", "agent-owner")
        created = self.client.post("/api/v1/projects/one/tasks", json={
            "kind": "evaluation", "title": "Agent-created evaluation", "request_key": "worker-eval-1",
            "agent_session_id": session["session_id"]})
        self.assertEqual(created.status_code, 202, created.text)
        self.assertEqual(created.json()["source"]["agent_session_kind"], "background")
        foreign = self.client.app.state.agent_sessions.create("owner", "another", "agent-other")
        rejected = self.client.post("/api/v1/projects/one/tasks", json={
            "kind": "evaluation", "title": "Wrong project", "request_key": "worker-eval-2",
            "agent_session_id": foreign["session_id"]})
        self.assertEqual(rejected.status_code, 422)

    def test_task_list_uses_stable_cursor(self):
        for number in range(3):
            response = self.client.post("/api/v1/projects/one/tasks", json={
                "kind": "custom", "title": f"task-{number}", "request_key": f"page-{number}"})
            self.assertEqual(response.status_code, 202, response.text)
        first = self.client.get("/api/v1/projects/one/tasks?limit=2").json()
        self.assertEqual(len(first["items"]), 2)
        self.assertEqual(first["total"], 3)
        self.assertTrue(first["truncated"])
        self.assertIsNotNone(first["next_cursor"])
        second = self.client.get("/api/v1/projects/one/tasks", params={
            "limit": 2, "after": first["next_cursor"]}).json()
        self.assertEqual(len(second["items"]), 1)
        self.assertFalse(second["truncated"])
        self.assertEqual({item["task_id"] for item in first["items"]}.intersection(
            item["task_id"] for item in second["items"]), set())


if __name__ == "__main__":
    unittest.main()
