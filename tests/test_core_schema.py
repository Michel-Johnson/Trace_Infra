"""The production-minimum schema is an explicit architectural contract."""

import json
from pathlib import Path
import tempfile
import unittest

from trace_hunter.content import LocalContentStore
from trace_hunter.database import Repository
from trace_hunter.traces import TraceRevisions
from trace_hunter.traces.index import TraceIndex


ROOT = Path(__file__).resolve().parents[1]
EXPECTED = {
    "schema_migrations", "projects", "traces", "trace_revisions", "trace_objects",
    "trace_edges", "trace_search_documents", "analysis_results",
    "trace_search_hot_terms", "trace_search_hot_postings",
}


class CoreSchemaTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.repository = Repository(Path(temporary.name) / "core.sqlite")
        self.addCleanup(self.repository.close)
        content = LocalContentStore(Path(temporary.name) / "content")
        self.revisions = TraceRevisions(self.repository, content)
        self.index = TraceIndex(self.revisions)

    def test_sqlite_contains_core_tables_and_derived_hot_search_index(self):
        names = {row["name"] for row in self.repository.rows(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        self.assertEqual(names, EXPECTED)
        indexes = {row["name"] for row in self.repository.rows(
            "SELECT name FROM sqlite_master WHERE type='index'")}
        self.assertIn("trace_objects_source_window", indexes)

    def test_v2_projection_splits_objects_edges_and_search_documents(self):
        raw = (ROOT / "examples/drafts/trace-v2/multiturn-resume.json").read_bytes()
        descriptor = self.revisions.append("core", raw, request_key="one")["revision"]
        status = self.index.project("core", descriptor["run_id"], 1)
        self.assertEqual(status["state"], "complete")
        kinds = {row["object_kind"]: row["n"] for row in self.repository.rows(
            "SELECT object_kind,count(*) AS n FROM trace_objects GROUP BY object_kind")}
        document = json.loads(raw)
        self.assertEqual(kinds["span"], len(document["spans"]))
        self.assertEqual(kinds["message"], len(document["messages"]))
        self.assertEqual(kinds["context"], len(document["contexts"]))
        relations = {row["relation"] for row in self.repository.rows(
            "SELECT DISTINCT relation FROM trace_edges")}
        self.assertIn("context_message", relations)
        self.assertGreater(self.repository.rows(
            "SELECT count(*) AS n FROM trace_search_documents")[0]["n"], 0)
        usage = self.repository.rows("""SELECT requested_model,input_tokens,output_tokens
            FROM trace_objects WHERE kind='model' ORDER BY source_ordinal LIMIT 1""")[0]
        self.assertEqual((usage["requested_model"], usage["input_tokens"], usage["output_tokens"]),
                         ("demo-model", 100, 20))


if __name__ == "__main__":
    unittest.main()
