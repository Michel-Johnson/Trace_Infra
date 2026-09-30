import json
import tempfile
import time
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from trace_hunter.storage import Store
from trace_hunter_api.app import create_app
from tests.test_trace_search import availability_document


class AdvancedAPITests(unittest.TestCase):
    def setUp(self):
        self.temp = self.enterContext(tempfile.TemporaryDirectory())
        self.store = Store(Path(self.temp) / "api.sqlite")
        self.addCleanup(self.store.close)
        self.app = create_app(self.store); self.app.state.projects.create_project("one", "One")
        document = availability_document("advanced-api")
        document["run"]["attributes"] = {"release": "2026.09"}
        document["spans"][0]["attributes"] = {"region": "us"}
        self.store.revisions.append("one", json.dumps(document).encode(), request_key="seed")
        self.app.state.trace_index.project("one", "advanced-api", 1)
        self.client = self.enterContext(TestClient(self.app, client=("127.0.0.1", 44000)))

    def test_query_metrics_and_validation_details(self):
        query = self.client.post("/api/v1/projects/one/objects/query", json={
            "attributes": [{"path": "region", "value": "us"}], "fields": ["run_id", "object_id", "attributes"]})
        self.assertEqual(query.status_code, 200, query.text)
        self.assertEqual(len(query.json()["items"]), 1)
        metrics = self.client.post("/api/v1/projects/one/metrics/query", json={
            "query": {"trace_attributes": [{"path": "release", "value": "2026.09"}]},
            "metrics": ["count", "error_rate"], "group_by": ["kind"]})
        self.assertEqual(metrics.status_code, 200, metrics.text)
        invalid = self.client.post("/api/v1/projects/one/objects/query", json={"limit": 0})
        self.assertEqual(invalid.status_code, 422)
        self.assertEqual(invalid.json()["details"][0]["field"], "body.limit")

    def test_otlp_http_imports_without_running_analysis(self):
        envelope = {"resourceSpans": [{"resource": {"attributes": [
            {"key": "service.name", "value": {"stringValue": "api-worker"}}]},
            "scopeSpans": [{"scope": {"name": "test"}, "spans": [{
                "traceId": "1" * 32, "spanId": "2" * 16, "name": "work",
                "startTimeUnixNano": "1000000000", "endTimeUnixNano": "1100000000"}]}]}]}
        response = self.client.post("/v1/traces", headers={"X-Trace-Project": "one"}, json=envelope)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["X-Trace-Hunter-Accepted-Traces"], "1")
        self.assertFalse(self.store.repository.rows("SELECT * FROM analysis_results WHERE project_id='one'"))

    def test_evidence_export_uses_task_and_single_download(self):
        started = self.client.post("/api/v1/projects/one/evidence-exports", json={
            "request_key": "evidence-1", "query": {"filters": {"run_id": ["advanced-api"]}}})
        self.assertEqual(started.status_code, 202, started.text)
        task_id = started.json()["task_id"]
        for _ in range(100):
            task = self.client.get(f"/api/v1/projects/one/tasks/{task_id}").json()
            if task["state"] in ("succeeded", "failed"): break
            time.sleep(0.01)
        self.assertEqual(task["state"], "succeeded", task)
        content = self.client.get(f"/api/v1/projects/one/evidence-exports/{task_id}/content")
        self.assertEqual(content.status_code, 200, content.text)
        self.assertIn(b'"type":"manifest"', content.content)
        self.assertIn(b'"type":"object"', content.content)



if __name__ == "__main__":
    unittest.main()
