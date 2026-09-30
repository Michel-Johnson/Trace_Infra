"""Real PostgreSQL migration and immutable-import smoke tests."""

import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from scripts.build_api_contract import synthetic_inputs
from trace_hunter.content import LocalContentStore
from trace_hunter.database import Conflict
from trace_hunter.storage import Store
from trace_hunter.traces.index import TraceIndex
from trace_hunter.query.advanced import AdvancedQuery
from tests.test_trace_search import availability_document


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_TABLES = {
    "schema_migrations", "projects", "traces", "trace_revisions", "trace_objects",
    "trace_edges", "trace_search_documents", "analysis_results",
    "trace_search_hot_terms", "trace_search_hot_postings",
}


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"),
                     "Set TEST_DATABASE_URL to a dedicated PostgreSQL test database")
class PostgresTests(unittest.TestCase):
    def setUp(self):
        base = make_url(os.environ["TEST_DATABASE_URL"]).set(drivername="postgresql+psycopg")
        self.admin = create_engine(base, hide_parameters=True)
        self.schema = "test_core_" + uuid.uuid4().hex
        with self.admin.begin() as db:
            db.execute(text("CREATE SCHEMA " + self.schema))
        url = base.update_query_dict({"options": "-csearch_path=" + self.schema})
        self.content_directory = tempfile.TemporaryDirectory()
        self.store = Store(url.render_as_string(hide_password=False),
                           content_store=LocalContentStore(self.content_directory.name))
        self.store.repository.migrate()
        self.first, _, _ = synthetic_inputs()
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.store.close()
        self.content_directory.cleanup()
        with self.admin.begin() as db:
            db.execute(text("DROP SCHEMA " + self.schema + " CASCADE"))
        self.admin.dispose()

    def test_schema_is_exact_and_migrations_are_idempotent(self):
        self.store.repository.migrate()
        names = {row["table_name"] for row in self.store.repository.rows("""
            SELECT table_name FROM information_schema.tables
            WHERE table_schema=current_schema() AND table_type='BASE TABLE'""")}
        self.assertEqual(names, EXPECTED_TABLES)
        count = self.store.repository.rows("SELECT count(*) AS n FROM schema_migrations")[0]["n"]
        self.assertEqual(count, len(list((ROOT / "db/migrations").glob("*.sql"))))
        indexes = {row["indexname"] for row in self.store.repository.rows(
            "SELECT indexname FROM pg_indexes WHERE schemaname=current_schema()")}
        self.assertIn("trace_objects_source_window", indexes)
        self.assertTrue({"trace_objects_attributes", "trace_objects_absolute_time",
                         "trace_objects_session", "trace_edges_reverse_lookup"}.issubset(indexes))
        columns = {(row["table_name"], row["column_name"]): row["data_type"] for row in self.store.repository.rows(
            "SELECT table_name,column_name,data_type FROM information_schema.columns WHERE table_schema=current_schema()")}
        self.assertEqual(columns[("trace_objects", "attributes")], "jsonb")
        self.assertEqual(columns[("trace_objects", "start_at")], "timestamp with time zone")

    def test_concurrent_import_is_idempotent_and_content_is_immutable(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.store.import_trace(self.first), range(16)))
        self.assertEqual(sum(result["created"] for result in results), 1)
        self.assertEqual(self.store.repository.rows(
            "SELECT count(*) AS n FROM trace_revisions")[0]["n"], 1)
        changed = copy.deepcopy(self.first)
        changed["run"]["title"] = "changed"
        with self.assertRaises(Conflict):
            self.store.import_trace(changed)
        self.assertEqual(self.store.get(self.first["run"]["id"])["trace"], self.first)

    def test_advanced_query_uses_projection_four_and_time_index(self):
        document = availability_document("postgres-advanced")
        document["run"]["attributes"] = {"release": "2026.09"}
        document["clocks"][0].update(kind="wall", origin_at="2026-09-21T10:00:00+00:00")
        document["spans"][0]["attributes"] = {"region": "us"}
        self.store.revisions.append("p1", json.dumps(document).encode(), request_key="pg-advanced")
        TraceIndex(self.store.revisions).project("p1", "postgres-advanced", 1)
        result = AdvancedQuery(self.store.repository).query("p1", {
            "attributes": [{"path": "region", "value": "us"}],
            "time": {"from": "2026-09-21T10:00:00Z"}})
        self.assertEqual(len(result["items"]), 1)
        with self.store.repository.engine.begin() as db:
            db.execute(text("SET LOCAL enable_seqscan=off"))
            plan = "\n".join(row[0] for row in db.execute(text("EXPLAIN SELECT * FROM trace_objects "
                "WHERE project_id='p1' AND projector_version='trace-index/4' "
                "AND start_at>='2026-09-21T10:00:00Z'::timestamptz")))
            attribute_plan = "\n".join(row[0] for row in db.execute(text("EXPLAIN SELECT * FROM trace_objects "
                "WHERE attributes @> '{\"region\":\"us\"}'::jsonb")))
        self.assertIn("trace_objects_absolute_time", plan)
        self.assertIn("trace_objects_attributes", attribute_plan)


if __name__ == "__main__":
    unittest.main()
