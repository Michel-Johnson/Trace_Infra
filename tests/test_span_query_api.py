"""Span HTTP routes preserve authorization, sparse results and evidence."""
import copy
import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from trace_hunter.storage import Store
from trace_hunter_api.app import create_app

ROOT = Path(__file__).resolve().parents[1]


class SpanQueryAPITests(unittest.TestCase):
    def setUp(self):
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.store = Store(Path(self.directory) / "span-api.sqlite")
        self.addCleanup(self.store.close)
        self.app = create_app(self.store)
        projects = self.app.state.projects
        projects.create_project("one", "One"); projects.create_project("two", "Two")
        self.auth = {}
        self.client = self.enterContext(TestClient(self.app, client=("127.0.0.1", 44000)))
        template = json.loads((ROOT / "examples/drafts/trace-v2/multiturn-resume.json").read_text())
        for index in range(6):
            document = copy.deepcopy(template)
            document["run"]["id"] = f"api-duration-{index}"
            span = next(item for item in document["spans"] if item["id"] == "read-1")
            span["tool"]["skill"] = {"name": "lark-cli", "action": "invoke"}
            duration = 9000 if index == 5 else 100 + index
            span["timing"].update(start_ms=1000, end_ms=1000 + duration)
            self.store.revisions.append("one", json.dumps(document).encode(), request_key=f"seed-{index}")
            self.app.state.trace_index.project("one", document["run"]["id"], 1)

    def test_reader_queries_skill_spans_and_duration_order(self):
        response = self.client.post("/api/v1/projects/one/spans/query", headers=self.auth, json={
            "filters": {"skill_name": ["lark-cli"], "min_duration_ms": 1000},
            "fields": ["run_id", "revision", "span_id", "skill_name", "duration_ms", "source_refs"],
            "order": "duration_desc",
        })
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["items"], [{
            "run_id": "api-duration-5", "revision": 1, "span_id": "read-1",
            "skill_name": "lark-cli", "duration_ms": 9000.0,
            "source_refs": [{"source_id": "fixture", "pointer": ""}],
        }])
        self.assertEqual(response.headers["cache-control"], "no-store")

    def test_span_window_route_returns_exact_revision_neighborhood(self):
        response = self.client.post("/api/v1/projects/one/spans/window", headers=self.auth, json={
            "anchor": {"run_id": "api-duration-0", "revision": 1, "span_id": "read-1"},
            "before": 1, "after": 1, "include": ["documents", "related_objects", "edges"],
        })
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result["spans"][1]["span_id"], "read-1")
        self.assertEqual(result["ordering"], {"basis": "source_ordinal", "causal": False})
        self.assertTrue(any(item["span_id"] == "read-1" for item in result["documents"]))
        missing = self.client.post("/api/v1/projects/one/spans/window", headers=self.auth, json={
            "anchor": {"run_id": "api-duration-0", "revision": 1, "span_id": "absent"}})
        self.assertEqual(missing.status_code, 404, missing.text)

    def test_routes_require_project_reader_and_reject_body_search(self):
        capability = self.client.get("/api/v1/span-query-capabilities", headers=self.auth)
        self.assertEqual(capability.status_code, 200, capability.text)
        self.assertFalse(capability.json()["source_content"])
        url = "/api/v1/projects/one/spans/query"
        self.assertEqual(self.client.post(url.replace("/one/", "/two/"), headers=self.auth, json={}).status_code, 200)
        self.assertEqual(self.client.post(url, headers=self.auth, json={"fields": ["input"]}).status_code, 422)
        self.assertEqual(self.store.repository.rows("SELECT count(*) AS n FROM analysis_results")[0]["n"], 0)


if __name__ == "__main__":
    unittest.main()
