"""Trigram-ready search never widens a model's proven context."""
import copy
import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from trace_hunter.content import LocalContentStore
from trace_hunter.database import Repository
from trace_hunter.query.search import TraceSearch, VisibilityReports, _portable_pattern
from trace_hunter.query.spans import SpanQuery
from trace_hunter.traces import TraceRevisions
from trace_hunter.traces.index import TraceIndex

ROOT = Path(__file__).resolve().parents[1]


def availability_document(run_id="search-run"):
    document = json.loads((ROOT / "examples/drafts/trace-v2/multiturn-resume.json").read_text())
    document["schema_version"] = "trace-hunter/2.0-draft.2"
    document["run"]["id"] = run_id
    for message in document["messages"]:
        message["content"]["available_at"] = {"clock_id": "clock-1", "at_ms": 0}
    for context in document["contexts"]:
        context["request"]["available_at"] = {"clock_id": "clock-1", "at_ms": 0}
    return document


class TraceSearchTests(unittest.TestCase):
    def setUp(self):
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.repository = Repository(Path(self.directory) / "search.sqlite")
        self.addCleanup(self.repository.close)
        content = LocalContentStore(Path(self.directory) / "objects")
        self.revisions = TraceRevisions(self.repository, content)
        self.index = TraceIndex(self.revisions)
        self.search = TraceSearch(self.repository)
        self.spans = SpanQuery(self.repository)
        self.reports = VisibilityReports(self.repository)

    def append(self, document):
        result = self.revisions.append("p1", json.dumps(document).encode(), request_key=uuid.uuid4().hex)
        self.index.project("p1", document["run"]["id"], 1)
        return result

    def test_analysis_search_finds_inline_message_and_span_fields(self):
        self.append(availability_document())
        message = self.search.query("p1", {"query": "库存", "mode": "literal", "scope": "analysis"})
        self.assertIn(("message", "user-1", "content"), {
            (item["object_kind"], item["object_id"], item["field"]) for item in message["items"]})
        regex = self.search.query("p1", {"query": "读取.*供应商", "mode": "regex", "scope": "analysis"})
        self.assertTrue(any(item["object_kind"] == "message" for item in regex["items"]))
        alternate = self.search.query("p1", {"query": "(?:库存|供应商)", "mode": "regex", "scope": "analysis"})
        self.assertTrue(alternate["items"])
        expected = "postgresql_pg_trgm" if self.repository.postgres else "sqlite_scan_fallback"
        self.assertEqual(self.search.capabilities()["backend"], expected)
        self.assertEqual(self.search.capabilities()["regex_dialect"], "postgresql_are")

    def test_regex_word_boundaries_and_portable_aliases(self):
        document = json.loads((ROOT / "examples/minimal.trace.json").read_text())
        document["run"]["id"] = "regex-boundaries"
        document["spans"][0]["input"] = "前 tool toolbox tool 后"
        self.append(document)
        canonical = self.search.query("p1", {
            "query": r"\ytool\y", "mode": "regex", "scope": "analysis"})
        self.assertEqual(canonical["effective_pattern"], r"\ytool\y")
        self.assertEqual(canonical["total_count"], 1)
        item = canonical["items"][0]
        self.assertEqual([item["snippet"][part["start"]:part["end"]]
                          for part in item["match_ranges"]], ["tool", "tool"])
        portable = self.search.query("p1", {
            "query": r"\btool\b", "mode": "regex", "regex_syntax": "portable", "scope": "analysis"})
        self.assertEqual(portable["effective_pattern"], r"\ytool\y")
        self.assertEqual(portable["items"][0]["match_ranges"], item["match_ranges"])
        self.assertNotEqual(portable["query_digest"], canonical["query_digest"])
        self.assertEqual(self.search.query("p1", {
            "query": r"\btool\b", "mode": "regex", "scope": "analysis"})["total_count"], 0)
        self.assertEqual(_portable_pattern(r"\\b[\b]\b"), r"\\b[\b]\y")

    def test_invalid_regex_and_syntax_options_fail_explicitly(self):
        self.append(availability_document())
        for request in (
            {"query": "[", "mode": "regex", "scope": "analysis"},
            {"query": "tool", "mode": "literal", "regex_syntax": "portable", "scope": "analysis"},
            {"query": r"(?=tool)", "mode": "regex", "scope": "analysis"},
        ):
            with self.subTest(request=request), self.assertRaises(ValueError):
                self.search.query("p1", request)

    def test_curated_search_terms_preserve_literal_results_and_projection_updates(self):
        first = json.loads((ROOT / "examples/minimal.trace.json").read_text())
        first["run"]["id"] = "hot-first"
        first["spans"][0]["input"] = "open toolkit and 50%_done"
        self.append(first)
        request = {"query": "tool", "mode": "literal", "scope": "analysis", "limit": 1}
        before = self.search.query("p1", request)
        self.assertGreater(before["total_count"], 0)
        self.search.hot_terms.observe("p1", "tool", 42.0, before["total_count"])
        self.search.hot_terms.observe("p1", "tool", 10.0, before["total_count"])
        candidate = self.search.hot_terms.get("p1", "tool")
        self.assertEqual((candidate["search_count"], candidate["average_latency_ms"]), (2, 26.0))
        promoted = self.search.hot_terms.promote("p1", "tool")
        self.assertTrue(promoted["active"])
        self.assertEqual(promoted["posting_count"], before["total_count"])
        after = self.search.query("p1", request)
        for key in ("items", "next_cursor", "total_count", "matched_trace_count", "query_digest"):
            self.assertEqual(after[key], before[key], key)
        if after["next_cursor"]:
            self.assertEqual(self.search.query("p1", {**request, "cursor": after["next_cursor"]})["total_count"],
                             before["total_count"])
        special = {**request, "query": "50%_done"}
        special_before = self.search.query("p1", special)
        self.search.hot_terms.promote("p1", "50%_done")
        self.assertEqual(self.search.query("p1", special)["items"], special_before["items"])
        second = json.loads((ROOT / "examples/minimal.trace.json").read_text())
        second["run"]["id"] = "hot-second"
        second["spans"][0]["input"] = "another toolkit"
        self.append(second)
        indexed = self.search.query("p1", request)
        self.assertEqual(indexed["total_count"], before["total_count"] + 1)
        self.assertEqual(indexed["matched_trace_count"], before["matched_trace_count"] + 1)
        self.index.project("p1", "hot-first", 1)
        self.assertEqual(self.search.query("p1", request)["total_count"], indexed["total_count"])
        self.search.hot_terms.demote("p1", "tool")
        self.assertEqual(self.search.query("p1", request)["total_count"], indexed["total_count"])
        self.assertFalse(self.search.hot_terms.get("p1", "tool")["active"])
        self.assertEqual(self.search.hot_terms.get("p1", "tool")["posting_count"], 0)
        self.assertEqual(self.search.hot_terms.list("p2")["items"], [])

    def test_existing_v1_trace_body_is_searchable_for_analysis(self):
        document = json.loads((ROOT / "examples/minimal.trace.json").read_text())
        document["run"]["id"] = "legacy-search"
        document["spans"][0]["input"] = {"command": "lark-cli base search"}
        self.append(document)
        result = self.search.query("p1", {"query": "lark-cli", "scope": "analysis"})
        self.assertEqual([(item["run_id"], item["field"]) for item in result["items"]],
                         [("legacy-search", "input")])
        item = result["items"][0]
        self.assertEqual(item["snippet"][item["match_ranges"][0]["start"]:
                                         item["match_ranges"][0]["end"]], "lark-cli")
        self.assertIsInstance(item["score"], float)

    def test_text_search_combines_skill_tool_and_status_filters_server_side(self):
        document = json.loads((ROOT / "examples/minimal.trace.json").read_text())
        document["run"]["id"] = "structured-search"
        document["spans"][0]["input"] = {"command": "lark-cli base search"}
        document["spans"][0]["skill"] = {"name": "lark-cli", "action": "invoke"}
        document["spans"][0]["status"] = "error"
        self.append(document)
        matched = self.search.query("p1", {
            "query": "lark-cli", "scope": "analysis",
            "filters": {"skill_name": ["lark-cli"], "name": [document["spans"][0]["name"]],
                        "status": ["error"]},
        })
        self.assertEqual(len(matched["items"]), 1)
        missed = self.search.query("p1", {
            "query": "lark-cli", "scope": "analysis",
            "filters": {"skill_name": ["other"]},
        })
        self.assertEqual(missed["items"], [])
        self.assertEqual((missed["total_count"], missed["matched_trace_count"]), (0, 0))
        self.assertIn("skill_name", self.search.capabilities()["structured_filters"])

    def test_search_returns_bounded_snippet_and_regex_ranges(self):
        document = json.loads((ROOT / "examples/minimal.trace.json").read_text())
        document["run"]["id"] = "snippet-search"
        document["spans"][0]["input"] = "前" * 600 + "读取 /docs/库存说明.md" + "后" * 600
        self.append(document)
        result = self.search.query("p1", {
            "query": r"/docs/[^ ]+\.md", "mode": "regex", "scope": "analysis"})
        item = result["items"][0]
        self.assertLessEqual(len(item["snippet"]), 512)
        self.assertTrue(item["snippet_truncated"])
        self.assertEqual(item["text_length"], len(document["spans"][0]["input"]))
        match = item["match_ranges"][0]
        self.assertEqual(item["snippet"][match["start"]:match["end"]], "/docs/库存说明.md")
        self.assertGreater(item["snippet_start"], 0)

    def test_model_context_search_only_returns_proven_snapshot_documents(self):
        self.append(availability_document())
        result = self.search.query("p1", {
            "query": "库存", "scope": "model_context",
            "visible_to": {"run_id": "search-run", "revision": 1, "model_span_id": "model-1"},
        })
        self.assertEqual(result["visibility"]["status"], "pass")
        self.assertTrue(result["items"])
        self.assertEqual({item["object_kind"] for item in result["items"]}, {"message", "context"})
        self.assertNotIn("assistant-1", {item["object_id"] for item in result["items"]})

    def test_search_cursor_is_stable_and_bound_to_the_query(self):
        self.append(availability_document())
        request = {"query": "库存", "scope": "analysis", "limit": 1}
        first = self.search.query("p1", request)
        self.assertIsNotNone(first["next_cursor"])
        second = self.search.query("p1", {**request, "cursor": first["next_cursor"]})
        self.assertGreater(first["total_count"], len(first["items"]))
        self.assertEqual(second["total_count"], first["total_count"])
        self.assertEqual(second["matched_trace_count"], first["matched_trace_count"])
        first_ids = {(item["object_kind"], item["object_id"], item["field"]) for item in first["items"]}
        second_ids = {(item["object_kind"], item["object_id"], item["field"]) for item in second["items"]}
        self.assertTrue(second_ids)
        self.assertTrue(first_ids.isdisjoint(second_ids))
        with self.assertRaises(ValueError):
            self.search.query("p1", {**request, "query": "供应商", "cursor": first["next_cursor"]})

    def test_future_message_blocks_context_search_and_is_auditable(self):
        document = availability_document()
        next(item for item in document["messages"] if item["id"] == "user-1")["content"]["available_at"]["at_ms"] = 1
        self.append(document)
        result = self.search.query("p1", {
            "query": "库存", "scope": "model_context",
            "visible_to": {"run_id": "search-run", "revision": 1, "model_span_id": "model-1"},
        })
        self.assertEqual(result["items"], [])
        self.assertEqual(result["visibility"]["status"], "fail")
        self.assertIn("FUTURE_MESSAGE_IN_REQUEST_CONTEXT",
                      {item["code"] for item in result["visibility"]["issues"]})
        report = self.reports.get("p1", "search-run", 1)
        self.assertEqual(report["status"], "fail")
        projected = self.spans.query("p1", {
            "filters": {"visibility_status": ["fail"]},
            "fields": ["span_id", "context_id", "visibility_status", "visibility_issues"],
        })
        self.assertEqual(projected["items"][0]["span_id"], "model-1")
        self.assertEqual(projected["items"][0]["visibility_status"], "fail")

    def test_missing_availability_is_unknown_and_never_defaults_to_visible(self):
        document = availability_document()
        del next(item for item in document["messages"] if item["id"] == "user-1")["content"]["available_at"]
        self.append(document)
        result = self.search.query("p1", {
            "query": "库存", "scope": "model_context",
            "visible_to": {"run_id": "search-run", "revision": 1, "model_span_id": "model-1"},
        })
        self.assertEqual((result["visibility"]["status"], result["items"]), ("unknown", []))

    def test_model_output_cannot_be_smuggled_into_its_request_context(self):
        document = availability_document()
        model = next(item for item in document["spans"] if item["id"] == "model-1")
        model["model"]["output_message_ids"] = ["assistant-1"]
        document["contexts"][0]["message_ids"].append("assistant-1")
        self.append(document)
        report = self.reports.get("p1", "search-run", 1)
        item = next(item for item in report["items"] if item["model_span_id"] == "model-1")
        self.assertEqual(item["status"], "fail")
        self.assertIn("MODEL_OUTPUT_IN_REQUEST_CONTEXT", {issue["code"] for issue in item["issues"]})

    def test_model_context_is_bound_to_requested_revision_not_current_head(self):
        first = availability_document()
        self.append(first)
        second = copy.deepcopy(first)
        second["document"].update(id="document-search-revision-2", revision=2,
                                  previous_document_id=first["document"]["id"])
        next(item for item in second["messages"] if item["id"] == "user-1")["content"]["value"] = "未来改写"
        self.revisions.append("p1", json.dumps(second).encode(), request_key=uuid.uuid4().hex,
                              expected_previous=1)
        self.index.project("p1", "search-run", 2)
        result = self.search.query("p1", {
            "query": "库存", "scope": "model_context",
            "visible_to": {"run_id": "search-run", "revision": 1, "model_span_id": "model-1"},
        })
        self.assertTrue(result["items"])
        self.assertEqual({item["revision"] for item in result["items"]}, {1})

    def test_query_is_bounded_and_rejects_unsafe_regex_or_implicit_scope(self):
        self.append(availability_document())
        for value in ({"query": "库存"},
                      {"query": "库存", "mode": "fuzzy", "scope": "analysis"},
                      {"query": "(a+)+", "mode": "regex", "scope": "analysis"},
                      {"query": "库存", "scope": "model_context"},
                      {"query": "库存", "scope": "model_context", "visible_to": {
                          "run_id": "search-run", "revision": 1, "model_span_id": "model-1"},
                       "filters": {"status": ["error"]}},
                      {"query": "库存", "scope": "analysis", "visible_to": {
                          "run_id": "search-run", "revision": 1, "model_span_id": "model-1"}}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.search.query("p1", value)


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "Dedicated PostgreSQL test database required")
class PostgresTraceSearchTests(TraceSearchTests):
    def setUp(self):
        base = make_url(os.environ["TEST_DATABASE_URL"]).set(drivername="postgresql+psycopg")
        admin = create_engine(base, hide_parameters=True)
        schema = "test_search_" + uuid.uuid4().hex
        with admin.begin() as db:
            db.execute(text("CREATE SCHEMA " + schema))

        def cleanup():
            self.repository.close()
            with admin.begin() as db:
                db.execute(text("DROP SCHEMA " + schema + " CASCADE"))
            admin.dispose()

        self.addCleanup(cleanup)
        location = base.update_query_dict({"options": "-csearch_path=" + schema}).render_as_string(hide_password=False)
        self.repository = Repository(location)
        self.repository.migrate()
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        content = LocalContentStore(Path(self.directory) / "objects")
        self.revisions = TraceRevisions(self.repository, content)
        self.index = TraceIndex(self.revisions)
        self.search = TraceSearch(self.repository)
        self.spans = SpanQuery(self.repository)
        self.reports = VisibilityReports(self.repository)

    def test_hot_posting_lookup_has_project_term_range_index(self):
        document = json.loads((ROOT / "examples/minimal.trace.json").read_text())
        document["run"]["id"] = "hot-plan"
        document["spans"][0]["input"] = "toolkit"
        self.append(document)
        self.search.hot_terms.promote("p1", "tool")
        with self.repository.engine.begin() as db:
            db.execute(text("SET LOCAL enable_seqscan=off"))
            plan = "\n".join(row[0] for row in db.execute(text("""EXPLAIN SELECT run_id,revision,document_ordinal
                FROM trace_search_hot_postings WHERE project_id='p1' AND term='tool'
                AND projector_version='trace-index/4' ORDER BY run_id COLLATE "C",revision,document_ordinal
                LIMIT 20""")))
        self.assertIn("trace_search_hot_postings_page", plan)

    def test_hot_literal_pages_reuse_exact_matches_and_invalidate_on_version_change(self):
        version = ["v1"]
        self.search = TraceSearch(self.repository, version_provider=lambda _: version[0])

        def append(run_id):
            document = json.loads((ROOT / "examples/minimal.trace.json").read_text())
            document["run"]["id"] = run_id
            document["spans"][0]["input"] = {"query": "hotterm-2026"}
            self.append(document)

        for index in range(3):
            append(f"hot-{index}")
        request = {"query": "hotterm-2026", "scope": "analysis", "limit": 1}
        first = self.search.query("p1", request)
        self.assertEqual((first["total_count"], first["matched_trace_count"]), (3, 3))
        self.assertEqual(len(self.search._matches), 1)
        with patch.object(self.search, "_postgres_rows", side_effect=AssertionError("match scan repeated")):
            second = self.search.query("p1", {**request, "cursor": first["next_cursor"]})
        self.assertEqual(second["total_count"], 3)
        self.assertEqual(second["items"][0]["run_id"], "hot-1")

        append("hot-3")
        version[0] = "v2"
        refreshed = self.search.query("p1", request)
        self.assertEqual((refreshed["total_count"], refreshed["matched_trace_count"]), (4, 4))


if __name__ == "__main__":
    unittest.main()
