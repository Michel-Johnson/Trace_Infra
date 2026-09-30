"""Query transport shares project authorization and returns only selected fields."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src"), str(ROOT / "apps/api")]
from scripts.build_api_contract import synthetic_inputs
from trace_hunter.storage import Store
from trace_hunter_api.app import create_app


class TraceQueryAPITests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store = Store(Path(folder.name) / "query.sqlite")
        self.addCleanup(self.store.close)
        self.app = create_app(self.store)
        projects = self.app.state.projects
        projects.create_project("one", "One")
        projects.create_project("two", "Two")
        self.client = TestClient(self.app, client=("127.0.0.1", 43000))
        self.addCleanup(self.client.close)
        self.remote = self.client
        self.untrusted = TestClient(self.app, client=("198.51.100.11", 43000))
        self.addCleanup(self.untrusted.close)
        self.read_auth = {}; self.write_auth = {}
        trace, _, _ = synthetic_inputs()
        self.run_id = trace["run"]["id"]
        for project in ("one", "two"):
            self.store.revisions.append(project, json.dumps(trace).encode(), request_key="seed")
            self.app.state.trace_index.project(project, self.run_id, 1)
        self.url = "/api/v1/projects/one/traces/query"

    def test_trusted_operator_queries_projects_and_untrusted_peer_is_rejected(self):
        response = self.remote.post(self.url, json={"fields": ["run_id", "revision"]}, headers=self.read_auth)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["items"], [{"run_id": self.run_id, "revision": 1}])
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(self.remote.post(self.url.replace("/one/", "/two/"), json={}, headers=self.read_auth).status_code, 200)
        self.assertEqual(self.untrusted.post(self.url, json={}).status_code, 403)

    def test_discovery_and_query_do_not_create_work(self):
        response = self.remote.get("/api/v1/query-capabilities", headers=self.read_auth)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(response.json()["triggers_analysis"])
        self.assertEqual(response.json()["max_page_size"], 100)
        self.assertEqual(self.remote.post(self.url, json={}, headers=self.read_auth).status_code, 200)
        self.assertEqual(self.store.repository.rows("SELECT count(*) AS n FROM analysis_results")[0]["n"], 0)

    def test_invalid_fields_limits_and_cursors_are_rejected(self):
        for body in ({"fields": ["payload"]}, {"fields": ["run_id", "run_id"]}, {"limit": True},
                     {"limit": 101}, {"filters": {"model": None}}, {"filters": {"model": []}}, {"sql": "SELECT * FROM runs"}, {"cursor": "forged"}):
            with self.subTest(body=body):
                response = self.remote.post(self.url, json=body, headers=self.read_auth)
                self.assertEqual(response.status_code, 422, response.text)

    def test_unknown_counts_are_explicit_null_in_sparse_response(self):
        trace, _, _ = synthetic_inputs()
        trace["run"]["id"] = "pending"
        self.store.revisions.append("one", json.dumps(trace).encode(), request_key="pending")
        response = self.remote.post(self.url, json={"filters": {"index_state": ["unindexed"]},
            "fields": ["run_id", "record_count", "index_state"]}, headers=self.read_auth)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["items"], [{"run_id": "pending", "record_count": None, "index_state": "unindexed"}])


if __name__ == "__main__":
    unittest.main()
