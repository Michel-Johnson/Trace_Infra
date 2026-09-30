"""Adversarial examples for RFC 0002, separate from the active HTTP protocol."""
import copy
import hashlib
import importlib.util
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / (name + ".py"))
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


draft = module("validate_trace_v2")
builder = module("build_trace_v2_draft")
fixtures = module("build_trace_v2_examples")


class DraftContractTests(unittest.TestCase):
    def setUp(self):
        self.doc = json.loads((ROOT / "examples/drafts/trace-v2/multiturn-resume.json").read_text())

    def rejects(self, text):
        with self.assertRaisesRegex(ValueError, text):
            draft.validate(self.doc)

    def test_generated_schema_and_examples_match_committed_files(self):
        self.assertEqual(builder.build(), draft.SCHEMA)
        draft.Draft202012Validator.check_schema(draft.SCHEMA)
        for name, example in fixtures.examples().items():
            with self.subTest(name=name):
                path = ROOT / "examples/drafts/trace-v2" / (name + ".json")
                self.assertEqual(example, json.loads(path.read_text()))
                draft.validate(example)

    def test_synthetic_source_hash_matches_fixture_bytes(self):
        raw = (ROOT / "examples/drafts/trace-v2/synthetic-source.json").read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), self.doc["sources"][0]["sha256"])

    def test_parallel_shared_request_retry_and_compaction_accounting(self):
        summary = draft.summarize(self.doc)
        self.assertEqual(summary["turns_observed"], 2)
        self.assertEqual(summary["model_requests_observed"], 4)
        self.assertEqual(summary["tool_executions_observed"], 4)
        self.assertEqual(summary["observed_tokens"], {"input_tokens": 255, "output_tokens": 57})
        self.assertEqual(summary["tool_interval_union_ms_by_clock"], {"clock-1": 700})
        self.assertEqual(summary["tools_without_intervals"], 1)

    def test_missing_usage_stays_null(self):
        summary = draft.summarize(fixtures.examples()["minimal-partial"])
        self.assertEqual(summary["observed_tokens"], {"input_tokens": None, "output_tokens": None})

    def test_unknown_batch_does_not_become_one_model_request(self):
        summary = draft.summarize(fixtures.examples()["opaque-batch"])
        self.assertEqual(summary["model_requests_observed"], 0)
        self.assertEqual(summary["unknown_model_batches"], 1)
        self.assertEqual(summary["observed_tokens"]["input_tokens"], 200)

    def test_fork_consumption_excludes_inherited_parent(self):
        summary = draft.summarize(fixtures.examples()["fork-at-checkpoint"])
        self.assertEqual(summary["observed_tokens"]["input_tokens"], 30)
        self.assertEqual(summary["model_requests_observed"], 1)

    def test_two_clock_domains_are_not_merged(self):
        tool = copy.deepcopy(self.doc["spans"][1])
        tool.update(id="remote-tool", segment_id="segment-2", order={"stream_id": "remote", "sequence": 0})
        tool["timing"]["clock_id"] = "clock-2"
        self.doc["spans"].append(tool)
        self.assertEqual(draft.summarize(self.doc)["tool_interval_union_ms_by_clock"], {"clock-1": 700, "clock-2": 500})

    def test_usage_total_and_components_cannot_both_be_additive(self):
        self.doc["spans"][-1]["model"]["usage"]["total"] = {"input_tokens": 25, "output_tokens": 5}
        self.rejects("usage")

    def test_cache_is_a_subset(self):
        self.doc["spans"][0]["model"]["usage"]["total"]["cache_read_tokens"] = 101
        self.rejects("Cache counters")

    def test_duplicate_attempt_is_not_a_new_request(self):
        duplicate = copy.deepcopy(self.doc["spans"][0])
        duplicate["id"] = "instrumentation-wrapper-copy"
        duplicate["order"]["sequence"] = 999
        self.doc["spans"].append(duplicate)
        self.rejects("Duplicate execution attempt")

    def test_wrapper_usage_cannot_overlap_child_ownership(self):
        self.doc["spans"][-1]["parent_id"] = "model-1"
        self.rejects("accounting ownership")

    def test_retry_requires_same_invocation(self):
        self.doc["spans"][6]["call"]["invocation_id"] = "unrelated-invocation"
        self.rejects("Retry must reference")

    def test_parent_and_resume_cycles_rejected(self):
        self.doc["spans"][0]["parent_id"] = "model-1"
        self.rejects("Parent cycle")
        del self.doc["spans"][0]["parent_id"]
        self.doc["segments"][0]["resumed_from_segment_id"] = "segment-2"
        self.rejects("Segment resume cycle")

    def test_dangling_context_or_source_reference_rejected(self):
        self.doc["spans"][0]["model"]["context_id"] = "does-not-exist"
        self.rejects("Missing contexts")
        self.doc["spans"][0]["model"]["context_id"] = "context-1"
        self.doc["spans"][0]["source_refs"][0]["source_id"] = "does-not-exist"
        self.rejects("Missing sources")

    def test_repeated_history_does_not_create_another_turn(self):
        self.doc["turns"][1]["input_message_ids"] = ["user-1"]
        self.rejects("multiple turns")

    def test_inconsistent_duration_rejected(self):
        self.doc["spans"][0]["timing"]["duration_ms"] = 9999
        self.rejects("Duration contradicts")

    def test_timestamp_requires_domain_and_response_requires_boundary(self):
        self.doc["spans"][0]["timing"]["clock_id"] = None
        self.rejects("clock domain")
        self.doc["spans"][0]["timing"]["clock_id"] = "clock-1"
        del self.doc["spans"][0]["timing"]["response_boundary"]
        self.rejects("boundary definition")

    def test_wait_classification_is_explicit(self):
        self.assertEqual(self.doc["spans"][4]["wait"]["reason"], "human_input")
        del self.doc["spans"][4]["wait"]
        self.rejects("wait")

    def test_raw_payload_is_not_reinterpreted_as_protocol_references(self):
        self.doc["spans"][1]["input"] = {"state": "complete", "value": {"source_id": "not-a-protocol-id", "pointer": "/", "span_id": "opaque"}}
        draft.validate(self.doc)

    def test_nonfinite_raw_json_rejected(self):
        self.doc["spans"][1]["input"] = {"state": "complete", "value": float("nan")}
        self.rejects("Not interoperable JSON")

    def test_redaction_does_not_allow_inline_content(self):
        self.doc["spans"][3]["output"]["value"] = "should not be present"
        self.rejects("output")

    def test_revision_requires_previous_document(self):
        self.doc["document"]["revision"] = 2
        self.rejects("revision lineage")

    def test_complete_usage_requires_known_primary_counters(self):
        self.doc["spans"][0]["model"]["usage"]["total"]["input_tokens"] = None
        self.rejects("Complete usage")

    def test_live_protocol_does_not_accept_draft_version(self):
        sys.path.insert(0, str(ROOT / "src"))
        from trace_hunter.protocol import InvalidTrace, validate
        with self.assertRaises(InvalidTrace):
            validate(self.doc)


if __name__ == "__main__":
    unittest.main()
