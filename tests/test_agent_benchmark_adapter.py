import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from trace_hunter.interop.common import IngestError
from trace_hunter.interop.service import check_bundle, convert, describe, encoded, prepare


BINDING = {"run_id": "run-1", "query_id": "query-1", "env_id": "benchmark-a-10-20260921"}
FORMAT = "agent-benchmark/1"


def envelope(trace, **extra):
    value = {
        "id": "A-TEST-1",
        "benchmark": "ExampleBench",
        "query": "What should the agent do?",
        "source_id": "source:1",
        "source_url": "https://example.invalid/source/1",
        "license": "test-only",
        "trace": trace,
    }
    value.update(extra)
    return value


class AgentBenchmarkAdapterTests(unittest.TestCase):
    def convert(self, source):
        return convert(encoded(source), BINDING, FORMAT)

    def test_describe_registers_explicit_format(self):
        formats = {item["format"] for item in describe()["adapters"]}
        self.assertIn(FORMAT, formats)

    def test_apb_maps_question_messages_proposals_and_executions(self):
        source = envelope({
            "messages": [
                {"role": "system", "content": "Follow the policy."},
                {"role": "user", "content": "What should the agent do?"},
                {"role": "assistant", "content": "", "tool_calls": [{
                    "id": "call-1", "type": "function",
                    "function": {"name": "search", "arguments": "{\"q\":\"docs\"}"},
                }]},
                {"role": "tool", "name": "search", "tool_call_id": "call-1", "content": "relevant body"},
                {"role": "assistant", "content": "Final answer"},
            ],
            "tools": [{"type": "function", "function": {"name": "search"}}],
            "answer_text": "Final answer",
        })
        converted, details = self.convert(source)
        document = converted.document
        self.assertEqual(document["messages"][0]["content"]["search_text"], source["query"])
        self.assertEqual(len(document["turns"]), 1)
        tool = next(span for span in document["spans"] if span["kind"] == "tool")
        self.assertEqual(tool["tool"]["call_id"], "call-1")
        self.assertEqual(tool["output"]["search_text"], "relevant body")
        self.assertEqual(details["mapping"]["unmatched_tool_proposals"], 0)

    def test_apb_non_assistant_tool_request_keeps_execution_without_fake_proposal(self):
        source = envelope({
            "messages": [
                {"role": "user", "content": "What should the agent do?"},
                {"role": "user", "content": "", "tool_calls": [{
                    "id": "call-env-1", "type": "function",
                    "function": {"name": "toggle_wifi", "arguments": "{\"enabled\":true}"},
                }]},
                {"role": "tool", "name": "toggle_wifi", "tool_call_id": "call-env-1", "content": "done"},
            ],
            "tools": [{"type": "function", "function": {"name": "toggle_wifi"}}],
            "answer_text": None,
        })
        converted, details = self.convert(source)
        tool = next(span for span in converted.document["spans"] if span["kind"] == "tool")
        self.assertIsNone(tool["tool"]["proposal_id"])
        self.assertEqual(tool["tool"]["call_id"], "call-env-1")
        self.assertEqual(converted.document["tool_calls"], [])
        self.assertEqual(details["mapping"]["non_assistant_tool_requests"], 1)
        self.assertIn("NON_ASSISTANT_TOOL_REQUESTS", {item["code"] for item in details["issues"]})

    def test_rootse_keeps_step_body_and_action_status_without_output_inference(self):
        source = envelope([{
            "index": "1", "thought": "inspect", "response": "run tests", "observation": "test output",
            "execution_time": "", "state": "", "extra_info": "",
            "action": [{"func_name": "bash", "arguments": {"command": "pytest"}, "call_ok": False}],
        }])
        converted, details = self.convert(source)
        step, tool = converted.document["spans"]
        self.assertEqual(step["output"]["search_text"], "test output")
        self.assertEqual(tool["status"], "error")
        self.assertEqual(tool["output"], {"state": "missing"})
        self.assertIn("AGGREGATE_OBSERVATION_UNATTRIBUTED", {item["code"] for item in details["issues"]})

    def test_rootse_accepts_legacy_single_action_shapes(self):
        source = envelope([
            {"index": 1, "thought": "inspect", "response": "run", "observation": "done",
             "action": {"tool": "execute_bash", "input": {"command": "pytest"}}},
            {"index": 2, "thought": "finish", "response": "submit", "observation": None,
             "action": "submit"},
            {"index": 3, "thought": "stop", "response": "", "observation": None,
             "action": ""},
        ])
        converted, details = self.convert(source)
        tools = [span for span in converted.document["spans"] if span["kind"] == "tool"]
        self.assertEqual([span["name"] for span in tools], ["execute_bash", "submit"])
        self.assertTrue(all(span["status"] == "unknown" for span in tools))
        self.assertEqual(details["mapping"]["tool_executions"], 2)

    def test_tel_preserves_ordered_raw_body_without_guessing_kind(self):
        source = envelope({"spans": [{"id": "s001", "raw": "analysis body"}, {"id": "s002", "raw": "[ERROR]: failed"}]})
        converted, details = self.convert(source)
        spans = converted.document["spans"]
        self.assertEqual([span["order"]["sequence"] for span in spans], [0, 1])
        self.assertEqual(spans[0]["output"]["search_text"], "analysis body")
        self.assertEqual(spans[1]["status"], "error")
        self.assertEqual(details["mapping"]["opaque_steps"], 2)

    def test_trail_flattens_nested_spans_and_normalizes_string_token_counts(self):
        child = {
            "span_id": "child", "parent_span_id": "root", "span_name": "Model.call", "span_kind": "Internal",
            "timestamp": "2026-09-21T00:00:01Z", "duration": "PT0.5S", "status_code": "Ok",
            "span_attributes": {"openinference.span.kind": "LLM", "input.value": "prompt body", "output.value": "answer body",
                                "llm.model_name": "model-a", "llm.token_count.prompt": "12", "llm.token_count.completion": "3"},
            "events": [], "logs": [], "links": [], "child_spans": [],
        }
        root = {
            "span_id": "root", "parent_span_id": None, "span_name": "run", "span_kind": "Internal",
            "timestamp": "2026-09-21T00:00:00Z", "duration": "PT2S", "status_code": "Unset",
            "span_attributes": {"openinference.span.kind": "CHAIN", "input.value": "run body"},
            "events": [], "logs": [], "links": [], "child_spans": [child],
        }
        converted, details = self.convert(envelope({"trace_id": "trace-1", "spans": [root]}))
        model = next(span for span in converted.document["spans"] if span["kind"] == "model")
        self.assertEqual(model["parent_id"], "trail-root")
        self.assertEqual(model["model"]["usage"]["total"], {"input_tokens": 12, "output_tokens": 3})
        self.assertEqual(model["timing"]["duration_ms"], 500)
        self.assertEqual(model["input"]["search_text"], "prompt body")
        self.assertEqual(details["mapping"]["timed_spans"], 2)

    def test_trail_preserves_duplicate_source_span_occurrences(self):
        duplicate = {
            "span_id": "same", "parent_span_id": None, "span_name": "step", "span_kind": "Internal",
            "timestamp": "2026-09-21T00:00:00Z", "duration": "PT1S", "status_code": "Unset",
            "span_attributes": {}, "events": [], "logs": [], "links": [], "child_spans": [],
        }
        converted, details = self.convert(envelope({"trace_id": "trace-1", "spans": [duplicate, duplicate]}))
        self.assertEqual([span["id"] for span in converted.document["spans"]],
                         ["trail-same-occurrence-1", "trail-same-occurrence-2"])
        self.assertIn("DUPLICATE_SOURCE_SPAN_ID", {item["code"] for item in details["issues"]})

    def test_ctb_maps_embedded_messages_shell_steps_and_other_file_body(self):
        trajectory = {
            "trajectory_format": "mini-swe-agent-1", "instance_id": "issue-1", "info": {},
            "messages": [
                {"role": "system", "content": "Use shell.", "timestamp": "2026-09-21T00:00:00Z"},
                {"role": "user", "content": "What should the agent do?", "timestamp": "2026-09-21T00:00:01Z"},
                {"role": "assistant", "content": "```sh\npytest\n```", "timestamp": "2026-09-21T00:00:02Z"},
                {"role": "user", "content": "<returncode>1</returncode>\n<output>failure body</output>", "timestamp": "2026-09-21T00:00:03Z"},
            ],
        }
        source = envelope({"files": [
            {"path": "case.traj.json", "content": json.dumps(trajectory)},
            {"path": "report.json", "content": "{\"summary\":\"report body\"}"},
        ]})
        converted, details = self.convert(source)
        document = converted.document
        shell = next(span for span in document["spans"] if span["kind"] == "tool")
        self.assertEqual(shell["status"], "error")
        self.assertIn("failure body", shell["output"]["search_text"])
        self.assertIn("report body", document["artifacts"][0]["content"]["search_text"])
        self.assertEqual(details["mapping"]["shell_executions"], 1)

    def test_ctb_maps_terminus_debug_calls_without_projecting_auxiliary_files(self):
        debug = {
            "litellm_call_id": "call-1", "model": "model-a", "custom_llm_provider": "provider-a",
            "messages": [{"role": "user", "content": "request body"}],
            "original_response": "response body",
        }
        source = envelope({"files": [
            {"path": "run/episode-1/debug.json", "content": json.dumps(debug)},
            {"path": "run/episode-1/response.txt", "content": "response body"},
        ]})
        converted, details = self.convert(source)
        model = next(span for span in converted.document["spans"] if span["kind"] == "model")
        self.assertIn("request body", model["input"]["search_text"])
        self.assertEqual(model["output"]["search_text"], "response body")
        self.assertEqual(details["mapping"]["embedded_model_calls"], 1)

    def test_unknown_variant_is_rejected_instead_of_guessed(self):
        for trace in ({"unknown": []}, {"spans": []}, []):
            with self.subTest(trace=trace), self.assertRaises(IngestError) as caught:
                self.convert(envelope(trace))
            self.assertEqual(caught.exception.issues[0]["code"], "SOURCE_SHAPE_UNSUPPORTED")

    def test_bundle_is_idempotent_and_all_source_refs_resolve(self):
        source = envelope({"spans": [{"id": "s001", "raw": "body"}]})
        raw = encoded(source)
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "bundle"
            first = prepare(raw, target, BINDING, FORMAT)
            second = prepare(raw, target, BINDING, FORMAT)
            checked = check_bundle(target)
            self.assertFalse(first["reused"])
            self.assertTrue(second["reused"])
            self.assertTrue(checked["evidence"]["byte_hash_verified"])
            self.assertGreater(checked["evidence"]["resolved_source_pointers"], 0)


if __name__ == "__main__":
    unittest.main()
