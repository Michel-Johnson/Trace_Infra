"""Composite object analysis stays bounded, evidence-backed and visibility-safe."""

import copy
import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from trace_hunter.content import LocalContentStore
from trace_hunter.database import Repository
from trace_hunter.query import AnalysisResults, ObjectAnalysis
from trace_hunter.traces import TraceRevisions
from trace_hunter.traces.index import TraceIndex
from tests.test_trace_search import availability_document

ROOT = Path(__file__).resolve().parents[1]


class ObjectAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.repository = Repository(Path(self.directory) / "objects.sqlite")
        self.addCleanup(self.repository.close)
        self.revisions = TraceRevisions(
            self.repository, LocalContentStore(Path(self.directory) / "content"))
        self.index = TraceIndex(self.revisions)
        self.results = AnalysisResults(self.repository)
        self.analysis = ObjectAnalysis(self.repository, self.results)

    def append(self, document):
        descriptor = self.revisions.append(
            "p1", json.dumps(document).encode(), request_key=uuid.uuid4().hex)["revision"]
        self.index.project("p1", descriptor["run_id"], descriptor["revision"])

    def seeded(self, run_id="analysis-run"):
        document = availability_document(run_id)
        span = next(item for item in document["spans"] if item["id"] == "read-1")
        span["tool"]["skill"] = {"name": "lark-cli", "action": "invoke"}
        span["status"] = "error"
        span["input"] = {"state": "complete", "value": {
            "command": "lark-cli doc read /docs/guide.md"}}
        span["output"] = {"state": "complete", "value": "ERROR timeout"}
        next_span = next(item for item in document["spans"] if item["id"] == "read-2")
        next_span["input"] = {"state": "complete", "value": {
            "command": "lark-cli task list --failed"}}
        next_span["output"] = {"state": "complete", "value": "ERROR failed task"}
        self.append(document)
        return document

    def test_combines_structured_text_filters_and_statistics(self):
        self.seeded()
        result = self.analysis.analyze("p1", {
            "scope": {"mode": "analysis"},
            "filters": {"skill_name": ["lark-cli"], "status": ["error"]},
            "text": {"query": "lark-cli doc", "fields": ["input"]},
            "text_all": [{"query": "timeout", "fields": ["output"]}],
            "group_by": ["skill_presence"],
        })
        self.assertEqual(result["coverage"]["matched_objects"], 1)
        self.assertEqual(result["groups"][0]["key"], {"skill_presence": "with_skill"})
        self.assertEqual(result["groups"][0]["error_rate"], 1)
        item = result["objects"][0]
        self.assertEqual((item["span_id"], item["skill_name"]), ("read-1", "lark-cli"))
        self.assertEqual({match["field"] for match in item["text_matches"]}, {"input", "output"})

    def test_rejects_removed_fuzzy_text_mode(self):
        self.seeded()
        with self.assertRaises(ValueError):
            self.analysis.analyze("p1", {
                "scope": {"mode": "analysis"},
                "text": {"query": "lark", "mode": "fuzzy"},
            })

    def test_unknown_status_does_not_claim_zero_error_rate(self):
        document = availability_document("unknown-status-run")
        tools = [item for item in document["spans"] if item["kind"] == "tool"]
        for span in tools:
            span["tool"]["skill"] = {"name": "unknown-skill", "action": "load"}
            span["status"] = "unknown"
        self.append(document)
        result = self.analysis.analyze("p1", {
            "scope": {"mode": "analysis"},
            "filters": {"skill_name": ["unknown-skill"]},
            "group_by": ["skill_name"],
        })
        group = result["groups"][0]
        self.assertIsNone(group["error_rate"])
        self.assertEqual(group["status_coverage"], {
            "known_count": 0, "unknown_count": len(tools), "total_count": len(tools)})
        self.assertEqual(group["issues"], [{
            "code": "STATUS_DATA_MISSING",
            "message": "原始数据未包含可确认的执行状态，无法计算错误率。",
        }])

    def test_facets_return_complete_structured_value_counts(self):
        self.seeded("facet-a")
        self.seeded("facet-b")
        result = self.analysis.facets("p1", {
            "field": "skill_name", "filters": {"object_kind": ["span"]}, "limit": 1})
        self.assertEqual(result["items"], [{"value": "lark-cli", "count": 2, "distinct_runs": 2}])
        self.assertGreater(result["missing_count"], 0)
        self.assertEqual(len(result["resolved_revisions"]), 2)
        self.assertFalse(result["truncated"])

    def test_facets_report_truncation_and_trace_metadata(self):
        first = self.seeded("facet-one")
        second = availability_document("facet-two")
        second["run"]["env_id"] = "staging"
        self.append(second)
        result = self.analysis.facets("p1", {"field": "trace_env_id", "limit": 1})
        self.assertEqual(result["group_count"], 2)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["items"][0]["distinct_runs"], 1)
        self.assertIn(result["items"][0]["value"], {"synthetic-vm-v1", "staging"})

    def test_partial_status_uses_only_known_denominator_and_warns(self):
        document = availability_document("partial-status-run")
        tools = [item for item in document["spans"] if item["kind"] == "tool"]
        for span, status in zip(tools, ("error", "unknown")):
            span["tool"]["skill"] = {"name": "partial-skill", "action": "load"}
            span["status"] = status
        self.append(document)
        result = self.analysis.analyze("p1", {
            "scope": {"mode": "analysis"},
            "filters": {"skill_name": ["partial-skill"]},
            "group_by": ["skill_name"],
        })
        group = result["groups"][0]
        self.assertEqual(group["error_rate"], 1)
        self.assertEqual(group["status_coverage"], {
            "known_count": 1, "unknown_count": 1, "total_count": 2})
        self.assertEqual(group["issues"][0]["code"], "STATUS_DATA_PARTIAL")

    def test_traverses_edges_and_source_order(self):
        self.seeded()
        invoked = self.analysis.analyze("p1", {
            "scope": {"mode": "analysis"}, "filters": {"span_id": ["model-1"]},
            "steps": [{"relation": "invokes", "filters": {"kind": ["tool"]}}],
        })
        self.assertEqual({item["span_id"] for item in invoked["objects"]}, {"read-1", "read-2"})
        following = self.analysis.analyze("p1", {
            "scope": {"mode": "analysis"}, "filters": {"span_id": ["read-1"]},
            "steps": [{"relation": "next_source", "filters": {"kind": ["tool"]},
                       "text": {"query": "lark-cli task", "fields": ["input"]},
                       "text_all": [{"query": "ERROR", "fields": ["output"]}]}],
        })
        self.assertEqual(following["objects"][0]["span_id"], "read-2")
        self.assertIn("lark-cli task", following["objects"][0]["text_matches"][0]["snippet"])
        self.assertEqual(following["objects"][0]["path"][0]["evidence"]["basis"],
                         "source_ordinal")

    def test_groups_by_exact_text_match_for_document_cohorts(self):
        self.seeded()
        result = self.analysis.analyze("p1", {
            "scope": {"mode": "analysis"}, "filters": {"span_id": ["read-1"]},
            "text": {"query": r"/docs/[^\" ]+\.md", "mode": "regex", "fields": ["input"]},
            "group_by": ["text_match"],
        })
        self.assertEqual(result["groups"][0]["key"], {"text_match": "/docs/guide.md"})

    def test_composite_regex_uses_same_boundary_dialect_for_seed_and_text_all(self):
        self.seeded("regex-object")
        request = {
            "scope": {"mode": "analysis"}, "filters": {"span_id": ["read-1"]},
            "text": {"query": r"\blark-cli\b", "mode": "regex",
                     "regex_syntax": "portable", "fields": ["input"]},
            "text_all": [{"query": r"\yERROR\y", "mode": "regex", "fields": ["output"]}],
        }
        result = self.analysis.analyze("p1", request)
        self.assertEqual(result["coverage"]["matched_objects"], 1)
        matches = result["objects"][0]["text_matches"]
        self.assertEqual([match["effective_pattern"] for match in matches],
                         [r"\ylark-cli\y", r"\yERROR\y"])
        self.assertEqual([match["snippet"][match["match_ranges"][0]["start"]:
                                           match["match_ranges"][0]["end"]] for match in matches],
                         ["lark-cli", "ERROR"])
        native = self.analysis.analyze("p1", {**request, "text": {
            **request["text"], "regex_syntax": "postgresql_are"}})
        self.assertEqual(native["coverage"]["matched_objects"], 0)
        with self.assertRaises(ValueError):
            self.analysis.analyze("p1", {**request, "text": {
                "query": "[", "mode": "regex", "fields": ["input"]}})

    def test_filters_and_groups_objects_by_trace_metadata(self):
        document = self.seeded("metadata-run")
        harness = document["run"]["harness"]["name"]
        result = self.analysis.analyze("p1", {
            "scope": {"mode": "analysis"},
            "filters": {"skill_name": ["lark-cli"], "trace_harness": [harness]},
            "group_by": ["trace_harness"],
        })
        self.assertEqual(result["groups"][0]["key"], {"trace_harness": harness})
        self.assertEqual(result["objects"][0]["capture_coverage"]["tools"], "partial")

    def test_projects_model_usage_for_query_and_aggregation(self):
        self.seeded("usage-run")
        result = self.analysis.analyze("p1", {
            "scope": {"mode": "analysis"},
            "filters": {"requested_model": ["demo-model"]},
            "group_by": ["requested_model"],
        })
        self.assertEqual(result["coverage"]["matched_objects"], 4)
        group = result["groups"][0]
        self.assertEqual(group["key"], {"requested_model": "demo-model"})
        self.assertEqual(group["usage"]["input_tokens"]["sum"], 255)
        self.assertEqual(group["usage"]["output_tokens"]["sum"], 57)

    def test_filters_and_groups_by_capture_quality(self):
        self.seeded("quality-run")
        result = self.analysis.analyze("p1", {
            "scope": {"mode": "analysis"},
            "filters": {"coverage_tools": ["partial"], "skill_name": ["lark-cli"]},
            "group_by": ["coverage_tools"],
        })
        self.assertEqual(result["coverage"]["matched_objects"], 1)
        self.assertEqual(result["groups"][0]["key"], {"coverage_tools": "partial"})

    def test_model_context_never_exposes_spans_or_future_objects(self):
        self.seeded("visible-run")
        target = {"run_id": "visible-run", "revision": 1, "model_span_id": "model-1"}
        result = self.analysis.analyze("p1", {
            "scope": {"mode": "model_context", "visible_to": target},
            "text": {"query": "库存"},
        })
        self.assertEqual(result["scope"]["status"], "pass")
        self.assertTrue(result["objects"])
        self.assertEqual({item["object_kind"] for item in result["objects"]}, {"message", "context"})
        self.assertNotIn("assistant-1", {item["object_id"] for item in result["objects"]})
        self.assertEqual(self.analysis.analyze("p1", {
            "scope": {"mode": "model_context", "visible_to": target},
            "filters": {"skill_name": ["lark-cli"]},
        })["objects"], [])

    def test_model_context_uses_the_exact_historical_revision(self):
        first = self.seeded("historical-run")
        second = copy.deepcopy(first)
        second["document"].update(id="object-analysis-revision-2", revision=2,
                                  previous_document_id=first["document"]["id"])
        next(item for item in second["messages"] if item["id"] == "user-1")["content"][
            "value"] = "未来改写"
        descriptor = self.revisions.append(
            "p1", json.dumps(second).encode(), request_key=uuid.uuid4().hex,
            expected_previous=1)
        self.index.project("p1", "historical-run", descriptor["revision"]["revision"])
        result = self.analysis.analyze("p1", {
            "scope": {"mode": "model_context", "visible_to": {
                "run_id": "historical-run", "revision": 1, "model_span_id": "model-1"}},
            "text": {"query": "库存"},
        })
        self.assertTrue(result["objects"])
        self.assertEqual({item["revision"] for item in result["objects"]}, {1})

    def test_unknown_or_future_visibility_returns_no_data(self):
        document = availability_document("future-run")
        next(item for item in document["messages"] if item["id"] == "user-1")["content"][
            "available_at"]["at_ms"] = 1
        self.append(document)
        result = self.analysis.analyze("p1", {"scope": {"mode": "model_context", "visible_to": {
            "run_id": "future-run", "revision": 1, "model_span_id": "model-1"}}})
        self.assertEqual((result["scope"]["status"], result["objects"]), ("fail", []))
        self.assertEqual(result["coverage"]["matched_objects"], 0)

    def test_persists_and_reads_immutable_analysis_result(self):
        self.seeded()
        result = self.analysis.analyze("p1", {
            "scope": {"mode": "analysis"}, "filters": {"skill_name": ["lark-cli"]},
            "persist": {"analyzer": "workbench", "analyzer_version": "1"},
        })
        receipt = result["persisted"]
        stored = self.results.get("p1", "workbench", "1", receipt["result_id"])
        self.assertEqual(stored["result"]["query_digest"], result["query_digest"])
        repeated = self.analysis.analyze("p1", {
            "scope": {"mode": "analysis"}, "filters": {"skill_name": ["lark-cli"]},
            "persist": {"analyzer": "workbench", "analyzer_version": "1"},
        })
        self.assertEqual(repeated["persisted"]["result_id"], receipt["result_id"])
        self.assertEqual(self.repository.rows(
            "SELECT count(*) AS n FROM analysis_results WHERE analyzer='workbench'")[0]["n"], 1)
        listed = self.results.query("p1", {"analyzer": "workbench"})
        self.assertEqual([item["result_id"] for item in listed["items"]], [receipt["result_id"]])

    def test_persisted_result_binds_all_selected_revisions_even_without_matches(self):
        self.seeded("selected-a")
        self.seeded("selected-b")
        result = self.analysis.analyze("p1", {
            "scope": {"mode": "analysis"}, "filters": {"skill_name": ["missing-skill"]},
            "persist": {"analyzer": "workbench", "analyzer_version": "zero"},
        })
        self.assertEqual(result["coverage"]["matched_objects"], 0)
        self.assertEqual({item["run_id"] for item in result["coverage"]["resolved_revisions"]},
                         {"selected-a", "selected-b"})
        self.assertIsNotNone(result["persisted"])

    def test_resolves_positive_and_negative_trace_sets(self):
        self.seeded("skill-run")
        plain = availability_document("plain-run")
        self.append(plain)
        read = self.analysis.resolve_runs("p1", {
            "scope": {"mode": "analysis"},
            "contains": [{"filters": {"skill_name": ["lark-cli"]}}],
        })
        unread = self.analysis.resolve_runs("p1", {
            "scope": {"mode": "analysis"},
            "not_contains": [{"filters": {"skill_name": ["lark-cli"]}}],
        })
        self.assertEqual(read["members"], [{"run_id": "skill-run", "revision": 1}])
        self.assertEqual(unread["members"], [{"run_id": "plain-run", "revision": 1}])

    def test_funnel_returns_each_prefix_and_interruption_members(self):
        self.seeded("complete-chain")
        interrupted = self.seeded("interrupted-chain")
        with self.repository.engine.begin() as db:
            db.execute(text("""UPDATE trace_objects SET operation='other'
                WHERE project_id='p1' AND run_id='interrupted-chain' AND span_id='read-2'"""))
        result = self.analysis.funnel("p1", {
            "scope": {"mode": "analysis"},
            "seed": {"filters": {"skill_name": ["lark-cli"]}},
            "stages": [{"relation": "next_source", "filters": {"operation": ["read"]}}],
        })
        self.assertEqual([stage["matched_runs"] for stage in result["stages"]], [2, 1])
        self.assertEqual(result["interruptions"][0]["members"],
                         [{"run_id": "interrupted-chain", "revision": 1}])

    def test_batches_and_aggregates_five_thousand_traces_server_side(self):
        timestamp = "2026-09-20T00:00:00+00:00"
        traces, revisions, objects = [], [], []
        for index in range(5000):
            run = f"batch-{index:04d}"
            traces.append({"project": "p1", "run": run, "at": timestamp})
            revisions.append({"project": "p1", "run": run, "request": "batch:" + run,
                              "fingerprint": "f:" + run, "content": "sha256:" + "0" * 64,
                              "metadata": json.dumps({"run_id": run}), "at": timestamp})
            objects.extend([
                {"project": "p1", "run": run, "ordinal": 0, "object": "skill-0",
                 "skill": "lark-cli", "operation": "skill",
                 "duration": float(index % 1000), "refs": "[]"},
                {"project": "p1", "run": run, "ordinal": 1, "object": "tool-1",
                 "skill": None, "operation": "bash",
                 "duration": float((index + 1) % 1000), "refs": "[]"},
            ])
        with self.repository.engine.begin() as db:
            db.execute(text("INSERT INTO projects(project_id,name,created_at) VALUES('p1','P1',:at)"),
                       {"at": timestamp})
            db.execute(text("""INSERT INTO traces(
                project_id,run_id,latest_revision,created_at,updated_at)
                VALUES(:project,:run,1,:at,:at)"""), traces)
            db.execute(text("""INSERT INTO trace_revisions(
                project_id,run_id,revision,request_key,request_fingerprint,content_digest,size_bytes,
                media_type,format_version,derivation,metadata,projector_version,projection_state,
                identity_basis,created_at) VALUES(:project,:run,1,:request,:fingerprint,:content,0,
                'application/json','trace-hunter/2.0-draft.2','capture',:metadata,'trace-index/4',
                'complete','source',:at)"""), revisions)
            db.execute(text("""INSERT INTO trace_objects(
                project_id,run_id,revision,projector_version,object_ordinal,object_kind,object_id,
                source_ordinal,span_id,kind,name,operation,status,skill_name,duration_ms,source_refs,payload)
                VALUES(:project,:run,1,'trace-index/4',:ordinal,'span',:object,:ordinal,:object,'tool','Tool',
                :operation,'ok',:skill,:duration,:refs,'{}')"""), objects)
        result = self.analysis.analyze("p1", {
            "scope": {"mode": "analysis"}, "filters": {"kind": ["tool"]},
            "group_by": ["skill_presence"], "max_objects": 10000,
            "max_runs": 5000, "return_limit": 0,
        })
        self.assertEqual(result["coverage"]["distinct_runs"], 5000)
        self.assertEqual(result["coverage"]["matched_objects"], 10000)
        self.assertEqual({group["key"]["skill_presence"] for group in result["groups"]},
                         {"with_skill", "without_skill"})
        self.assertTrue(result["objects_truncated"])
        chain = self.analysis.analyze("p1", {
            "scope": {"mode": "analysis"}, "filters": {"skill_name": ["lark-cli"]},
            "steps": [{"relation": "next_source", "filters": {"operation": ["bash"]}}],
            "group_by": ["operation"], "max_objects": 5000, "max_runs": 5000,
            "return_limit": 0,
        })
        self.assertEqual(chain["coverage"]["matched_objects"], 5000)
        self.assertEqual(chain["groups"][0]["key"], {"operation": "bash"})
        run_set = self.analysis.resolve_runs("p1", {
            "scope": {"mode": "analysis"},
            "contains": [{"filters": {"skill_name": ["lark-cli"]}}],
        })
        self.assertEqual(run_set["member_count"], 5000)
        funnel = self.analysis.funnel("p1", {
            "scope": {"mode": "analysis"},
            "seed": {"filters": {"skill_name": ["lark-cli"]}},
            "stages": [{"relation": "next_source", "filters": {"operation": ["bash"]}}],
            "max_objects": 5000, "sample_limit": 2,
        })
        self.assertEqual([stage["matched_runs"] for stage in funnel["stages"]], [5000, 5000])


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "Dedicated PostgreSQL test database required")
class PostgresObjectAnalysisTests(ObjectAnalysisTests):
    def setUp(self):
        base = make_url(os.environ["TEST_DATABASE_URL"]).set(drivername="postgresql+psycopg")
        admin = create_engine(base, hide_parameters=True)
        schema = "test_object_analysis_" + uuid.uuid4().hex
        with admin.begin() as db:
            db.execute(text("CREATE SCHEMA " + schema))
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        location = base.update_query_dict({"options": "-csearch_path=" + schema}).render_as_string(
            hide_password=False)
        self.repository = Repository(location)
        self.repository.migrate()
        self.addCleanup(lambda: self._cleanup_postgres(admin, schema))
        self.revisions = TraceRevisions(
            self.repository, LocalContentStore(Path(self.directory) / "content"))
        self.index = TraceIndex(self.revisions)
        self.results = AnalysisResults(self.repository)
        self.analysis = ObjectAnalysis(self.repository, self.results)

    def _cleanup_postgres(self, admin, schema):
        self.repository.close()
        with admin.begin() as db:
            db.execute(text("DROP SCHEMA " + schema + " CASCADE"))
        admin.dispose()

    def test_postgres_object_queries_have_a_statement_timeout(self):
        with self.assertRaises(Exception):
            self.analysis._rows("SELECT pg_sleep(3)", {})


if __name__ == "__main__":
    unittest.main()
