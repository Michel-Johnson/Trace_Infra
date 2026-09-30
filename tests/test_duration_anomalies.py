"""Skill duration analysis uses fixed structured facts and evidence."""
import copy
import json
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from trace_hunter.content import LocalContentStore
from trace_hunter.database import Repository
from trace_hunter.query.anomalies import DurationAnomalies
from trace_hunter.traces import TraceRevisions
from trace_hunter.traces.index import TraceIndex

ROOT = Path(__file__).resolve().parents[1]


class DurationAnomalyTests(unittest.TestCase):
    def setUp(self):
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.repository = Repository(Path(self.directory) / "anomalies.sqlite")
        self.addCleanup(self.repository.close)
        self.content = LocalContentStore(Path(self.directory) / "objects")
        self.revisions = TraceRevisions(self.repository, self.content)
        self.index = TraceIndex(self.revisions)
        self.analysis = DurationAnomalies(self.repository)
        self.template = json.loads((ROOT / "examples/drafts/trace-v2/multiturn-resume.json").read_text())

    def seed(self, count=8):
        for index in range(count):
            document = copy.deepcopy(self.template)
            document["run"]["id"] = f"duration-{index:02d}"
            span = next(item for item in document["spans"] if item["id"] == "read-1")
            span["tool"]["skill"] = {"name": "lark-cli", "action": "invoke"}
            duration = 10_000 if index == count - 1 else 100 + index
            span["timing"].update(start_ms=1000, end_ms=1000 + duration)
            result = self.revisions.append("p1", json.dumps(document).encode(), request_key=uuid.uuid4().hex)
            self.index.project("p1", result["revision"]["run_id"], 1)

    def test_finds_injected_skill_duration_outlier_with_revision_evidence(self):
        self.seed()
        result = self.analysis.analyze("p1", {"skill_name": "lark-cli", "skill_action": "invoke"})
        self.assertEqual(result["status"], "evaluated")
        self.assertEqual(result["sample_count"], 8)
        self.assertEqual([row["run_id"] for row in result["matches"]], ["duration-07"])
        self.assertEqual(result["matches"][0]["duration_ms"], 10_000)
        self.assertEqual(len(result["resolved_revisions"]), 8)
        self.assertEqual(result["matches"][0]["source_refs"], [{"source_id": "fixture", "pointer": ""}])

    def test_small_cohort_is_explicitly_insufficient(self):
        self.seed(3)
        result = self.analysis.analyze("p1", {"skill_name": "lark-cli", "min_samples": 5})
        self.assertEqual(result["status"], "insufficient_data")
        self.assertEqual(result["sample_count"], 3)
        self.assertIsNone(result["threshold_ms"])
        self.assertEqual(result["matches"], [])

    def test_analysis_does_not_read_source_or_mutate_index(self):
        self.seed(5)
        with patch.object(self.revisions, "read", side_effect=AssertionError("no source read")), \
                patch.object(self.index, "project", side_effect=AssertionError("no rebuild")):
            self.analysis.analyze("p1", {"skill_name": "lark-cli"})


if __name__ == "__main__":
    unittest.main()
