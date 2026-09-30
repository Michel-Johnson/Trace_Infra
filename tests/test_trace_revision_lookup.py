"""Lossless trace identity metadata without a side lookup table."""

import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
import uuid

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]
from trace_hunter.content import ContentCorruption, LocalContentStore
from trace_hunter.database import Repository
from trace_hunter.query import TraceQuery
from trace_hunter.query.aggregate import TraceAggregates
from trace_hunter.traces import TraceRevisions
from trace_hunter.traces.lookup import decode, encode


class MetadataAssertions:
    def append(self, run_id, *, model="test-model", harness="test-harness", project="one", previous=0):
        source = copy.deepcopy(self.source)
        source["run"].update(id=run_id, model=model, harness=harness, title="title\x00kept")
        raw = json.dumps(source, ensure_ascii=False).encode()
        return self.revisions.append(project, raw, request_key=uuid.uuid4().hex,
                                     expected_previous=previous)["revision"], raw

    def test_identity_metadata_is_atomic_and_source_remains_immutable(self):
        descriptor, raw = self.append("a", model="left\x00right")
        row = self.repo.rows("SELECT model_hex,title_hex FROM traces WHERE run_id='a'")[0]
        self.assertEqual(decode(row["model_hex"]), "left\x00right")
        self.assertEqual(decode(row["title_hex"]), "title\x00kept")
        self.assertEqual(self.revisions.read("one", "a", 1), raw)
        self.assertEqual(descriptor["metadata"]["model"], "left\x00right")

    def test_nul_literal_escape_and_long_prefix_query_exactly(self):
        values = {"a": "left\x00right", "b": "left\\u0000right"}
        for run_id, model in values.items():
            self.append(run_id, model=model)
        for run_id, model in values.items():
            result = self.query.query("one", {"fields": ["run_id", "model"],
                                               "filters": {"model": [model]}})
            self.assertEqual(result["items"], [{"run_id": run_id, "model": model}])
        prefix = "界" * 4095
        self.append("long-a", harness=prefix + "A")
        self.append("long-b", harness=prefix + "B")
        result = self.query.query("one", {"fields": ["run_id"],
            "filters": {"harness": [prefix + "B"]}})
        self.assertEqual(result["items"], [{"run_id": "long-b"}])

    def test_null_group_is_distinct_from_literal_values(self):
        self.append("literal-null", model="null")
        self.append("literal-unknown", model="unknown")
        document = json.loads((ROOT / "examples/drafts/trace-v2/multiturn-resume.json").read_text())
        self.revisions.append("one", json.dumps(document).encode(), request_key="null-model")
        result = self.query.query("one", {"fields": ["run_id", "model"], "filters": {"model": [None]}})
        self.assertEqual(result["items"], [{"run_id": document["run"]["id"], "model": None}])
        groups = self.aggregate.summarize("one", {"group_by": "model"})["groups"]
        self.assertEqual({row["value"] for row in groups}, {None, "null", "unknown"})

    def test_query_and_aggregate_each_use_one_statement(self):
        self.append("a")
        statements = []
        def observe(connection, cursor, statement, parameters, context, executemany):
            statements.append(statement)
        event.listen(self.repo.engine, "before_cursor_execute", observe)
        try:
            queried = self.query.query("one", {"fields": ["run_id", "model"]})
            self.assertEqual(queried["items"], [{"run_id": "a", "model": "test-model"}])
            self.assertEqual(len(statements), 1)
            statements.clear()
            aggregate = self.aggregate.summarize("one", {"group_by": "model"})
            self.assertEqual(len(statements), 1)
        finally:
            event.remove(self.repo.engine, "before_cursor_execute", observe)
        self.assertEqual(aggregate["totals"]["unindexed"], 1)


class EncodingTests(unittest.TestCase):
    def test_round_trip_and_invalid_encoded_values(self):
        for value in (None, "", "\x00", "left\x00right", "文本🙂", "界" * 4096):
            self.assertEqual(decode(encode(value)), value)
        for value in ("0", "zz", "FF", "00 00", 0, True):
            with self.assertRaises(ContentCorruption):
                decode(value)


class SQLiteTraceMetadataTests(MetadataAssertions, unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        self.repo = Repository(Path(directory.name) / "metadata.sqlite"); self.addCleanup(self.repo.close)
        self.configure(directory.name)

    def configure(self, directory):
        self.content = LocalContentStore(Path(directory) / "content")
        self.revisions = TraceRevisions(self.repo, self.content)
        self.query = TraceQuery(self.repo)
        self.aggregate = TraceAggregates(self.repo)
        self.source = json.loads((ROOT / "examples/minimal.trace.json").read_text())


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "Dedicated PostgreSQL test database required")
class PostgresTraceMetadataTests(MetadataAssertions, unittest.TestCase):
    def setUp(self):
        base = make_url(os.environ["TEST_DATABASE_URL"]).set(drivername="postgresql+psycopg")
        admin = create_engine(base, hide_parameters=True)
        schema = "test_metadata_" + uuid.uuid4().hex
        with admin.begin() as db:
            db.execute(text("CREATE SCHEMA " + schema))
        def cleanup():
            if hasattr(self, "repo"): self.repo.close()
            with admin.begin() as db: db.execute(text("DROP SCHEMA " + schema + " CASCADE"))
            admin.dispose()
        self.addCleanup(cleanup)
        location = base.update_query_dict({"options": "-csearch_path=" + schema}).render_as_string(hide_password=False)
        self.repo = Repository(location); self.repo.migrate()
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        SQLiteTraceMetadataTests.configure(self, directory.name)


if __name__ == "__main__":
    unittest.main()
