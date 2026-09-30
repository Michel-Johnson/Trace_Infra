"""Structured Span query behavior and immutable revision boundaries."""
import copy
import json
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from trace_hunter.content import LocalContentStore
from trace_hunter.database import Repository
from trace_hunter.query.spans import SpanQuery
from trace_hunter.traces import TraceRevisions
from trace_hunter.traces.index import TraceIndex
from scripts.rebuild_span_indexes import rebuild

ROOT = Path(__file__).resolve().parents[1]


class SpanQueryTests(unittest.TestCase):
    def setUp(self):
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.repository = Repository(Path(self.directory) / "span-query.sqlite")
        self.addCleanup(self.repository.close)
        self.content = LocalContentStore(Path(self.directory) / "objects")
        self.revisions = TraceRevisions(self.repository, self.content)
        self.index = TraceIndex(self.revisions)
        self.query = SpanQuery(self.repository)
        self.document = json.loads((ROOT / "examples/drafts/trace-v2/multiturn-resume.json").read_text())
        self.document["run"]["id"] = "span-run"
        spans = {span["id"]: span for span in self.document["spans"]}
        spans["read-1"]["name"] = "Skill"
        spans["read-1"]["parent_id"] = "model-1"
        spans["read-1"]["tool"].update(skill={"name": "lark-cli", "action": "invoke"})
        spans["read-2"]["name"] = "Read"
        spans["read-2"]["tool"].update(skill={"name": "lark-cli", "action": "load"})

    def append(self, document=None, previous=0, indexed=True):
        document = copy.deepcopy(document or self.document)
        result = self.revisions.append("p1", json.dumps(document).encode(), request_key=uuid.uuid4().hex,
                                       expected_previous=previous)
        if indexed:
            self.index.project("p1", document["run"]["id"], previous + 1)
        return result

    def test_skill_duration_filter_projects_execution_facts(self):
        self.append()
        result = self.query.query("p1", {
            "filters": {"skill_name": ["lark-cli"], "min_duration_ms": 400},
            "fields": ["span_id", "skill_name", "skill_action", "duration_ms", "interval_duration_ms",
                       "source_reported_duration_ms", "parent_id", "call_id", "source_refs"],
        })
        self.assertEqual(result["items"], [{
            "span_id": "read-1", "skill_name": "lark-cli", "skill_action": "invoke",
            "duration_ms": 500, "interval_duration_ms": 500, "source_reported_duration_ms": None,
            "parent_id": "model-1", "call_id": "call-a",
            "source_refs": [{"source_id": "fixture", "pointer": ""}],
        }])
        self.assertEqual(result["consistency"], "live_keyset")
        self.assertIsNone(result["next_cursor"])

    def test_duration_order_and_cursor_are_stable(self):
        self.append()
        request = {"filters": {"skill_name": ["lark-cli"]}, "fields": ["span_id", "duration_ms"],
                   "order": "duration_desc", "limit": 1}
        first = self.query.query("p1", request)
        self.assertEqual(first["items"], [{"span_id": "read-1", "duration_ms": 500}])
        second = self.query.query("p1", {**request, "cursor": first["next_cursor"]})
        self.assertEqual(second["items"], [{"span_id": "read-2", "duration_ms": 300}])
        self.assertIsNone(second["next_cursor"])

    def test_window_returns_contiguous_spans_and_expands_attached_facts(self):
        self.document["spans"][0]["output"] = {"state": "complete", "value": "abcdefghijklmno"}
        self.append()
        result = self.query.window("p1", {
            "anchor": {"run_id": "span-run", "revision": 1, "span_id": "model-1"},
            "before": 0, "after": 2, "preview_chars": 8,
            "include": ["documents", "related_objects", "edges"],
        })
        self.assertEqual([item["span_id"] for item in result["spans"]],
                         ["model-1", "read-1", "read-2"])
        self.assertEqual(result["bounds"]["returned_before"], 0)
        self.assertTrue(result["bounds"]["has_more_after"])
        self.assertFalse(result["ordering"]["causal"])
        self.assertIn("context-1", {item["object_id"] for item in result["related_objects"]})
        self.assertIn("user-1", {item["object_id"] for item in result["related_objects"]})
        self.assertTrue(any(item["preview_truncated"] for item in result["documents"]))
        self.assertIn("context_message", {item["relation"] for item in result["edges"]})
        self.assertLess(len(json.dumps(result).encode()), 2 * 1024 * 1024)

    def test_window_handles_first_last_and_missing_anchors(self):
        self.append()
        first = self.query.window("p1", {"anchor": {
            "run_id": "span-run", "revision": 1, "span_id": "model-1"},
            "before": 100, "after": 0})
        self.assertEqual((first["bounds"]["returned_before"], first["bounds"]["has_more_before"]),
                         (0, False))
        last = self.query.window("p1", {"anchor": {
            "run_id": "span-run", "revision": 1, "span_id": "model-after-resume"},
            "before": 2, "after": 100})
        self.assertEqual((last["bounds"]["returned_after"], last["bounds"]["has_more_after"]),
                         (0, False))
        with self.assertRaises(KeyError):
            self.query.window("p1", {"anchor": {
                "run_id": "span-run", "revision": 1, "span_id": "missing"}})
        for invalid in ({"before": 101}, {"include": ["raw_content"]}):
            with self.assertRaises(ValueError):
                self.query.window("p1", {"anchor": {
                    "run_id": "span-run", "revision": 1, "span_id": "model-1"}, **invalid})

    def test_latest_revision_never_falls_back_to_old_projection(self):
        self.append()
        changed = copy.deepcopy(self.document)
        changed["spans"] = [span for span in changed["spans"] if span["id"] != "read-1"]
        changed["links"] = [link for link in changed["links"] if link["to"] != "read-1"]
        changed["messages"] = [message for message in changed["messages"] if message.get("tool_span_id") != "read-1"]
        for context in changed.get("contexts", []):
            context["message_ids"] = [message_id for message_id in context["message_ids"] if message_id != "tool-result-1"]
        self.append(changed, previous=1, indexed=False)
        self.assertEqual(self.query.query("p1", {"filters": {"skill_action": ["invoke"]}})["items"], [])
        history = self.query.query("p1", {"filters": {"skill_action": ["invoke"]}, "revisions": "all",
                                          "fields": ["revision", "span_id"]})
        self.assertEqual(history["items"], [{"revision": 1, "span_id": "read-1"}])

    def test_query_is_read_only_and_rejects_unsafe_options(self):
        self.append()
        with patch.object(self.revisions, "read", side_effect=AssertionError("no source read")), \
                patch.object(self.index, "project", side_effect=AssertionError("no rebuild")):
            self.query.query("p1", {"filters": {"skill_name": ["lark-cli"]}})
        invalid = [
            {"filters": {"body": ["secret"]}}, {"filters": {"min_duration_ms": -1}},
            {"filters": {"min_duration_ms": 10, "max_duration_ms": 1}},
            {"fields": ["input"]}, {"order": "DROP TABLE trace_index_records"},
            {"limit": 0}, {"cursor": "not-a-cursor"},
        ]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.query.query("p1", value)

    def test_explicit_rebuild_indexes_pending_revisions_idempotently(self):
        self.append(indexed=False)
        first = rebuild(type("StoreView", (), {"repository": self.repository, "revisions": self.revisions})())
        self.assertEqual((first["state"], first["rebuilt"]), ("complete", 1))
        self.assertEqual(len(self.query.query("p1", {"filters": {"skill_name": ["lark-cli"]}})["items"]), 2)
        second = rebuild(type("StoreView", (), {"repository": self.repository, "revisions": self.revisions})())
        self.assertEqual((second["state"], second["rebuilt"]), ("complete", 0))


if __name__ == "__main__":
    unittest.main()
