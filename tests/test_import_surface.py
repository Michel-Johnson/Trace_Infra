"""Import consolidation keeps uploads deterministic and removed routes unavailable."""
import hashlib
import json
import tempfile
import time
import unittest
from pathlib import Path

from fastapi.testclient import TestClient
from scripts.build_api_contract import make_spec
from trace_hunter.storage import Store
from trace_hunter_api.app import create_app

RETIRED = [
    ("POST", "/api/v1/projects/one/imports"),
    ("GET", "/api/v1/projects/one/imports/old-job"),
    ("POST", "/api/v1/projects/one/imports/uploads/old-upload/retry"),
    ("POST", "/api/v1/projects/one/imports/uploads/old-upload/cancel"),
]


class ImportSurfaceTests(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.store = Store(root / "db.sqlite")
        self.addCleanup(self.store.close)
        self.app = create_app(self.store)
        self.app.state.projects.create_project("one", "One")
        self.app.state.projects.create_project("two", "Two")
        self.addCleanup(self.app.state.uploads.close)
        self.client = self.enterContext(TestClient(self.app, client=("127.0.0.1", 49000)))

    def test_four_retired_routes_are_absent_without_mutation(self):
        for method, path in RETIRED:
            response = self.client.request(method, path, json={})
            self.assertEqual(response.status_code, 404, response.text)
        spec = make_spec()
        for path in ("/api/v1/projects/{project_id}/imports",
                     "/api/v1/projects/{project_id}/imports/{job_id}",
                     "/api/v1/projects/{project_id}/imports/uploads/{upload_id}/retry",
                     "/api/v1/projects/{project_id}/imports/uploads/{upload_id}/cancel"):
            self.assertNotIn(path, spec["paths"])
        self.assertEqual(sum(m in ("get", "post", "patch", "put", "delete")
                             for v in spec["paths"].values() for m in v), 78)
        self.assertEqual(self.app.state.tasks.list("one")["items"], [])

    def test_named_adapter_upload_is_idempotent_and_does_not_start_agent(self):
        raw = (Path(__file__).resolve().parents[1] / "examples/minimal.trace.json").read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        root = "/api/v1/projects/one/imports/uploads"
        body = {"request_key": "named-adapter", "source_format": "trace-hunter/1.1",
                "size_bytes": len(raw), "sha256": digest, "part_size": 1048576}
        created = self.client.post(root, json=body)
        self.assertEqual(created.status_code, 201, created.text)
        url = root + "/" + created.json()["upload_id"]
        self.assertEqual(self.client.post(url + "/complete").status_code, 409)
        self.assertEqual(self.client.get(url.replace("/one/", "/two/")).status_code, 404)
        bad = self.client.put(url + "/parts/0", content=raw, headers={"X-Chunk-SHA256": "0" * 64})
        self.assertEqual(bad.status_code, 422, bad.text)
        self.assertEqual(self.client.get(url).json()["received_parts"], [])
        self.assertEqual(self.client.put(url + "/parts/0", content=raw,
            headers={"X-Chunk-SHA256": digest}).status_code, 200)
        complete = self.client.post(url + "/complete")
        self.assertEqual(complete.status_code, 200, complete.text)
        task_id = complete.json()["job_id"]
        self.assertIsNone(complete.json()["agent_session_id"])
        for _ in range(200):
            task = self.client.get(f"/api/v1/projects/one/tasks/{task_id}").json()
            if task["state"] in ("succeeded", "failed"): break
            time.sleep(0.01)
        self.assertEqual(task["state"], "succeeded", task)
        self.assertEqual(task["source"]["source_format"], "trace-hunter/1.1")
        self.assertEqual(task["result"]["import_result"]["source_content"]["digest"], "sha256:" + digest)
        self.assertEqual(self.client.post(url + "/complete").json()["job_id"], task_id)
        self.assertEqual(self.client.post(root, json=body).json()["upload_id"], created.json()["upload_id"])
        self.assertEqual(len(self.app.state.tasks.list("one")["items"]), 1)
        self.assertEqual(self.store.repository.rows("SELECT count(*) AS n FROM trace_revisions")[0]["n"], 1)


if __name__ == "__main__":
    unittest.main()
