"""Single-statement record aggregates on SQLite and isolated PostgreSQL schemas."""

import copy
import json
import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]
from scripts.build_api_contract import synthetic_inputs
from trace_hunter.content import LocalContentStore
from trace_hunter.database import Repository
from trace_hunter.query.aggregate import GROUP_FIELDS, TraceAggregates
from trace_hunter.traces import TraceRevisions
from trace_hunter.traces.index import TraceIndex


class AggregateAssertions:
    def seed(self, run_id, *, project="one", indexed=True, previous=0, empty=False, document=None, **metadata):
        source = copy.deepcopy(self.source if document is None else document)
        source["run"].update(id=run_id, **metadata)
        if empty:
            source.update(spans=[], links=[], evidence=[])
        self.revisions.append(project, json.dumps(source).encode(), request_key=uuid.uuid4().hex,
                              expected_previous=previous)
        if indexed:
            result = self.index.project(project, run_id, previous + 1)
            self.assertEqual(result["state"], "complete")
        return source

    def test_empty_selection_has_known_zero_and_no_artificial_null_group(self):
        for request in ({}, {"group_by": "model"}):
            result = self.aggregates.summarize("absent", request)
            totals = result["totals"]
            self.assertEqual(totals["matched_revisions"], 0)
            self.assertEqual(totals["indexed_revision_count"], 0)
            self.assertEqual(set(totals["counts"].values()), {0})
            self.assertEqual(set(totals["record_status"].values()), {0})
            self.assertEqual(totals["error_rate"], {"basis": "indexed_records_with_known_outcome",
                "numerator": 0, "denominator": 0, "value": None})
            self.assertEqual(result["groups"], [])
            self.assertEqual((result["group_count"], result["remaining_group_count"], result["truncated"]), (0, 0, False))
            self.assertEqual(result["watermark"], {"kind": "coverage", "matched_revisions": 0,
                "indexed_revision_count": 0, "latest_indexed_at": None})

    def test_missing_projection_is_unknown_but_empty_complete_is_zero(self):
        self.seed("unindexed", indexed=False)
        missing = self.aggregates.summarize("one", {})["totals"]
        self.assertEqual((missing["unindexed"], missing["index_complete"]), (1, 0))
        self.assertEqual(set(missing["counts"].values()), {None})
        self.assertEqual(set(missing["record_status"].values()), {None})
        self.assertEqual(missing["error_rate"]["denominator"], None)
        self.seed("empty", empty=True)
        groups = self.aggregates.summarize("one", {"group_by": "index_state"})["groups"]
        self.assertEqual([group["value"] for group in groups], ["complete", "unindexed"])
        known = groups[0]["metrics"]
        self.assertEqual(known["indexed_revision_count"], 1)
        self.assertEqual(set(known["counts"].values()), {0})
        self.assertEqual(set(known["record_status"].values()), {0})
        self.assertIsNotNone(known["latest_indexed_at"])

    def test_mixed_coverage_includes_failed_and_unindexed_revision_metadata(self):
        self.seed("ok")
        self.seed("missing", indexed=False)
        self.seed("failed", indexed=False)
        with patch.object(self.index, "_project", side_effect=RuntimeError("private failure detail")):
            self.assertEqual(self.index.project("one", "failed", 1)["state"], "failed")
        request = {"group_by": "index_state", "filters": {
            key: [self.source["run"][key]] for key in ("query_id", "env_id", "harness", "model", "status")}}
        result = self.aggregates.summarize("one", request)
        totals = result["totals"]
        self.assertEqual((totals["matched_revisions"], totals["index_complete"], totals["index_failed"], totals["unindexed"]), (3, 1, 1, 1))
        self.assertEqual(totals["indexed_revision_count"], 1)
        self.assertEqual(totals["counts"]["records"], 4)
        self.assertEqual(totals["record_status"], {"ok": 4, "error": 0, "unknown": 0, "other": 0})
        self.assertEqual([group["value"] for group in result["groups"]], ["complete", "failed", "unindexed"])
        for group in result["groups"][1:]:
            self.assertEqual(set(group["metrics"]["counts"].values()), {None})
            self.assertIsNone(group["metrics"]["latest_indexed_at"])
        self.assertNotIn("private failure detail", json.dumps(result))

    def test_record_status_counts_and_revision_totals_do_not_multiply_at_join(self):
        for span, status in zip(self.source["spans"], ("error", "ok", "unknown", "running")):
            span["status"] = status
        self.seed("a")
        self.seed("b")
        self.seed("missing", indexed=False)
        totals = self.aggregates.summarize("one", {})["totals"]
        self.assertEqual(totals["matched_revisions"], 3)
        self.assertEqual(totals["indexed_revision_count"], 2)
        self.assertEqual(totals["counts"], {"records": 8, "models": 4, "model_batches": 0,
            "tools": 4, "agents": 0, "waits": 0, "other": 0, "unknown": 2})
        self.assertEqual(totals["record_status"], {"ok": 2, "error": 2, "unknown": 2, "other": 2})
        self.assertEqual(totals["error_rate"], {"basis": "indexed_records_with_known_outcome",
            "numerator": 2, "denominator": 4, "value": 0.5})

    def test_known_unknown_status_is_counted_without_inventing_failure_rate(self):
        for span in self.source["spans"]:
            span["status"] = "unknown"
        self.seed("unknown-outcomes")
        totals = self.aggregates.summarize("one", {})["totals"]
        self.assertEqual(totals["counts"]["unknown"], 4)
        self.assertEqual(totals["record_status"], {"ok": 0, "error": 0, "unknown": 4, "other": 0})
        self.assertEqual(totals["error_rate"]["denominator"], 0)
        self.assertIsNone(totals["error_rate"]["value"])

    def test_revision_metadata_drives_groups_even_when_projection_metadata_differs(self):
        self.seed("indexed", query_id="truth")
        self.seed("pending", indexed=False, query_id="truth")
        with self.repo.engine.begin() as db:
            db.execute(text("UPDATE traces SET query_id_hex='7374616c65'"))
        result = self.aggregates.summarize("one", {"group_by": "query_id", "filters": {"query_id": ["truth"]}})
        self.assertEqual(result["totals"]["matched_revisions"], 2)
        self.assertEqual(result["groups"][0]["value"], "truth")
        self.assertEqual(result["groups"][0]["metrics"]["matched_revisions"], 2)

    def test_project_revision_and_projector_boundaries_do_not_leak_or_fall_back(self):
        self.seed("same", query_id="old-query")
        self.seed("same", indexed=False, previous=1, query_id="new-query")
        self.seed("same", project="two", query_id="other-project")
        self.index.projector_version = "future-test-projector"
        self.assertEqual(self.index.project("one", "same", 2)["state"], "complete")
        latest = self.aggregates.summarize("one", {"group_by": "query_id"})
        self.assertEqual(latest["totals"]["matched_revisions"], 1)
        self.assertEqual(latest["totals"]["unindexed"], 1)
        self.assertIsNone(latest["totals"]["counts"]["records"])
        self.assertEqual(latest["groups"][0]["value"], "new-query")
        all_revisions = self.aggregates.summarize("one", {"revisions": "all", "group_by": "query_id"})
        self.assertEqual(all_revisions["totals"]["matched_revisions"], 2)
        self.assertEqual(all_revisions["totals"]["counts"]["records"], 4)
        old = self.aggregates.summarize("one", {"revisions": "all", "filters": {"query_id": ["old-query"]}})
        self.assertEqual(old["totals"]["index_complete"], 1)
        self.assertEqual(self.aggregates.summarize("two", {})["totals"]["counts"]["records"], 4)

    def test_null_group_and_literal_unknown_remain_distinct_without_batch_expansion(self):
        document = json.loads((ROOT / "examples/drafts/trace-v2/opaque-batch.json").read_text())
        self.seed("batch", document=document)
        self.seed("literal-unknown", model="unknown")
        result = self.aggregates.summarize("one", {"group_by": "model"})
        self.assertEqual([group["value"] for group in result["groups"]], ["unknown", None])
        batch = result["groups"][1]["metrics"]
        self.assertEqual((batch["counts"]["records"], batch["counts"]["models"], batch["counts"]["model_batches"]), (1, 0, 1))
        for field in ("tokens", "turns", "wall_ms", "duration_ms"):
            self.assertNotIn(field, json.dumps(result))

    def test_fifty_group_limit_reports_full_totals_and_exact_remaining_groups(self):
        for ordinal in range(55):
            self.seed("run-" + str(ordinal), indexed=False, query_id=f"q{ordinal:02}")
        result = self.aggregates.summarize("one", {"group_by": "query_id"})
        self.assertEqual(result["totals"]["matched_revisions"], 55)
        self.assertEqual((result["group_count"], result["remaining_group_count"], result["truncated"]), (55, 5, True))
        self.assertEqual([group["value"] for group in result["groups"]], [f"q{ordinal:02}" for ordinal in range(50)])
        self.assertIsNone(result["totals"]["counts"]["records"])
        without_groups = self.aggregates.summarize("one", {})
        self.assertEqual(without_groups["totals"], result["totals"])
        self.assertEqual(without_groups["group_count"], 0)
        self.assertFalse(without_groups["truncated"])

    def test_groups_sort_by_revision_count_then_binary_value_before_limit(self):
        for number, value in enumerate(("é", "a", "Z", "a", "Z", "a", "A", "z")):
            self.seed(str(number), empty=True, model=value)
        result = self.aggregates.summarize("one", {"group_by": "model", "limit": 4})
        self.assertEqual([group["value"] for group in result["groups"]], ["a", "Z", "A", "z"])
        self.assertEqual([group["metrics"]["matched_revisions"] for group in result["groups"]], [3, 2, 1, 1])
        self.assertEqual(result["totals"]["matched_revisions"], 8)
        self.assertEqual(result["remaining_group_count"], 1)
        for field in GROUP_FIELDS:
            with self.subTest(group_by=field):
                self.assertEqual(self.aggregates.summarize("one", {"group_by": field})["totals"], result["totals"])

    def test_one_read_statement_no_content_indexing_or_jobs(self):
        self.seed("ready")
        self.seed("not-ready", indexed=False)
        statements = []
        def observe(conn, cursor, statement, params, context, executemany):
            statements.append(statement)
        event.listen(self.repo.engine, "before_cursor_execute", observe)
        try:
            with patch.object(self.content, "open_verified", side_effect=AssertionError("no body read")), \
                    patch.object(self.index, "project", side_effect=AssertionError("no implicit indexing")):
                result = self.aggregates.summarize("one", {"group_by": "query_id"})
        finally:
            event.remove(self.repo.engine, "before_cursor_execute", observe)
        self.assertEqual(len(statements), 1)
        self.assertRegex(statements[0].lstrip(), r"(?i)^(WITH|SELECT)\b")
        self.assertIn("FROM trace_revisions", statements[0])
        self.assertIn("trace_objects", statements[0])
        self.assertEqual(result["consistency"], "statement_snapshot")
        self.assertNotIn("input", json.dumps(result))
        self.assertEqual(self.repo.rows("SELECT COUNT(*) AS n FROM analysis_results")[0]["n"], 0)
        self.assertEqual(self.repo.rows("SELECT COUNT(*) AS n FROM trace_revisions")[0]["n"], 2)

    def test_coverage_timestamp_is_latest_observation_not_continuous_watermark(self):
        self.seed("a")
        self.seed("b")
        self.seed("pending", indexed=False)
        with self.repo.engine.begin() as db:
            db.execute(text("UPDATE trace_revisions SET indexed_at=:stamp WHERE run_id='a'"), {"stamp": "2026-01-01T00:00:00Z"})
            db.execute(text("UPDATE trace_revisions SET indexed_at=:stamp WHERE run_id='b'"), {"stamp": "2026-01-03T00:00:00Z"})
        result = self.aggregates.summarize("one", {})
        self.assertEqual(result["watermark"], {"kind": "coverage", "matched_revisions": 3,
            "indexed_revision_count": 2, "latest_indexed_at": "2026-01-03T00:00:00Z"})
        self.index.project("one", "pending", 1)
        next_result = self.aggregates.summarize("one", {})
        self.assertEqual(next_result["query_digest"], result["query_digest"])
        self.assertEqual(next_result["watermark"]["indexed_revision_count"], 3)

    def test_request_digest_binds_selection_not_group_limit_and_filters_normalize(self):
        a = self.aggregates.summarize("one", {"filters": {"run_id": ["b", "a", "b"]}, "group_by": "model"})
        b = self.aggregates.summarize("one", {"filters": {"run_id": ["a", "b"]}, "group_by": "model", "limit": 1})
        self.assertEqual(a["query_digest"], b["query_digest"])
        for project, request in (("two", {"filters": {"run_id": ["a", "b"]}, "group_by": "model"}),
                                 ("one", {"filters": {"run_id": ["a"]}, "group_by": "model"}),
                                 ("one", {"filters": {"run_id": ["a", "b"]}, "group_by": "query_id"}),
                                 ("one", {"filters": {"run_id": ["a", "b"]}, "group_by": "model", "revisions": "all"})):
            self.assertNotEqual(a["query_digest"], self.aggregates.summarize(project, request)["query_digest"])

    def test_invalid_options_fail_without_database_access(self):
        invalid = [None, [], {"fields": ["run_id"]}, {"cursor": "ignored?"}, {"tokens": True},
                   {"group_by": []}, {"group_by": "run_id"}, {"group_by": "model;DROP TABLE runs"},
                   {"revisions": "first"}, {"filters": {"missing": ["x"]}},
                   {"filters": {"run_id": []}}, {"filters": {"run_id": ["x\x00y"]}}]
        invalid += [{"limit": value} for value in (0, 51, -1, True, 1.5, "1", None)]
        with patch.object(self.repo.engine, "connect", side_effect=AssertionError("must validate before SQL")):
            for request in invalid:
                with self.subTest(request=request), self.assertRaises(ValueError):
                    self.aggregates.summarize("one", request)
            with self.assertRaises(ValueError):
                self.aggregates.summarize("one\x00two", {})


class TraceAggregateTests(AggregateAssertions, unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.repo = Repository(Path(directory.name) / "test.sqlite")
        self.addCleanup(self.repo.close)
        self.content = LocalContentStore(Path(directory.name) / "content")
        self.revisions = TraceRevisions(self.repo, self.content)
        self.index = TraceIndex(self.revisions)
        self.aggregates = TraceAggregates(self.repo)
        self.source, _, _ = synthetic_inputs()


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "Dedicated PostgreSQL test database required")
class PostgresTraceAggregateTests(AggregateAssertions, unittest.TestCase):
    def setUp(self):
        base = make_url(os.environ["TEST_DATABASE_URL"]).set(drivername="postgresql+psycopg")
        admin = create_engine(base, hide_parameters=True)
        schema = "test_aggregate_" + uuid.uuid4().hex
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
        self.aggregates = TraceAggregates(self.repo)
        self.source, _, _ = synthetic_inputs()


if __name__ == "__main__":
    unittest.main()
