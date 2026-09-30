"""Source-only, rebuildable projections on SQLite and isolated PostgreSQL schemas."""

import copy
import json
import os
import sys
import tempfile
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]
from trace_hunter.content import ContentRef, LocalContentStore
from trace_hunter.database import Repository
from trace_hunter.traces import TraceRevisions
from trace_hunter.traces.index import TraceIndex


class IndexAssertions:
    def append(self, document=None, *, project="p1", previous=0):
        document = self.document if document is None else document
        raw = json.dumps(document, ensure_ascii=False).encode()
        self.revisions.append(project, raw, request_key=uuid.uuid4().hex, expected_previous=previous)
        return document["run"]["id"]

    def records(self, run_id=None, revision=1, project="p1", projector=None):
        return [dict(row) for row in self.repo.rows("""
            SELECT * FROM trace_objects WHERE project_id=:project AND run_id=:run
                AND revision=:revision AND projector_version=:version AND object_kind='span'
                ORDER BY source_ordinal
        """, {"project": project, "run": run_id or self.run_id, "revision": revision,
              "version": projector or self.index.projector_version})]

    def test_status_is_read_only_and_unindexed_counts_are_unknown(self):
        self.append()
        with patch.object(self.content, "open_verified", side_effect=AssertionError("must not read body")):
            status = self.index.status("p1", self.run_id, 1)
        self.assertEqual(status["state"], "unindexed")
        self.assertTrue(all(value is None for value in status["counts"].values()))
        self.assertEqual(self.repo.rows("SELECT projection_state FROM trace_revisions")[0]["projection_state"], "unindexed")
        for revision in (None, True, 0):
            with self.assertRaises(ValueError):
                self.index.status("p1", self.run_id, revision)
        with self.assertRaises(KeyError):
            self.index.status("other-project", self.run_id, 1)

    def test_source_records_are_indexed_idempotently_without_bodies_or_analysis(self):
        self.document["spans"][1]["input"] = {"private-body": "MUST_NOT_ENTER_INDEX"}
        for position, span in enumerate(self.document["spans"]):
            span["sequence"] = 100 - position
        self.append()
        raw = self.revisions.read("p1", self.run_id, 1)
        first = self.index.project("p1", self.run_id, 1)
        self.assertEqual(first, self.index.project("p1", self.run_id, 1))
        self.assertEqual(first["state"], "complete")
        self.assertEqual(first["counts"], {"records": 4, "models": 2, "model_batches": 0,
            "tools": 2, "agents": 0, "waits": 0, "other": 0, "unknown": 0})
        records = self.records()
        self.assertEqual([r["span_id"] for r in records], [s["id"] for s in self.document["spans"]])
        self.assertEqual([r["source_ordinal"] for r in records], [0, 1, 2, 3])
        self.assertEqual([r["source_sequence"] for r in records], [100, 99, 98, 97])
        self.assertNotIn("MUST_NOT_ENTER_INDEX", json.dumps({"status": first, "records": records}))
        self.assertNotIn("input", records[0])
        self.assertNotIn("output", records[0])
        self.assertEqual(raw, self.revisions.read("p1", self.run_id, 1))
        self.assertIsNone(records[0]["duration_ms"])
        self.assertEqual(records[0]["start_ms"], 0)
        self.assertIsNone(records[0]["clock_id"])
        self.assertEqual(self.repo.rows("SELECT count(*) AS n FROM analysis_results")[0]["n"], 0)

    def test_rebuild_uses_original_content_after_projection_damage(self):
        self.append()
        first = self.index.project("p1", self.run_id, 1)
        records = self.records()
        with self.repo.engine.begin() as db:
            db.execute(text("DELETE FROM trace_objects WHERE source_ordinal=1"))
        self.assertEqual(self.index.project("p1", self.run_id, 1), first)
        self.assertEqual(self.records(), records)

    def test_nul_in_body_preserves_source_and_degrades_search_projection(self):
        self.document["spans"][0]["input"] = "before\x00after"
        self.append()
        self.assertEqual(self.index.project("p1", self.run_id, 1)["state"], "complete")
        row = self.repo.rows("""
            SELECT text,text_state FROM trace_search_documents
            WHERE project_id='p1' AND run_id=:run AND field='input'
        """, {"run": self.run_id})[0]
        self.assertEqual(row["text"], "before\ufffdafter")
        self.assertEqual(row["text_state"], "truncated")
        source = json.loads(self.revisions.read("p1", self.run_id, 1))
        self.assertEqual(source["spans"][0]["input"], "before\x00after")

    def test_corrupt_or_missing_content_is_failed_and_retry_rebuilds(self):
        self.append()
        ref = ContentRef(**self.revisions.get("p1", self.run_id, 1)["content"])
        path = self.content._path(ref)
        original = path.read_bytes()
        for expected in ("content_integrity_failed", "content_missing"):
            with self.subTest(error=expected):
                self.assertEqual(self.index.project("p1", self.run_id, 1)["state"], "complete")
                if expected == "content_missing":
                    path.unlink()
                else:
                    path.write_bytes(b"private corrupt payload")
                failed = self.index.project("p1", self.run_id, 1)
                self.assertEqual(failed["state"], "failed")
                self.assertEqual(failed["error_code"], expected)
                self.assertIsNone(failed["counts"]["records"])
                self.assertEqual(self.records(), [])
                self.assertNotIn(str(path), json.dumps(failed))
                self.assertNotIn("private corrupt payload", json.dumps(failed))
                path.write_bytes(original)
                self.assertEqual(self.index.project("p1", self.run_id, 1)["counts"]["records"], 4)

    def test_v2_clock_domains_zero_unknown_and_capture_coverage_remain_distinct(self):
        document = json.loads((ROOT / "examples/drafts/trace-v2/multiturn-resume.json").read_text())
        unknown = next(span for span in document["spans"] if span["id"] == "unknown-tool")
        unknown["timing"] = {"clock_id": None, "start_ms": None, "end_ms": None,
            "duration_ms": 0, "duration_basis": "source_reported", "duration_scope": "active"}
        run_id = self.append(document)
        status = self.index.project("p1", run_id, 1)
        records = self.records(run_id)
        self.assertEqual(status["coverage"]["index"], "complete")
        self.assertEqual(status["coverage"]["capture"]["tools"], "partial")
        self.assertEqual(status["counts"]["records"], 9)
        self.assertEqual(status["counts"]["models"], 4)
        self.assertEqual(status["counts"]["tools"], 4)
        self.assertEqual(status["counts"]["waits"], 1)
        self.assertEqual(status["counts"]["unknown"], sum(s["status"] == "unknown" for s in document["spans"]))
        self.assertNotIn("turns", status["counts"])
        self.assertNotIn("wall_ms", status)
        self.assertEqual({r["clock_id"] for r in records}, {None, "clock-1", "clock-2"})
        self.assertEqual(records[0]["clock_uncertainty_ms"], 0)
        unknown_row = next(r for r in records if r["span_id"] == "unknown-tool")
        self.assertEqual(unknown_row["duration_ms"], 0)
        self.assertIsNone(unknown_row["start_ms"])
        self.assertIsNone(unknown_row["clock_id"])
        self.assertEqual([r["turn_id"] for r in records], [s.get("turn_id") for s in document["spans"]])

    def test_legacy_identity_and_opaque_batch_are_not_fabricated(self):
        legacy = copy.deepcopy(self.document)
        legacy["schema_version"] = "trace-hunter/1.0"
        legacy["run"]["task_key"] = legacy["run"].pop("query_id")
        legacy["run"].pop("env_id")
        legacy.pop("environment")
        run_id = self.append(legacy)
        status = self.index.project("p1", run_id, 1)
        self.assertEqual(status["identity_basis"], "legacy_compatibility")
        self.assertEqual(status["run"]["query_id"], legacy["run"]["task_key"])
        self.assertEqual(status["coverage"]["capture"]["messages"], "unknown")
        batch = json.loads((ROOT / "examples/drafts/trace-v2/opaque-batch.json").read_text())
        run_id = self.append(batch)
        status = self.index.project("p1", run_id, 1)
        self.assertEqual(status["counts"]["model_batches"], 1)
        self.assertEqual(status["counts"]["models"], 0)
        self.assertIsNone(status["run"]["model"])

    def test_old_revisions_projects_and_projector_versions_are_not_overwritten(self):
        self.append()
        first = self.index.project("p1", self.run_id, 1)
        changed = copy.deepcopy(self.document)
        changed["run"]["title"] = "second revision"
        self.append(changed, previous=1)
        second = self.index.project("p1", self.run_id, 2)
        self.assertNotEqual(second["content_digest"], first["content_digest"])
        self.assertEqual(first, self.index.status("p1", self.run_id, 1))
        self.append(changed, project="p2")
        self.index.project("p2", self.run_id, 1)
        self.assertEqual(len(self.records()), 4)
        self.assertEqual(len(self.records(revision=2)), 4)
        self.assertEqual(len(self.records(project="p2")), 4)

    def test_failed_record_write_has_no_partial_projection_and_can_retry(self):
        self.append()
        self.index.project("p1", self.run_id, 1)
        fired = []
        def break_after_insert(connection, cursor, statement, parameters, context, executemany):
            if statement.startswith("INSERT INTO trace_objects"):
                fired.append(True)
                raise RuntimeError("private backend detail must not be stored")
        event.listen(self.repo.engine, "after_cursor_execute", break_after_insert)
        try:
            failed = self.index.project("p1", self.run_id, 1)
        finally:
            event.remove(self.repo.engine, "after_cursor_execute", break_after_insert)
        self.assertTrue(fired)
        self.assertEqual(failed["error_code"], "index_write_failed")
        self.assertEqual(failed["state"], "failed")
        self.assertEqual(failed, self.index.status("p1", self.run_id, 1))
        self.assertEqual(self.records(), [])
        self.assertNotIn("private backend detail", json.dumps(failed))
        self.assertEqual(self.index.project("p1", self.run_id, 1)["state"], "complete")
        self.assertEqual(len(self.records()), 4)

    def test_concurrent_rebuilds_publish_one_consistent_projection(self):
        self.append()
        with ThreadPoolExecutor(max_workers=10) as pool:
            results = list(pool.map(lambda _: self.index.project("p1", self.run_id, 1), range(20)))
        self.assertTrue(all(result == results[0] for result in results))
        self.assertEqual(results[0]["state"], "complete")
        self.assertEqual(len(self.records()), 4)
        self.assertEqual(self.repo.rows("SELECT count(*) AS n FROM trace_revisions")[0]["n"], 1)


class TraceIndexTests(IndexAssertions, unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.repo = Repository(Path(directory.name) / "index.sqlite")
        self.addCleanup(self.repo.close)
        self.content = LocalContentStore(Path(directory.name) / "objects")
        self.revisions = TraceRevisions(self.repo, self.content)
        self.index = TraceIndex(self.revisions)
        self.document = json.loads((ROOT / "examples/minimal.trace.json").read_text())
        self.run_id = self.document["run"]["id"]


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "Dedicated PostgreSQL test database required")
class PostgresTraceIndexTests(IndexAssertions, unittest.TestCase):
    def setUp(self):
        base = make_url(os.environ["TEST_DATABASE_URL"]).set(drivername="postgresql+psycopg")
        admin = create_engine(base, hide_parameters=True)
        schema = "test_index_" + uuid.uuid4().hex
        with admin.begin() as db:
            db.execute(text("CREATE SCHEMA " + schema))
        def cleanup():
            if hasattr(self, "repo"):
                self.repo.close()
            with admin.begin() as db:
                db.execute(text("DROP SCHEMA " + schema + " CASCADE"))
            admin.dispose()
        self.addCleanup(cleanup)
        url = base.update_query_dict({"options": "-csearch_path=" + schema})
        self.repo = Repository(url.render_as_string(hide_password=False))
        self.repo.migrate()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.content = LocalContentStore(directory.name)
        self.revisions = TraceRevisions(self.repo, self.content)
        self.index = TraceIndex(self.revisions)
        self.document = json.loads((ROOT / "examples/minimal.trace.json").read_text())
        self.run_id = self.document["run"]["id"]


if __name__ == "__main__":
    unittest.main()
