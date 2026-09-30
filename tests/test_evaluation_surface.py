"""The four evaluation dimensions share a small evidence API surface."""
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from fastapi.testclient import TestClient

from scripts import trace_hunter_cli
from scripts.build_api_contract import make_spec
from trace_hunter.storage import Store
from trace_hunter_api.app import create_app
from tests.test_trace_search import availability_document


RETIRED = [
    ("GET", "/api/v1/object-analysis-capabilities"),
    *[("POST", "/api/v1/projects/one/" + suffix) for suffix in (
        "objects/analyze", "objects/facets", "objects/resolve-runs", "objects/funnel",
        "objects/lineage", "sessions/query", "sessions/timeline", "analysis-batches",
        "analysis-results/query", "spans/analyze-duration")],
    ("GET", "/api/v1/projects/one/analysis-results/agent/1/old"),
    ("GET", "/api/v1/projects/one/runs/fixture/revisions/1/visibility"),
]


class EvaluationSurfaceTests(unittest.TestCase):
    def setUp(self):
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.store = Store(Path(self.directory) / "surface.sqlite")
        self.addCleanup(self.store.close)
        self.app = create_app(self.store)
        self.app.state.projects.create_project("one", "One")
        self.raw = json.dumps(availability_document("fixture")).encode()
        self.store.revisions.append("one", self.raw, request_key="fixture")
        self.app.state.trace_index.project("one", "fixture", 1)
        self.client = self.enterContext(TestClient(self.app, client=("127.0.0.1", 12345)))

    def test_removed_routes_are_not_registered_or_published_and_do_not_mutate_data(self):
        contract = make_spec()
        routes = {route.path for route in self.app.routes}
        for method, target in RETIRED:
            with self.subTest(target=target):
                response = self.client.request(method, target, json={})
                self.assertEqual(response.status_code, 404, response.text)
        for path in routes | set(contract["paths"]):
            self.assertFalse(any(part in path for part in (
                "object-analysis-capabilities", "/objects/analyze", "/objects/facets",
                "/objects/resolve-runs", "/objects/funnel", "/objects/lineage",
                "/sessions/query", "/sessions/timeline", "/analysis-batches",
                "/analysis-results", "/spans/analyze-duration", "/visibility")))
        content = self.client.get("/api/v1/projects/one/traces/fixture/revisions/1/content")
        self.assertEqual(content.content, self.raw)
        self.assertEqual(self.app.state.tasks.list("one")["items"], [])

    def test_discovery_retains_supporting_capabilities_without_retired_analysis(self):
        advanced = self.client.get("/api/v1/advanced-query-capabilities").json()
        self.assertTrue(advanced["evidence_exports"])
        self.assertIn("payload", advanced["fields"])
        for key in ("analysis_batches", "sessions", "lineage_max_nodes"):
            self.assertNotIn(key, advanced)
        self.assertEqual(self.client.get("/api/v1/task-capabilities").json()["retryable_sources"],
                         ["evidence_export", "auto_import"])
        for path in ("/api/skills/manifest", "/api/v1/query-capabilities",
                     "/api/v1/span-query-capabilities", "/api/v1/trace-search-capabilities",
                     "/api/v1/adapter-import-capabilities", "/api/v1/trace-formats"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200, response.text)
        archive = self.client.get("/api/skills/archive")
        self.assertEqual(archive.status_code, 200)
        with zipfile.ZipFile(io.BytesIO(archive.content)) as bundle:
            self.assertTrue(any(name.endswith("/trace-hunter-cli/SKILL.md")
                                for name in bundle.namelist()))

    def test_old_analysis_tasks_remain_readable_but_cannot_restart_removed_computation(self):
        task, _ = self.app.state.tasks.create("one", request_key="old", kind="analysis",
                                             title="Historical analysis", steps=[],
                                             source={"type": "analysis_batch"})
        task_id = task["task_id"]
        self.app.state.tasks.update("one", task_id, state="failed", error="Historical failure")
        url = f"/api/v1/projects/one/tasks/{task_id}"
        before = self.client.get(url).json()
        self.assertEqual(self.client.post(url + "/retry").status_code, 422)
        self.assertEqual(self.client.get(url).json(), before)

    def test_removed_cli_commands_fail_before_network_access(self):
        for name in ("duration", "object", "object-facet", "run-set", "funnel", "results",
                     "result", "lineage", "session-query", "session-timeline", "analysis-batch"):
            with self.subTest(command=name):
                out, err = io.StringIO(), io.StringIO()
                code = trace_hunter_cli.main(
                    ["--url", "http://127.0.0.1:1", "--project", "one", name],
                    environ={}, stdout=out, stderr=err)
                self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
