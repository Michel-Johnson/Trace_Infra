"""Real index queries: paging, project boundaries and changing live data."""

import base64
import copy
import gc
import json
import os
import sys
import tempfile
import unittest
import uuid
import warnings
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]
from scripts.build_api_contract import synthetic_inputs
from trace_hunter.content import LocalContentStore
from trace_hunter.database import Repository
from trace_hunter.query import TraceQuery
from trace_hunter.query.service import binding, options, VERSION
from trace_hunter.traces import TraceRevisions
from trace_hunter.traces.index import TraceIndex


class QueryAssertions:
    def seed(self, run_id, *, project="one", indexed=True, previous=0, empty=False):
        source = copy.deepcopy(self.source)
        source["run"]["id"] = run_id
        source["run"]["title"] = "revision " + str(previous + 1)
        if empty:
            source["spans"] = []
            source["links"] = []
            source["evidence"] = []
        result = self.revisions.append(project, json.dumps(source).encode(), request_key=uuid.uuid4().hex,
                                       expected_previous=previous)
        if indexed:
            self.index.project(project, run_id, previous + 1)
        return result

    def test_sparse_fields_and_filters_exclude_bodies_and_other_projects(self):
        self.seed("shared")
        self.seed("shared", project="two")
        self.seed("unindexed", indexed=False)
        response = self.query.query("one", {"fields": ["run_id", "revision", "record_count"],
                                           "filters": {"index_state": ["complete"]}})
        self.assertEqual(response["items"], [{"run_id": "shared", "revision": 1, "record_count": len(self.source["spans"])}])
        self.assertEqual(response["consistency"], "live_keyset")
        self.assertNotIn("input", json.dumps(response))
        self.assertIsNone(response["next_cursor"])
        self.assertEqual(self.query.query("missing", {})["items"], [])

    def test_unindexed_is_visible_and_known_zero_is_preserved(self):
        self.seed("a", indexed=False)
        self.seed("b", empty=True)
        rows = self.query.query("one", {})["items"]
        self.assertEqual(rows[0]["index_state"], "unindexed")
        self.assertIsNone(rows[0]["record_count"])
        self.assertEqual(rows[1]["record_count"], 0)
        self.assertEqual(rows[1]["index_state"], "complete")

    def test_unindexed_and_failed_records_filter_by_revision_metadata(self):
        self.seed("a-unindexed", indexed=False)
        self.seed("b-failed", indexed=False)
        with patch.object(self.index, "_project", side_effect=RuntimeError("projection failure")):
            self.assertEqual(self.index.project("one", "b-failed", 1)["state"], "failed")
        metadata_fields = ["query_id", "env_id", "harness", "model", "status"]
        request = {"fields": ["run_id", "index_state", "record_count", "indexed_at", *metadata_fields],
                   "filters": {key: [self.source["run"][key]] for key in metadata_fields}}
        rows = self.query.query("one", request)["items"]
        self.assertEqual([row["run_id"] for row in rows], ["a-unindexed", "b-failed"])
        self.assertEqual([row["index_state"] for row in rows], ["unindexed", "failed"])
        for row in rows:
            self.assertIsNone(row["record_count"])
            self.assertIsNone(row["indexed_at"])
            for key in metadata_fields:
                self.assertEqual(row[key], self.source["run"][key])

    def test_identity_nul_is_rejected_but_metadata_nul_is_lossless(self):
        with self.assertRaises(ValueError):
            self.query.query("project\x00other", {})
        for field in ("run_id", "content_digest", "index_state"):
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.query.query("one", {"filters": {field: ["value\x00suffix"]}})
        raw = json.dumps({"v": VERSION, "binding": binding("one", options({})),
                          "run_id": "run\x00suffix", "revision": 1}).encode()
        cursor = base64.urlsafe_b64encode(raw).decode()
        with self.assertRaises(ValueError):
            self.query.query("one", {"cursor": cursor})
        values = {"nul-model": "left\x00right", "literal-model": "left\\u0000right"}
        for run_id, model in values.items():
            self.source["run"]["model"] = model
            self.seed(run_id)
        for run_id, model in values.items():
            result = self.query.query("one", {"fields": ["run_id", "model"], "filters": {"model": [model]}})
            self.assertEqual(result["items"], [{"run_id": run_id, "model": model}])
        for field in ("query_id", "env_id", "harness", "status"):
            self.assertEqual(self.query.query("one", {"filters": {field: ["no\x00match"]}})["items"], [])

    def test_cursor_revision_is_bounded_by_database_signed_integer(self):
        for mode in ("latest", "all"):
            request = {"revisions": mode}
            for revision in (0, -1, True, 1.5, 2**31, 2**63, 10**100):
                raw = json.dumps({"v": VERSION, "binding": binding("one", options(request)),
                                  "run_id": "last", "revision": revision}).encode()
                cursor = base64.urlsafe_b64encode(raw).decode()
                self.assertLess(len(cursor), 4096)
                with self.subTest(mode=mode, revision=revision), self.assertRaises(ValueError):
                    self.query.query("one", {**request, "cursor": cursor})
            raw = json.dumps({"v": VERSION, "binding": binding("one", options(request)),
                              "run_id": "last", "revision": 2**31 - 1}).encode()
            cursor = base64.urlsafe_b64encode(raw).decode()
            self.assertEqual(self.query.query("one", {**request, "cursor": cursor})["items"], [])

    def test_latest_metadata_never_falls_back_to_previous_revision_index(self):
        self.seed("changing")
        old_query_id = self.source["run"]["query_id"]
        self.source["run"].update(query_id="new-query", status="partial")
        self.seed("changing", previous=1, indexed=False)
        fields = ["run_id", "revision", "query_id", "status", "index_state", "record_count", "indexed_at"]
        self.assertEqual(self.query.query("one", {"filters": {"query_id": [old_query_id]}})["items"], [])
        newest = self.query.query("one", {"fields": fields, "filters": {"query_id": ["new-query"], "status": ["partial"]}})["items"]
        self.assertEqual(newest, [{"run_id": "changing", "revision": 2, "query_id": "new-query",
                                  "status": "partial", "index_state": "unindexed", "record_count": None, "indexed_at": None}])
        old = self.query.query("one", {"revisions": "all", "fields": fields,
                                      "filters": {"query_id": [old_query_id]}})["items"]
        self.assertEqual(len(old), 1)
        self.assertEqual((old[0]["revision"], old[0]["status"], old[0]["index_state"]), (1, "completed", "complete"))

    def test_keyset_all_revisions_has_no_duplicates(self):
        for run_id in ("a", "b", "c"):
            self.seed(run_id)
            self.seed(run_id, previous=1)
        value = {"revisions": "all", "limit": 2, "fields": ["run_id", "revision"]}
        found = []
        while True:
            page = self.query.query("one", value)
            found.extend((row["run_id"], row["revision"]) for row in page["items"])
            if page["next_cursor"] is None:
                break
            value["cursor"] = page["next_cursor"]
        self.assertEqual(found, [(r, v) for r in "abc" for v in (1, 2)])

    def test_newest_created_order_is_stable_across_pages_and_ties(self):
        for run_id in ("z-old", "b-new", "a-new"):
            self.seed(run_id)
        with self.repo.engine.begin() as db:
            db.execute(text("UPDATE trace_revisions SET created_at=:at WHERE project_id='one' AND run_id=:run"),
                       [{"at": "2026-09-20T00:00:00+00:00", "run": "z-old"},
                        {"at": "2026-09-21T00:00:00+00:00", "run": "b-new"},
                        {"at": "2026-09-21T00:00:00+00:00", "run": "a-new"}])
        value = {"order": "created_at_desc", "limit": 1, "fields": ["run_id"]}
        found = []
        while True:
            page = self.query.query("one", value)
            found.extend(item["run_id"] for item in page["items"])
            if page["next_cursor"] is None:
                break
            value["cursor"] = page["next_cursor"]
        self.assertEqual(found, ["a-new", "b-new", "z-old"])
        with self.assertRaises(ValueError):
            self.query.query("one", {"cursor": value["cursor"]})

    def test_live_latest_pagination_does_not_repeat_a_run_after_append(self):
        self.seed("a")
        self.seed("b")
        first = self.query.query("one", {"limit": 1})
        self.seed("a", previous=1)
        self.seed("b", previous=1)
        second = self.query.query("one", {"limit": 1, "cursor": first["next_cursor"]})
        self.assertEqual([(r["run_id"], r["revision"]) for r in second["items"]], [("b", 2)])
        self.assertIsNone(second["next_cursor"])
        self.assertEqual(second["consistency"], "live_keyset")

    def test_cursor_binds_project_filters_fields_and_revision_mode(self):
        self.seed("a")
        self.seed("b")
        cursor = self.query.query("one", {"limit": 1})["next_cursor"]
        for project, value in (("two", {}), ("one", {"fields": ["run_id"]}),
                               ("one", {"filters": {"run_id": ["b"]}}), ("one", {"revisions": "all"})):
            with self.subTest(project=project, value=value), self.assertRaises(ValueError):
                self.query.query(project, {**value, "cursor": cursor})
        self.assertEqual(len(self.query.query("one", {"limit": 50, "cursor": cursor})["items"]), 1)

    def test_unsafe_fields_invalid_limits_and_bad_cursors_are_rejected(self):
        cases = [{"fields": ["payload"]}, {"fields": ["r.*"]}, {"fields": []}, {"fields": ["run_id", "run_id"]},
                 {"filters": {"run_id OR 1=1": ["a"]}}, {"filters": {"run_id": []}},
                 {"filters": {"run_id": ["a"] * 51}}, {"limit": True}, {"limit": 101}, {"limit": 0},
                 {"sort": "DROP TABLE runs"}, {"cursor": "../not-a-cursor"}, {"cursor": "x" * 4097}]
        for value in cases:
            with self.subTest(value=str(value)[:100]), self.assertRaises(ValueError):
                self.query.query("one", value)
        self.assertEqual(self.query.query("one", {"filters": {"run_id": ["' OR 1=1 --"]}})["items"], [])

    def test_query_is_read_only_without_projection_or_analysis(self):
        self.seed("a", indexed=False)
        with patch.object(self.revisions, "read", side_effect=AssertionError("no content read")), \
                patch.object(self.index, "project", side_effect=AssertionError("no rebuild")):
            self.query.query("one", {})
            self.query.capabilities()
        self.assertEqual(self.repo.rows("SELECT projection_state FROM trace_revisions")[0]["projection_state"], "unindexed")
        self.assertEqual(self.repo.rows("SELECT count(*) AS n FROM analysis_results")[0]["n"], 0)

    def test_byte_limited_page_continues_from_last_delivered_item(self):
        for run_id in "abc":
            self.seed(run_id)
        with patch("trace_hunter.query.service.MAX_ITEM_BYTES", 18):
            first = self.query.query("one", {"fields": ["run_id"]})
            self.assertEqual(first["items"], [{"run_id": "a"}])
            second = self.query.query("one", {"fields": ["run_id"], "cursor": first["next_cursor"]})
            self.assertEqual(second["items"], [{"run_id": "b"}])
        with patch("trace_hunter.query.service.MAX_ITEM_BYTES", 1), self.assertRaises(ValueError):
            self.query.query("one", {})

    def test_early_page_exit_closes_database_stream(self):
        for run_id in "abc":
            self.seed(run_id)
        with warnings.catch_warnings(record=True) as captured:
            warnings.simplefilter("always", ResourceWarning)
            self.assertIsNotNone(self.query.query("one", {"limit": 1})["next_cursor"])
            with patch("trace_hunter.query.service.MAX_ITEM_BYTES", 1), self.assertRaises(ValueError):
                self.query.query("one", {})
            gc.collect()
        self.assertEqual([str(item.message) for item in captured
                          if issubclass(item.category, ResourceWarning) and "still open" in str(item.message)], [])


class TraceQueryTests(QueryAssertions, unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.repo = Repository(Path(directory.name) / "test.sqlite")
        self.addCleanup(self.repo.close)
        self.revisions = TraceRevisions(self.repo, LocalContentStore(Path(directory.name) / "content"))
        self.index = TraceIndex(self.revisions)
        self.query = TraceQuery(self.repo)
        self.source, _, _ = synthetic_inputs()


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "Dedicated PostgreSQL test database required")
class PostgresTraceQueryTests(QueryAssertions, unittest.TestCase):
    def setUp(self):
        base = make_url(os.environ["TEST_DATABASE_URL"]).set(drivername="postgresql+psycopg")
        admin = create_engine(base, hide_parameters=True)
        schema = "test_query_" + uuid.uuid4().hex
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
        self.revisions = TraceRevisions(self.repo, LocalContentStore(directory.name))
        self.index = TraceIndex(self.revisions)
        self.query = TraceQuery(self.repo)
        self.source, _, _ = synthetic_inputs()


if __name__ == "__main__":
    unittest.main()
