"""Adapter imports expose real stage progress and searchable stored output."""

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

from trace_hunter.import_jobs import ImportJobs
from trace_hunter.storage import Store
from trace_hunter.tasks import TaskRegistry
from trace_hunter.traces.index import TraceIndex
from trace_hunter_api.app import create_app


def source():
    return {
        "schema_version": "ATIF-v1.7",
        "session_id": "source-session",
        "agent": {"name": "fixture", "version": "1", "model_name": "model"},
        "steps": [
            {"step_id": 1, "source": "user", "message": "inspect lark skill"},
            {"step_id": 2, "source": "agent", "message": "reading", "llm_call_count": 0,
             "tool_calls": [{"tool_call_id": "read-1", "function_name": "Read",
                              "arguments": {"file_path": "/skills/lark-cli/SKILL.md"}}],
             "observation": {"results": [{"source_call_id": "read-1", "content": "skill body"}]}}
        ],
    }


class ImportProgressAPITests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = Store(Path(directory.name) / "core.sqlite")
        self.addCleanup(self.store.close)
        with self.store.repository.engine.begin() as db:
            db.exec_driver_sql("INSERT INTO projects(project_id,name,created_at) VALUES('project-one','One',CURRENT_TIMESTAMP)")
        index = self.index = TraceIndex(self.store.revisions)
        app = create_app(self.store)
        self.addCleanup(app.state.uploads.close)
        self.client = TestClient(app, raise_server_exceptions=False, client=("127.0.0.1", 50000))
        self.addCleanup(self.client.close)

    def upload_source(self, *, content, headers):
        root = "/api/v1/projects/project-one/imports/uploads"
        body = {"request_key": headers["Idempotency-Key"],
                "source_format": headers["X-Trace-Source-Format"], "size_bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(), "part_size": 1048576,
                "binding": {key: headers[header] for key, header in (
                    ("run_id", "X-Trace-Run-Id"), ("query_id", "X-Trace-Query-Id"),
                    ("env_id", "X-Trace-Env-Id")) if header in headers}}
        for key, header in (("batch_id", "X-Trace-Batch-Id"), ("source_name", "X-Trace-Source-Name")):
            if header in headers: body[key] = headers[header]
        if "X-Trace-Batch-Total" in headers: body["batch_total"] = int(headers["X-Trace-Batch-Total"])
        created = self.client.post(root, json=body)
        self.assertEqual(created.status_code, 201, created.text)
        url = root + "/" + created.json()["upload_id"]
        part = self.client.put(url + "/parts/0", content=content,
                               headers={"X-Chunk-SHA256": body["sha256"]})
        self.assertEqual(part.status_code, 200, part.text)
        return self.client.post(url + "/complete")

    def import_task(self, job_id):
        for _ in range(200):
            response = self.client.get(f"/api/v1/projects/project-one/tasks/{job_id}")
            self.assertEqual(response.status_code, 200, response.text)
            task = response.json()
            if task["state"] in ("succeeded", "failed", "cancelled"):
                if task["result"]: task["result"] = task["result"]["import_result"]
                return task
            time.sleep(0.01)
        self.fail("Import did not finish")

    def test_completed_batch_survives_service_restart(self):
        root = self.store.content.root / "persistent-tasks"
        tasks = TaskRegistry(root)
        changed_projects = []
        jobs = ImportJobs(self.store, self.index, tasks, changed_projects.append)
        raw = json.dumps(source(), ensure_ascii=False, separators=(",", ":")).encode()
        job, created = jobs.create(
            "project-one", raw, request_key="persistent-import", source_format="ATIF-v1.7",
            binding={"run_id": "persistent-run", "query_id": "q", "env_id": "e"},
            batch_id="persistent-batch", batch_total=1, source_name="one.json")
        self.assertTrue(created)
        jobs.run("project-one", job["job_id"], raw, request_key="persistent-import",
                 source_format="ATIF-v1.7",
                 binding={"run_id": "persistent-run", "query_id": "q", "env_id": "e"})
        self.assertEqual(changed_projects, ["project-one"])
        before = jobs.get_batch("project-one", "persistent-batch")
        restored = ImportJobs(self.store, self.index, TaskRegistry(root)).get_batch(
            "project-one", "persistent-batch")
        self.assertEqual(restored["state"], "completed")
        self.assertEqual(restored["counts"], before["counts"])
        self.assertEqual(restored["items"][0]["source_name"], "one.json")
        restored_job = ImportJobs(self.store, self.index, TaskRegistry(root)).get(
            "project-one", job["job_id"])
        self.assertEqual(restored_job["state"], "succeeded")
        self.assertEqual(restored_job["task_id"], job["job_id"])

    def test_adapter_import_progress_preserves_source_and_builds_search_projection(self):
        capabilities = self.client.get("/api/v1/adapter-import-capabilities")
        self.assertEqual(capabilities.status_code, 200)
        self.assertIn("doubao-turn-export", {
            item["format"] for item in capabilities.json()["adapters"]
        })
        self.assertEqual(len(capabilities.json()["stages"]), 6)
        raw = json.dumps(source(), ensure_ascii=False, separators=(",", ":")).encode()
        response = self.upload_source(
            content=raw,
            headers={
                "Content-Type": "application/json",
                "Idempotency-Key": "fixture-1",
                "X-Trace-Source-Format": "ATIF-v1.7",
                "X-Trace-Run-Id": "run-one",
                "X-Trace-Query-Id": "query-one",
                "X-Trace-Env-Id": "env-one",
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        job_id = response.json()["job_id"]
        job = self.import_task(job_id)
        self.assertEqual(job["state"], "succeeded", job)
        self.assertTrue(all(step["state"] == "completed" for step in job["steps"]))
        self.assertEqual(job["result"]["revision"]["format_version"], "trace-hunter/2.0-draft.2")
        self.assertEqual(job["steps"][-1]["details"]["search_documents"], 8)
        self.assertGreaterEqual(self.store.repository.rows(
            "SELECT count(*) AS n FROM trace_search_documents WHERE text LIKE '%lark-cli%'"
        )[0]["n"], 1)

        summary = self.client.get("/api/v1/projects/project-one/observability").json()
        self.assertEqual(summary["counts"]["traces"], 1)
        self.assertEqual(summary["counts"]["spans"], 1)
        self.assertEqual(summary["projection"]["complete"], 1)
        self.assertEqual(summary["search_backend"], "sqlite_scan_fallback")

    def test_invalid_source_reports_failed_adapter_stage_without_storing_trace(self):
        response = self.upload_source(
            content=b"{}",
            headers={
                "Content-Type": "application/json",
                "Idempotency-Key": "invalid-1",
                "X-Trace-Source-Format": "ATIF-v1.7",
                "X-Trace-Run-Id": "run-invalid",
                "X-Trace-Query-Id": "query-one",
                "X-Trace-Env-Id": "env-one",
            },
        )
        job = self.import_task(response.json()["job_id"])
        self.assertEqual(job["state"], "failed")
        self.assertEqual(job["error"]["code"], "SOURCE_SCHEMA_INVALID")
        self.assertEqual(job["steps"][1]["state"], "failed")
        self.assertEqual(self.store.repository.rows("SELECT count(*) AS n FROM traces")[0]["n"], 0)

    def test_batch_import_reports_aggregate_and_per_file_progress(self):
        for number in (1, 2):
            response = self.upload_source(
                content=json.dumps(source()).encode(),
                headers={
                    "Content-Type": "application/json",
                    "Idempotency-Key": f"batch-item-{number}",
                    "X-Trace-Source-Format": "ATIF-v1.7",
                    "X-Trace-Run-Id": f"batch-run-{number}",
                    "X-Trace-Query-Id": "batch-query",
                    "X-Trace-Env-Id": "batch-env",
                    "X-Trace-Batch-Id": "batch-one",
                    "X-Trace-Batch-Total": "2",
                    "X-Trace-Source-Name": f"trace-{number}.json",
                },
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(self.import_task(response.json()["job_id"])["source"]["batch_id"], "batch-one")

        batch = self.client.get(
            "/api/v1/projects/project-one/import-batches/batch-one?limit=10"
        )
        self.assertEqual(batch.status_code, 200, batch.text)
        value = batch.json()
        self.assertEqual(value["state"], "completed")
        self.assertEqual(value["total"], 2)
        self.assertEqual(value["completed"], 2)
        self.assertEqual(value["counts"], {
            "queued": 0, "running": 0, "succeeded": 2, "failed": 0})
        self.assertEqual(value["progress"], 1.0)
        self.assertEqual({item["source_name"] for item in value["items"]}, {
            "trace-1.json", "trace-2.json"})
        self.assertTrue(all(item["steps"][-1]["state"] == "completed"
                            for item in value["items"]))

        first = self.client.get(
            "/api/v1/projects/project-one/import-batches/batch-one?limit=1"
        ).json()
        self.assertTrue(first["items_truncated"])
        self.assertIsNotNone(first["next_cursor"])
        second = self.client.get(
            "/api/v1/projects/project-one/import-batches/batch-one",
            params={"limit": 1, "after": first["next_cursor"]},
        ).json()
        self.assertEqual(len(second["items"]), 1)
        self.assertNotEqual(first["items"][0]["job_id"], second["items"][0]["job_id"])

    def test_v1_adapter_uses_source_identity_without_binding_headers(self):
        raw = (ROOT / "examples/minimal.trace.json").read_bytes()
        response = self.upload_source(
            content=raw,
            headers={"Content-Type": "application/json", "Idempotency-Key": "v1-1",
                     "X-Trace-Source-Format": "trace-hunter/1.1"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        job = self.import_task(response.json()["job_id"])
        self.assertEqual(job["state"], "succeeded", job)
        self.assertGreater(self.store.repository.rows(
            "SELECT count(*) AS n FROM trace_search_documents WHERE text LIKE '%config.json%'"
        )[0]["n"], 0)


if __name__ == "__main__":
    unittest.main()
