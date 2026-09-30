"""Exact content, optimistic append, project isolation and immutable history."""

import copy
import json
import os
import sys
import tempfile
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]
from scripts.build_api_contract import synthetic_inputs
from trace_hunter.content import LocalContentStore
from trace_hunter.database import Conflict, Repository
from trace_hunter.traces import TraceRevisions


class RevisionAssertions:
    def raw(self, title=None):
        trace = copy.deepcopy(self.first)
        if title is not None:
            trace["run"]["title"] = title
        return json.dumps(trace, ensure_ascii=False, indent=2).encode() + b"\n"

    def test_append_and_read_preserve_bytes_and_legacy_digest(self):
        raw = self.raw()
        result = self.service.append("p1", raw, request_key="one")
        self.assertTrue(result["created"])
        self.assertEqual(self.service.read("p1", self.run_id, 1), raw)
        self.assertEqual(result["revision"]["metadata"]["source_verification"], "document_bytes_only")
        self.assertEqual(self.repo.rows("SELECT count(*) AS n FROM analysis_results")[0]["n"], 0)

    def test_idempotent_request_and_conflicting_reuse(self):
        a = self.service.append("p1", self.raw(), request_key="one")
        b = self.service.append("p1", self.raw(), request_key="one")
        self.assertFalse(b["created"])
        self.assertEqual(a["revision"], b["revision"])
        with self.assertRaises(Conflict):
            self.service.append("p1", self.raw("changed"), request_key="one")
        self.assertEqual(self.service.read("p1", self.run_id, 1), self.raw())

    def test_supplement_is_new_revision_and_stale_append_rolls_back(self):
        self.service.append("p1", self.raw(), request_key="one")
        updated = self.raw("补采后的说明")
        self.service.append("p1", updated, request_key="two", expected_previous=1, derivation="supplement")
        with self.assertRaises(Conflict):
            self.service.append("p1", self.raw("stale"), request_key="stale", expected_previous=1)
        self.assertEqual(self.service.get("p1", self.run_id)["revision"], 2)
        self.assertEqual(self.service.read("p1", self.run_id, 1), self.raw())
        self.assertEqual(self.service.read("p1", self.run_id, 2), updated)
        self.assertEqual(self.repo.rows("SELECT count(*) AS n FROM trace_revisions")[0]["n"], 2)

    def test_project_identity_and_explicit_history_pagination(self):
        self.service.append("p1", self.raw(), request_key="shared")
        self.service.append("p2", self.raw("different project"), request_key="shared")
        with self.assertRaises(KeyError):
            self.service.get("p3", self.run_id)
        self.service.append("p1", self.raw("two"), request_key="two", expected_previous=1)
        page = self.service.history("p1", self.run_id, limit=1)
        self.assertEqual([r["revision"] for r in page["items"]], [2])
        page = self.service.history("p1", self.run_id, before=page["next_before"], limit=1)
        self.assertEqual([r["revision"] for r in page["items"]], [1])
        self.assertIsNone(page["next_before"])

    def test_invalid_or_duplicate_key_json_never_registers_a_revision(self):
        for raw in (b"{}", b'{"schema_version":"trace-hunter/1.1","schema_version":"other"}'):
            with self.assertRaises(ValueError):
                self.service.append("p1", raw, request_key="bad")
        self.assertEqual(self.repo.rows("SELECT count(*) AS n FROM traces")[0]["n"], 0)

    def test_v2_multiturn_document_is_stored_as_experimental_not_training_ready(self):
        raw = (ROOT / "examples/drafts/trace-v2/multiturn-resume.json").read_bytes()
        result = self.service.append("p1", raw, request_key="v2")
        self.assertEqual(result["revision"]["metadata"]["format_stability"], "experimental")
        self.assertEqual(result["revision"]["metadata"]["source_verification"], "document_bytes_only")
        self.assertEqual(self.service.read("p1", "multiturn-resume", 1), raw)

    def test_storage_schema_is_not_changed_by_the_draft_validator(self):
        from unittest.mock import patch
        from jsonschema import Draft202012Validator
        raw = (ROOT / "examples/drafts/trace-v2/minimal-partial.json").read_bytes()
        with patch("trace_hunter.contract_preview.VALIDATOR", Draft202012Validator({"not": {}})):
            result = self.service.append("p1", raw, request_key="pinned-profile")
        self.assertEqual(result["revision"]["format_version"], "trace-hunter/2.0-draft.1")

    def test_concurrent_identical_request_creates_exactly_one_revision(self):
        raw = self.raw()
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.service.append("p1", raw, request_key="same"), range(16)))
        self.assertEqual(sum(r["created"] for r in results), 1)
        self.assertEqual(self.repo.rows("SELECT count(*) AS n FROM trace_revisions")[0]["n"], 1)

    def test_competing_next_revisions_do_not_fork_the_head(self):
        self.service.append("p1", self.raw(), request_key="one")
        def append(n):
            try:
                return self.service.append("p1", self.raw(str(n)), request_key=str(n), expected_previous=1)
            except Conflict:
                return None
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(append, range(4)))
        self.assertEqual(sum(r is not None for r in results), 1)
        self.assertEqual(self.service.get("p1", self.run_id)["revision"], 2)
        self.assertEqual(self.repo.rows("SELECT count(*) AS n FROM trace_revisions")[0]["n"], 2)


class TraceRevisionTests(RevisionAssertions, unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.repo = Repository(Path(directory.name) / "test.sqlite")
        self.addCleanup(self.repo.close)
        self.content = LocalContentStore(Path(directory.name) / "objects")
        self.service = TraceRevisions(self.repo, self.content)
        self.first, _, _ = synthetic_inputs()
        self.run_id = self.first["run"]["id"]


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "Dedicated PostgreSQL test database required")
class PostgresTraceRevisionTests(RevisionAssertions, unittest.TestCase):
    def setUp(self):
        base = make_url(os.environ["TEST_DATABASE_URL"]).set(drivername="postgresql+psycopg")
        admin = create_engine(base, hide_parameters=True)
        schema = "test_revision_" + uuid.uuid4().hex
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
        self.service = TraceRevisions(self.repo, self.content)
        self.first, _, _ = synthetic_inputs()
        self.run_id = self.first["run"]["id"]


if __name__ == "__main__":
    unittest.main()
