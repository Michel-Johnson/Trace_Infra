"""Search HTTP contract preserves project and visibility boundaries."""
import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from trace_hunter.content import LocalContentStore
from trace_hunter.storage import Store
from trace_hunter_api.app import create_app
from tests.test_trace_search import availability_document


class TraceSearchAPITests(unittest.TestCase):
    def setUp(self):
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.store = Store(Path(self.directory) / "search-api.sqlite")
        self.addCleanup(self.store.close)
        self.app = create_app(self.store)
        projects = self.app.state.projects
        projects.create_project("one", "One"); projects.create_project("two", "Two")
        self.auth = {}
        self.client = self.enterContext(TestClient(self.app, client=("127.0.0.1", 44000)))
        document = availability_document("api-search")
        self.store.revisions.append("one", json.dumps(document).encode(), request_key="search-seed")
        self.app.state.trace_index.project("one", "api-search", 1)

    def test_reader_searches_with_source_evidence(self):
        response = self.client.post("/api/v1/projects/one/search", headers=self.auth,
                                    json={"query": "库存", "scope": "analysis"})
        self.assertEqual(response.status_code, 200, response.text)
        item = response.json()["items"][0]
        self.assertGreaterEqual(response.json()["total_count"], len(response.json()["items"]))
        self.assertEqual(response.json()["matched_trace_count"], 1)
        self.assertIn("库存", item["snippet"])
        self.assertTrue(item["match_ranges"])
        self.assertIsInstance(item["score"], float)
    def test_scope_and_project_are_mandatory(self):
        url = "/api/v1/projects/one/search"
        self.assertEqual(self.client.post(url, headers=self.auth, json={"query": "库存"}).status_code, 422)
        self.assertEqual(self.client.post(url, headers=self.auth, json={
            "query": "库存", "mode": "fuzzy", "scope": "analysis"}).status_code, 422)
        self.assertEqual(self.client.post(url.replace("/one/", "/two/"), headers=self.auth,
                                          json={"query": "库存", "scope": "analysis"}).status_code, 200)
        capabilities = self.client.get("/api/v1/trace-search-capabilities", headers=self.auth)
        self.assertEqual(capabilities.status_code, 200)
        self.assertEqual(capabilities.json()["modes"], ["literal", "regex"])
        self.assertEqual(capabilities.json()["regex_dialect"], "postgresql_are")
        self.assertIn("portable", capabilities.json()["regex_syntaxes"])

    def test_regex_compatibility_is_explicit_and_returns_effective_pattern(self):
        base = "/api/v1/projects/one/search"
        document = json.loads((Path(__file__).resolve().parents[1] / "examples/minimal.trace.json").read_text())
        document["run"]["id"] = "regex-api"
        document["spans"][0]["input"] = "tool failed"
        self.store.revisions.append("one", json.dumps(document).encode(), request_key="regex-api")
        self.app.state.trace_index.project("one", "regex-api", 1)
        result = self.client.post(base, json={"query": r"\btool\b", "mode": "regex",
                                               "regex_syntax": "portable", "scope": "analysis"})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()["effective_pattern"], r"\ytool\y")
        self.assertTrue(result.json()["items"])
        self.assertTrue(result.json()["items"][0]["match_ranges"])
        invalid = self.client.post(base, json={"query": "[", "mode": "regex", "scope": "analysis"})
        self.assertEqual(invalid.status_code, 422, invalid.text)

    def test_model_context_scope_remains_visibility_bounded(self):
        context = self.client.post("/api/v1/projects/one/search", json={
            "query": "库存", "scope": "model_context",
            "visible_to": {"run_id": "api-search", "revision": 1, "model_span_id": "model-1"},
        })
        self.assertEqual(context.status_code, 200, context.text)

    def test_cli_term_endpoints_and_evaluation_observation(self):
        base = "/api/v1/projects/one/search"
        body = {"query": "供应商", "scope": "analysis", "mode": "literal", "purpose": "evaluation", "limit": 1}
        first = self.client.post(base, json=body)
        self.assertEqual(first.status_code, 200, first.text)
        second = self.client.post(base, json=body)
        self.assertEqual(second.status_code, 200, second.text)
        listing = self.client.get(base + "/terms")
        self.assertEqual(listing.status_code, 200, listing.text)
        term = next(item for item in listing.json()["items"] if item["term"] == "供应商")
        self.assertEqual(term["search_count"], 2)
        self.assertFalse(term["active"])
        if first.json()["next_cursor"]:
            page = self.client.post(base, json={**body, "cursor": first.json()["next_cursor"]})
            self.assertEqual(page.status_code, 200, page.text)
            self.assertEqual(self.client.get(base + "/terms").json()["items"][0]["search_count"], 2)
        promoted = self.client.post(base + "/terms/promote", json={"term": "供应商"})
        self.assertEqual(promoted.status_code, 200, promoted.text)
        self.assertTrue(promoted.json()["active"])
        self.assertEqual(promoted.json()["posting_count"], first.json()["total_count"])
        self.assertEqual(self.client.post(base, json=body).json()["total_count"], first.json()["total_count"])
        demoted = self.client.post(base + "/terms/demote", json={"term": "供应商"})
        self.assertEqual(demoted.status_code, 200, demoted.text)
        self.assertFalse(demoted.json()["active"])
        short = self.client.post(base + "/terms/promote", json={"term": "库存"})
        self.assertEqual(short.status_code, 200, short.text)
        self.assertGreater(short.json()["posting_count"], 0)
        self.assertEqual(self.client.get("/api/v1/projects/two/search/terms").json()["items"], [])


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "Dedicated PostgreSQL test database required")
class PostgresTraceSearchAPITests(TraceSearchAPITests):
    def setUp(self):
        base = make_url(os.environ["TEST_DATABASE_URL"]).set(drivername="postgresql+psycopg")
        admin = create_engine(base, hide_parameters=True)
        schema = "test_search_api_" + uuid.uuid4().hex
        with admin.begin() as db:
            db.execute(text("CREATE SCHEMA " + schema))
        directory = self.enterContext(tempfile.TemporaryDirectory())
        url = base.update_query_dict({"options": "-csearch_path=" + schema})
        self.store = Store(url.render_as_string(hide_password=False),
                           content_store=LocalContentStore(Path(directory) / "content"))
        self.store.repository.migrate()
        def cleanup():
            self.store.close()
            with admin.begin() as db:
                db.execute(text("DROP SCHEMA " + schema + " CASCADE"))
            admin.dispose()
        self.addCleanup(cleanup)
        self.app = create_app(self.store)
        projects = self.app.state.projects
        projects.create_project("one", "One"); projects.create_project("two", "Two")
        self.auth = {}
        self.client = self.enterContext(TestClient(self.app, client=("127.0.0.1", 44000)))
        document = availability_document("api-search")
        self.store.revisions.append("one", json.dumps(document).encode(), request_key="search-seed")
        self.app.state.trace_index.project("one", "api-search", 1)


if __name__ == "__main__":
    unittest.main()
