import copy
import json
import tempfile
import unittest
from pathlib import Path

from trace_hunter.content import LocalContentStore
from trace_hunter.database import Repository
from trace_hunter.query.advanced import AdvancedQuery
from trace_hunter.query.object_analysis import ObjectAnalysis
from trace_hunter.traces import TraceRevisions
from trace_hunter.traces.index import TraceIndex


ROOT = Path(__file__).resolve().parents[1]


class AdvancedQueryTests(unittest.TestCase):
    def setUp(self):
        self.temp = self.enterContext(tempfile.TemporaryDirectory())
        root = Path(self.temp)
        self.repository = Repository(root / "db.sqlite")
        self.addCleanup(self.repository.close)
        self.revisions = TraceRevisions(self.repository, LocalContentStore(root / "content"))
        self.index = TraceIndex(self.revisions)
        self.query = AdvancedQuery(self.repository)
        self.document = json.loads((ROOT / "examples/drafts/trace-v2/multiturn-resume.json").read_text())
        self.document["schema_version"] = "trace-hunter/2.0-draft.2"
        self.document["run"].update(id="advanced-run", attributes={"release": "2026.09", "region": "us"})
        for clock in self.document["clocks"]:
            clock.update(kind="wall", origin_at="2026-09-21T10:00:00+00:00")
        for segment in self.document["segments"]:
            segment["session"] = {"namespace": "tests", "id": "session-1"}
        self.document["spans"][0]["attributes"] = {"prompt.version": "v2"}
        parent = "model-1"
        for index in range(1, 7):
            span_id = f"chain-{index}"
            self.document["spans"].append({"id": span_id, "kind": "other", "name": span_id,
                "agent_id": "agent-main", "segment_id": self.document["segments"][0]["id"],
                "turn_id": None, "phase_id": None, "parent_id": parent, "status": "ok",
                "source_refs": [{"source_id": "fixture", "pointer": ""}]})
            parent = span_id
        self.revisions.append("p1", json.dumps(self.document).encode(), request_key="advanced", expected_previous=0)
        self.index.project("p1", "advanced-run", 1)

    def test_attributes_time_snapshot_metrics_and_sessions(self):
        selection = {
            "attributes": [{"path": "prompt.version", "value": "v2"}],
            "trace_attributes": [{"path": "release", "value": "2026.09"}],
            "time": {"from": "2026-09-21T10:00:00Z", "to": "2026-09-21T11:00:00Z"},
        }
        result = self.query.query("p1", {**selection,
            "fields": ["run_id", "revision", "object_id", "attributes", "trace_attributes", "start_at", "session_id"],
        })
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["session_id"], "session-1")
        frozen = result["snapshot"]
        second = copy.deepcopy(self.document); second["document"]["revision"] = 2
        second["document"]["previous_document_id"] = self.document["document"]["id"]
        second["document"]["id"] += "-2"
        self.revisions.append("p1", json.dumps(second).encode(), request_key="advanced-2", expected_previous=1,
                              derivation="supplement")
        self.index.project("p1", "advanced-run", 2)
        replay = self.query.query("p1", {**selection, "snapshot": frozen,
                                          "fields": ["run_id", "revision", "object_id", "attributes", "trace_attributes", "start_at", "session_id"]})
        self.assertTrue(all(item["revision"] == 1 for item in replay["items"]))
        metrics = self.query.metrics("p1", {"query": {**selection, "snapshot": frozen}, "metrics": ["count", "error_rate", "p95"],
                                             "group_by": ["kind", "trace_attributes.release"], "interval_seconds": 300})
        self.assertTrue(metrics["groups"])
        sessions = self.query.sessions("p1", {**selection, "snapshot": frozen,
                                                "fields": ["run_id", "revision", "object_id", "attributes", "trace_attributes", "start_at", "session_id"]})
        self.assertEqual(sessions["items"][0]["session_id"], "session-1")
        timeline = self.query.session_timeline("p1", {
            "session_namespace": "tests", "session_id": "session-1",
            "fields": ["object_kind", "object_id", "span_id", "start_at", "session_id"],
        })
        self.assertTrue(any(item["object_kind"] == "message" for item in timeline["items"]))
        self.assertTrue(all(item["span_id"] and item["start_at"] for item in timeline["items"]))

    def test_lineage_has_no_four_step_limit_and_facets_custom_attributes(self):
        result = self.query.lineage("p1", {"anchor": {"run_id": "advanced-run", "revision": 1,
                                                       "object_kind": "span", "object_id": "chain-6"},
                                                   "direction": "ancestors"})
        self.assertGreaterEqual(max(item["depth"] for item in result["nodes"]), 6)
        self.assertFalse(result["partial"])
        facet = ObjectAnalysis(self.repository).facets("p1", {"field": "attributes.prompt.version"})
        self.assertEqual(facet["items"][0]["value"], "v2")

    def test_cursor_remains_valid_when_snapshot_is_reused(self):
        first = self.query.query("p1", {"limit": 1, "fields": ["run_id", "object_id"]})
        second = self.query.query("p1", {"limit": 1, "fields": ["run_id", "object_id"],
                                         "snapshot": first["snapshot"], "cursor": first["next_cursor"]})
        self.assertNotEqual(first["items"], second["items"])


if __name__ == "__main__":
    unittest.main()
