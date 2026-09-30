"""Server query cache survives browser refreshes and follows project data versions."""

import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import text

from scripts.build_api_contract import synthetic_inputs
from trace_hunter.storage import Store
from trace_hunter_api.app import create_app
from trace_hunter_api.query_cache import ServerQueryCache


class ServerQueryCacheTests(unittest.TestCase):
    def setUp(self):
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.store = Store(Path(self.directory) / "server-cache.sqlite")
        self.addCleanup(self.store.close)
        self.app = create_app(self.store)
        self.app.state.projects.create_project("one", "One")
        self.app.state.projects.create_project("two", "Two")
        trace, _, _ = synthetic_inputs()
        self.trace = trace
        self._append("one", trace, "seed-one")
        self.client = self.enterContext(TestClient(self.app, client=("127.0.0.1", 47000)))
        self.url = "/api/v1/projects/one/traces/query"

    def _append(self, project, trace, key):
        result = self.store.revisions.append(project, json.dumps(trace).encode(), request_key=key)
        self.app.state.trace_index.project(project, trace["run"]["id"], result["revision"]["revision"])

    def test_repeat_query_and_new_browser_session_hit_server_cache(self):
        body = {"fields": ["run_id", "revision"]}
        first = self.client.post(self.url, json=body)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.headers["x-trace-hunter-cache"], "MISS")
        second = self.client.post(self.url, json=body)
        self.assertEqual(second.headers["x-trace-hunter-cache"], "HIT")
        self.assertEqual(second.headers["x-trace-hunter-data-version"],
                         first.headers["x-trace-hunter-data-version"])
        with TestClient(self.app, client=("127.0.0.1", 47001)) as refreshed_browser:
            refreshed = refreshed_browser.post(self.url, json=body)
        self.assertEqual(refreshed.headers["x-trace-hunter-cache"], "HIT")
        self.assertEqual(refreshed.json(), first.json())

    def test_source_and_projection_change_invalidates_project_cache(self):
        body = {"fields": ["run_id", "revision"]}
        first = self.client.post(self.url, json=body)
        self.assertEqual(self.client.post(self.url, json=body).headers["x-trace-hunter-cache"], "HIT")
        trace = json.loads(json.dumps(self.trace))
        trace["run"]["id"] = "server-cache-new-run"
        self._append("one", trace, "seed-new")
        self.app.state.query_cache.invalidate_project("one")
        changed = self.client.post(self.url, json=body)
        self.assertEqual(changed.headers["x-trace-hunter-cache"], "MISS")
        self.assertNotEqual(changed.headers["x-trace-hunter-data-version"],
                            first.headers["x-trace-hunter-data-version"])
        self.assertEqual(len(changed.json()["items"]), 2)

    def test_project_body_and_explicit_bypass_are_isolated(self):
        base = self.client.post(self.url, json={})
        self.assertEqual(base.headers["x-trace-hunter-cache"], "MISS")
        different = self.client.post(self.url, json={"limit": 1})
        self.assertEqual(different.headers["x-trace-hunter-cache"], "MISS")
        other = self.client.post(self.url.replace("/one/", "/two/"), json={})
        self.assertEqual(other.headers["x-trace-hunter-cache"], "MISS")
        bypass = self.client.post(self.url, json={}, headers={"Cache-Control": "no-cache"})
        self.assertEqual(bypass.headers["x-trace-hunter-cache"], "BYPASS")

    def test_projection_failure_also_changes_data_version(self):
        before = self.app.state.query_cache.data_version("one")
        with self.store.repository.engine.begin() as connection:
            connection.execute(text(
                "UPDATE trace_revisions SET projection_state='failed' "
                "WHERE project_id=:project AND run_id=:run"),
                {"project": "one", "run": self.trace["run"]["id"]})
        self.app.state.query_cache.invalidate_project("one")
        self.assertNotEqual(self.app.state.query_cache.data_version("one"), before)

    def test_cache_is_bounded_by_entry_count_and_bytes(self):
        cache = ServerQueryCache(self.store.repository, ttl_seconds=60, max_entries=2,
                                 max_bytes=8, max_item_bytes=8)
        self.assertTrue(cache.put("a", b"1234", "application/json"))
        self.assertTrue(cache.put("b", b"5678", "application/json"))
        self.assertTrue(cache.put("c", b"9", "application/json"))
        self.assertIsNone(cache.get("a"))
        self.assertEqual(cache.status()["entries"], 2)
        self.assertFalse(cache.put("large", b"123456789", "application/json"))


if __name__ == "__main__":
    unittest.main()
