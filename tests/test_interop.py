import copy
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from trace_hunter.analysis import analyze
from trace_hunter.contract_preview import validate
from trace_hunter.interop.common import Conversion, IngestError, base
from trace_hunter.interop.service import ADAPTERS, convert, prepare, check_bundle, describe, diagnostics, encoded


def atif():
    return {"schema_version": "ATIF-v1.7", "session_id": "source-session", "agent": {"name": "test-harness", "version": "1", "model_name": "test-model"},
            "steps": [
                {"step_id": 1, "source": "user", "message": "Read the config"},
                {"step_id": 2, "source": "agent", "message": "Checking", "llm_call_count": 1, "timestamp": "2026-01-01T00:00:10Z",
                 "tool_calls": [{"tool_call_id": "read-call", "function_name": "Read", "arguments": {"file_path": "skills/ledger/SKILL.md"}},
                                {"tool_call_id": "not-executed", "function_name": "Bash", "arguments": {"command": "not run"}}],
                 "observation": {"results": [{"source_call_id": "read-call", "content": "skill body"}]},
                 "metrics": {"prompt_tokens": 100, "completion_tokens": 20, "cached_tokens": 60}}]}


def atif_v18():
    return {"schema_version": "ATIF-v1.8", "session_id": "harbor-run", "agent": {"name": "terminus-2", "version": "2.0", "model_name": "gpt-test"},
            "steps": [
                {"step_id": 1, "source": "user", "message": "Create hello.txt"},
                {"step_id": 2, "source": "agent", "model_name": "gpt-test", "message": "Creating it",
                 "tool_calls": [{"tool_call_id": "call-1", "function_name": "bash_command", "arguments": {"keystrokes": "printf hello > hello.txt\\n", "duration": 0.1}}],
                 "observation": {"results": [{"source_call_id": "call-1", "content": "done"}]},
                 "metrics": {"prompt_tokens": 2, "completion_tokens": 2, "prompt_token_ids": [10, 11],
                             "completion_token_ids": [12, 13], "logprobs": [-0.1, -0.2], "cost_usd": 0.01}},
                {"step_id": 3, "source": "system", "message": "Performed context summarization and handoff.",
                 "observation": {"results": [{"subagent_trajectory_ref": [{"session_id": "summary-run", "trajectory_path": "summary.json", "extra": {"summary": "summary generation"}}]}]},
                 "extra": {"context_management": {"type": "compaction", "boundary": "replace"}}},
                {"step_id": 4, "source": "user", "message": "Continue from the handoff"}],
            "final_metrics": {"total_prompt_tokens": 5, "total_completion_tokens": 3, "total_cached_tokens": 0, "total_cost_usd": 0.02}}


def claude_export():
    return {
        "meta": {"session_id": "claude-session", "started_local": "2026-01-01T00:00:00Z", "title": "fixture", "model": "claude-fixture"},
        "phases": [{"id": 1, "name": "task", "start_local": "2026-01-01T00:00:00Z", "end_local": "2026-01-01T00:00:05Z"}],
        "timeline": [
            {"kind": "user_message", "is_meta": False, "phase": 1, "text": "inspect skill"},
            {"kind": "assistant_message", "phase": 1, "text": "running", "request_id": "request-1", "ts_utc": "2026-01-01T00:00:01Z",
             "usage": {"input_tokens": 100, "output_tokens": 20, "cache_read_input_tokens": 10},
             "tool_uses": [{"tool_use_id": "tool-1", "tool_name": "Skill", "input": {"skill": "lark-cli"}}]},
        ],
        "tool_calls": [{"tool_use_id": "tool-1", "tool_name": "Skill", "phase": 1,
                        "ts_utc": "2026-01-01T00:00:02Z", "ended_ts_utc": "2026-01-01T00:00:04Z", "duration_s": 2,
                        "is_error": False, "input": {"skill": "lark-cli"}, "result": "done"}],
    }


def doubao_export(sqlite=False):
    value = {
        "manifest": {"conversation_id": "doubao-session", "selected_model_label": "fixture",
                     "actual_submission_observed_at": "2026-01-01T00:00:00Z", "completed_observed_at": "2026-01-01T00:00:08Z",
                     "native_final_file_mtime": "2026-01-01T00:00:09Z"},
        "native_messages": [{"role": "user", "content": "inspect skill"}, {"role": "assistant", "content": "working"}],
        "tool_calls": [{"tool_call_id": "call-1", "name": "Read", "native_message_index": 1,
                        "arguments": {"file_path": "/skills/lark-cli/SKILL.md"}, "native_result": {"content": "skill body"}}],
        "runtime_tool_calls": [{"runtime_id": "runtime-1", "native_tool_call_id": "call-1", "name": "Read",
                                "timing": {"started_at": "2026-01-01T00:00:03Z", "ended_at": "2026-01-01T00:00:05Z", "duration_ms": 2000}}],
    }
    if sqlite:
        value["native_tool_calls"] = [{"id": "native-1", "name": "Skill", "arguments": {"skill": "lark-cli"},
                                       "result": "done", "runtime_match": {"timing": {"started_at": "2026-01-01T00:00:04Z",
                                                                                        "ended_at": "2026-01-01T00:00:07Z", "duration_ms": 3000}}}]
    return value


def doubao_turn_export():
    return {
        "case_id": "session_107__turn_0",
        "domain": "dashboard",
        "source": {"source_session_index": 107, "source_turn_index": 0},
        "raw_turn": {
            "turn_index": 0,
            "user_prompt": "分析 lark skill",
            "calls": [
                {"seq": 1, "start_time": "2026-09-14T15:08:32.701+08:00", "duration_ms": 100,
                 "tool_call_id": "read-1", "tool_name": "Read",
                 "call_content": {"file_path": r"C:\workspace\.skills\lark-base\SKILL.md"}, "result": "skill body"},
                {"seq": 2, "start_time": "2026-09-14T15:08:33.001+08:00", "duration_ms": 250,
                 "tool_call_id": "bash-1", "tool_name": "Bash", "command": "lark-cli base +table-list",
                 "result": "Command 'lark-cli base +table-list' exited with code 1. stderr: denied"},
                {"seq": 3, "start_time": "", "duration_ms": None, "tool_call_id": "", "tool_name": "Write",
                 "call_content": {"file_path": "/tmp/report.md"}, "result": "written"},
            ],
        },
    }


BINDING = {"run_id": "test-run", "query_id": "test-case", "env_id": "test-env"}


class InteropTests(unittest.TestCase):
    def test_claude_native_export_converts_directly_to_v2(self):
        raw = encoded(claude_export())
        converted, details = convert(raw, BINDING, "claude-export")
        self.assertEqual(converted.document["schema_version"], "trace-hunter/2.0-draft.2")
        self.assertEqual(converted.document["capture"]["collector"]["name"], "claude-export")
        self.assertNotIn("trace_hunter.v1_run", converted.document["extensions"])
        self.assertEqual(len(converted.document["turns"]), 1)
        tool = next(span for span in converted.document["spans"] if span["kind"] == "tool")
        self.assertEqual(tool["tool"]["skill"], {"name": "lark-cli", "action": "invoke"})
        self.assertEqual(tool["input"]["search_text_state"], "exact")
        self.assertIn("lark-cli", tool["input"]["search_text"])
        self.assertIn("ref", tool["input"])
        self.assertEqual(tool["timing"]["duration_ms"], 2000)
        self.assertEqual(details["mapping"]["model_requests"], 1)
        self.assertTrue(details["evidence"]["byte_hash_verified"])

    def test_claude_duplicate_user_snapshot_does_not_create_a_turn(self):
        source = claude_export()
        source["timeline"][0]["row"] = 7
        source["timeline"].insert(1, dict(source["timeline"][0]))
        converted, details = convert(encoded(source), BINDING, "claude-export")
        self.assertEqual(len(converted.document["turns"]), 1)
        self.assertIn("DUPLICATE_USER_SNAPSHOT", {item["code"] for item in details["issues"]})

    def test_doubao_runtime_export_converts_directly_to_v2(self):
        converted, details = convert(encoded(doubao_export()), BINDING, "doubao-export")
        self.assertEqual(details["mapping"]["variant"], "runtime")
        self.assertEqual(len(converted.document["turns"]), 1)
        tool = next(span for span in converted.document["spans"] if span["kind"] == "tool")
        self.assertEqual(tool["id"], "runtime-1")
        self.assertEqual(tool["tool"]["skill"], {"name": "lark-cli", "action": "load"})
        self.assertEqual(tool["timing"]["start_ms"], 3000)
        self.assertTrue(details["evidence"]["byte_hash_verified"])

    def test_doubao_proposal_without_execution_evidence_is_not_a_span(self):
        source = doubao_export()
        source["runtime_tool_calls"] = []
        source["tool_calls"][0].pop("native_result")
        converted, details = convert(encoded(source), BINDING, "doubao-export")
        self.assertEqual(converted.document["tool_calls"][0]["call_id"], "call-1")
        self.assertFalse(any(span["kind"] == "tool" for span in converted.document["spans"]))
        self.assertIn("TOOL_PROPOSAL_NOT_EXECUTED", {item["code"] for item in details["issues"]})

    def test_doubao_sqlite_export_converts_directly_to_v2(self):
        converted, details = convert(encoded(doubao_export(sqlite=True)), BINDING, "doubao-export")
        self.assertEqual(details["mapping"]["variant"], "sqlite")
        tool = next(span for span in converted.document["spans"] if span["kind"] == "tool")
        self.assertEqual(tool["id"], "native-1")
        self.assertEqual(tool["tool"]["skill"], {"name": "lark-cli", "action": "invoke"})
        self.assertEqual(tool["timing"]["duration_ms"], 3000)

    def test_doubao_sqlite_accepts_legacy_runtime_index(self):
        source = doubao_export(sqlite=True)
        source["native_tool_calls"][0]["runtime_match"] = 0
        converted, _ = convert(encoded(source), BINDING, "doubao-export")
        tool = next(span for span in converted.document["spans"] if span["kind"] == "tool")
        self.assertEqual(tool["timing"]["start_ms"], 3000)

    def test_doubao_turn_export_converts_executions_without_inventing_model_events(self):
        converted, details = convert(encoded(doubao_turn_export()), BINDING, "doubao-turn-export")
        document = converted.document
        self.assertEqual(document["schema_version"], "trace-hunter/2.0-draft.2")
        self.assertEqual(document["segments"][0]["session"], {"namespace": "doubao-turn-export", "id": "107"})
        self.assertEqual(len(document["turns"]), 1)
        self.assertEqual(document["messages"][0]["content"]["search_text"], "分析 lark skill")
        self.assertEqual(len(document["spans"]), 3)
        self.assertFalse(document["tool_calls"])
        self.assertFalse(document["contexts"])
        read, bash, write = document["spans"]
        self.assertEqual(read["tool"]["skill"], {"name": "lark-base", "action": "load"})
        self.assertEqual(read["timing"]["start_ms"], 0)
        self.assertEqual(read["timing"]["duration_ms"], 100)
        self.assertEqual(bash["status"], "error")
        self.assertEqual(bash["input"]["search_text"], "lark-cli base +table-list")
        self.assertEqual(write["status"], "unknown")
        self.assertNotIn("timing", write)
        self.assertEqual(document["capture"]["coverage"]["timing"], "partial")
        self.assertEqual(details["mapping"]["tool_executions"], 3)
        self.assertTrue(details["evidence"]["byte_hash_verified"])

    def test_doubao_turn_export_projects_explicit_error_and_run_metadata(self):
        source = doubao_turn_export()
        source.update(case_index=7, query="inspect failure", raw_trace_sha256="a" * 64)
        source["source"]["source_file"] = "captures/session.jsonl"
        source["raw_turn"]["calls"].insert(0, {
            "seq": 0, "start_time": "2026-09-14T15:08:32.601+08:00", "duration_ms": 10,
            "tool_call_id": "context-1", "tool_name": "Context",
            "command": "vendor-record-run-metadata", "result": "vendor-environment=prod vendor-status=failed",
        })
        source["raw_turn"]["calls"][2]["result"] = "ERROR permission denied"

        converted, details = convert(encoded(source), BINDING, "doubao-turn-export")
        document = converted.document
        self.assertEqual(document["run"]["status"], "failed")
        self.assertEqual(document["spans"][2]["status"], "error")
        self.assertEqual(document["extensions"]["doubao.turn_export"]["case_index"], 7)
        self.assertEqual(document["extensions"]["doubao.turn_export"]["query"], "inspect failure")
        self.assertEqual(document["extensions"]["doubao.turn_export"]["run_metadata"]["vendor-environment"], "prod")
        self.assertEqual(document["sources"][1], {
            "id": "raw-trace", "state": "external", "locator": "captures/session.jsonl",
            "media_type": "application/x-ndjson", "sha256": "a" * 64,
        })
        self.assertEqual(details["evidence"]["external_sources_not_read"], 1)

    def test_doubao_turn_export_projects_string_skill_shell_and_truncation(self):
        source = doubao_turn_export()
        source["raw_turn"]["calls"][0].update(tool_name="Skill", call_content="load lark-base", result="loaded")
        source["raw_turn"]["calls"][1].update(tool_name="Shell", result="result truncated...")

        converted, _ = convert(encoded(source), BINDING, "doubao-turn-export")
        skill_span, shell_span = converted.document["spans"][:2]
        self.assertEqual(skill_span["tool"]["skill"], {"name": "lark-base", "action": "load"})
        self.assertEqual(shell_span["tool"]["operation"], "bash")
        self.assertEqual(shell_span["output"]["state"], "partial")
        self.assertEqual(shell_span["output"]["search_text_state"], "truncated")

    def test_doubao_turn_export_merges_exact_duplicate_execution_and_keeps_sources(self):
        source = doubao_turn_export()
        source["raw_turn"]["calls"].append(copy.deepcopy(source["raw_turn"]["calls"][1]))
        converted, details = convert(encoded(source), BINDING, "doubao-turn-export")
        self.assertEqual(len(converted.document["spans"]), 3)
        bash = next(span for span in converted.document["spans"] if span["name"] == "Bash")
        self.assertEqual(len(bash["source_refs"]), 2)
        self.assertEqual(details["mapping"]["duplicates_merged"], 1)
        self.assertIn("DUPLICATE_TOOL_EXECUTION_MERGED", {item["code"] for item in details["issues"]})

    def test_doubao_turn_export_rejects_conflicting_duplicate_call_id(self):
        source = doubao_turn_export()
        conflict = copy.deepcopy(source["raw_turn"]["calls"][1])
        conflict["result"] = "different"
        source["raw_turn"]["calls"].append(conflict)
        with self.assertRaises(IngestError) as caught:
            convert(encoded(source), BINDING, "doubao-turn-export")
        self.assertEqual(caught.exception.issues[0]["code"], "TOOL_EXECUTION_ID_CONFLICT")

    def test_doubao_turn_export_synthesizes_stable_identity_for_missing_call_id(self):
        source = doubao_turn_export()
        first, _ = convert(encoded(source), BINDING, "doubao-turn-export")
        second, _ = convert(encoded(source), BINDING, "doubao-turn-export")
        self.assertEqual(first.document["spans"][2]["id"], second.document["spans"][2]["id"])
        self.assertEqual(first.document["spans"][2]["tool"]["call_id"], "source-call-2")

    def test_native_exports_require_explicit_format(self):
        with self.assertRaises(IngestError) as caught:
            convert(encoded(claude_export()), BINDING)
        self.assertEqual(caught.exception.issues[0]["code"], "FORMAT_UNSUPPORTED")

    def test_explicit_native_format_allows_vendor_schema_metadata(self):
        source = claude_export()
        source["schema_version"] = "vendor-export/7"
        converted, _ = convert(encoded(source), BINDING, "claude-export")
        self.assertEqual(converted.document["capture"]["collector"]["name"], "claude-export")

    def test_describe_lists_direct_native_adapters(self):
        formats = {item["format"] for item in describe()["adapters"]}
        self.assertTrue({"ATIF-v1.8", "claude-export", "doubao-export", "doubao-turn-export"}.issubset(formats))

    def test_atif_v18_maps_compaction_and_external_subagent_without_inference(self):
        converted, details = convert(encoded(atif_v18()), BINDING)
        document = converted.document
        self.assertEqual(document["capture"]["collector"]["name"], "ATIF-v1.8")
        self.assertEqual(document["capture"]["collector"]["version"], "1.0.0")
        self.assertEqual(document["segments"][0]["session"], {"namespace": "ATIF-v1.8", "id": "harbor-run"})
        self.assertEqual(len([span for span in document["spans"] if span["kind"] == "model_batch"]), 1)
        model = next(span for span in document["spans"] if span["kind"] == "model_batch")
        self.assertIsNone(model["model"]["request_count"])
        self.assertEqual(model["extensions"]["atif.metric_evidence"]["logprobs"]["count"], 2)
        tool = next(span for span in document["spans"] if span["kind"] == "tool")
        self.assertEqual(tool["tool"]["operation"], "bash")
        compaction = next(event for event in document["events"] if event["type"] == "compaction")
        self.assertEqual(compaction["context_change"]["before"], None)
        self.assertIn("summarization", compaction["context_change"]["summary"]["search_text"])
        self.assertEqual(document["sources"][1]["state"], "missing")
        self.assertEqual(document["sources"][1]["locator"], "summary.json")
        self.assertEqual(compaction["source_refs"][1], {"source_id": document["sources"][1]["id"], "pointer": ""})
        self.assertEqual(details["mapping"]["external_subagent_refs"], 1)
        self.assertEqual(details["mapping"]["compactions"], 1)
        self.assertTrue({"EXTERNAL_SUBAGENT_TRAJECTORY_UNREAD", "FINAL_METRICS_SCOPE_DIFFERS"}.issubset({item["code"] for item in details["issues"]}))

    def test_atif_v18_rejects_version_alias_and_nonsequential_steps(self):
        source = atif_v18(); source["schema_version"] = "ATIF-v1.7"
        with self.assertRaises(IngestError) as caught:
            convert(encoded(source), BINDING, "ATIF-v1.8")
        self.assertEqual(caught.exception.issues[0]["code"], "SOURCE_VERSION_MISMATCH")
        source = atif_v18(); source["steps"][3]["step_id"] = 5
        with self.assertRaises(IngestError) as caught:
            convert(encoded(source), BINDING)
        self.assertEqual(caught.exception.issues[0]["code"], "STEP_ID_NOT_SEQUENTIAL")

    def test_atif_v18_preserves_zero_usage_and_rejects_empty_external_path(self):
        source = atif_v18()
        source["steps"][1]["metrics"].update(prompt_tokens=0, completion_tokens=0,
                                                prompt_token_ids=[], completion_token_ids=[], logprobs=[])
        converted, _ = convert(encoded(source), BINDING)
        usage = converted.document["spans"][0]["model"]["usage"]
        self.assertEqual(usage["total"]["input_tokens"], 0)
        self.assertEqual(usage["total"]["output_tokens"], 0)
        source = atif_v18()
        source["steps"][2]["observation"]["results"][0]["subagent_trajectory_ref"][0]["trajectory_path"] = ""
        with self.assertRaises(IngestError) as caught:
            convert(encoded(source), BINDING)
        self.assertEqual(caught.exception.issues[0]["code"], "EXTERNAL_SUBAGENT_REF_INVALID")

    def test_native_adapter_bundles_are_idempotent_and_checkable(self):
        for source, source_format in ((claude_export(), "claude-export"),
                                      (doubao_export(), "doubao-export"),
                                      (doubao_export(sqlite=True), "doubao-export"),
                                      (doubao_turn_export(), "doubao-turn-export")):
            with self.subTest(source_format=source_format, sqlite="native_tool_calls" in source), tempfile.TemporaryDirectory() as folder:
                target = Path(folder) / "bundle"
                raw = encoded(source)
                first = prepare(raw, target, BINDING, source_format)
                second = prepare(raw, target, BINDING, source_format)
                self.assertFalse(first["reused"])
                self.assertTrue(second["reused"])
                self.assertTrue(check_bundle(target)["ok"])
                self.assertEqual((target / "source.json").read_bytes(), raw)

    def test_native_adapter_rejects_unzoned_origin_time(self):
        source = claude_export()
        source["meta"]["started_local"] = "2026-01-01T00:00:00"
        with self.assertRaises(IngestError) as caught:
            convert(encoded(source), BINDING, "claude-export")
        self.assertEqual(caught.exception.issues[0]["code"], "SOURCE_TIME_INVALID")

    def test_all_existing_inputs_preserve_tool_ids_times_and_token_totals(self):
        for path in (ROOT / "examples").glob("*.trace.json"):
            with self.subTest(path=path.name):
                original = json.loads(path.read_bytes())
                converted, details = convert(path.read_bytes())
                expected = analyze(original, [p["id"] for p in original["phases"]])
                self.assertEqual(details["summary"]["observed_tokens"], {k: expected["tokens"][k] for k in ("input_tokens", "output_tokens")})
                before = {s["id"]: s for s in original["spans"] if s["kind"] == "tool"}
                after = {s["id"]: s for s in converted.document["spans"] if s["kind"] == "tool"}
                self.assertEqual(before.keys(), after.keys())
                for sid in before:
                    for field in ("start_ms", "end_ms", "duration_ms"):
                        self.assertEqual(before[sid][field], after[sid]["timing"][field])
                self.assertTrue(details["evidence"]["byte_hash_verified"])

    def test_real_claude_fragments_are_not_counted_as_new_requests(self):
        converted, details = convert((ROOT / "examples/claude-orange.trace.json").read_bytes())
        self.assertEqual(details["mapping"]["source_model_records"], 119)
        self.assertEqual(details["mapping"]["normalized_model_requests"], 59)
        self.assertEqual(details["mapping"]["merged_fragments"], 60)
        self.assertIn("FRAGMENT_TIME_AMBIGUOUS", [i["code"] for i in details["issues"]])
        self.assertFalse(any(s.get("model", {}).get("context_id") for s in converted.document["spans"]))

    def test_real_doubao_independent_durations_are_preserved(self):
        converted, details = convert((ROOT / "examples/doubao-pro.trace.json").read_bytes())
        self.assertIn("TIMING_MEASUREMENTS_DIFFER", [i["code"] for i in details["issues"]])
        self.assertTrue(any(s.get("timing", {}).get("duration_basis") == "source_reported" for s in converted.document["spans"]))
        self.assertEqual(details["capabilities"]["tool_timing"]["status"], "partial")

    def test_tool_proposal_without_result_is_not_an_execution(self):
        converted, details = convert(encoded(atif()), BINDING)
        self.assertEqual(len(converted.document["tool_calls"]), 2)
        self.assertEqual(details["summary"]["tool_executions_observed"], 1)
        tool = next(s for s in converted.document["spans"] if s["kind"] == "tool")
        self.assertEqual(tool["tool"]["skill"], {"name": "ledger", "action": "load"})
        self.assertNotIn("timing", tool)

    def test_missing_call_count_is_opaque_batch_not_one(self):
        source = atif(); del source["steps"][1]["llm_call_count"]
        converted, details = convert(encoded(source), BINDING)
        self.assertEqual(details["summary"]["model_requests_observed"], 0)
        self.assertEqual(details["summary"]["unknown_model_batches"], 1)
        self.assertEqual(details["summary"]["observed_tokens"]["input_tokens"], 100)

    def test_unknown_tools_keep_their_name_and_content_reference(self):
        source = atif(); source["steps"][1]["tool_calls"][0]["function_name"] = "vendor.custom_operation"
        converted, details = convert(encoded(source), BINDING)
        tool = next(s for s in converted.document["spans"] if s["kind"] == "tool")
        self.assertEqual(tool["name"], "vendor.custom_operation")
        self.assertEqual(tool["tool"]["operation"], "other")
        self.assertEqual(tool["output"]["ref"]["pointer"], "/steps/1/observation/results/0/content")

    def test_repeated_results_do_not_become_fake_retries(self):
        source = atif(); source["steps"][1]["observation"]["results"] *= 2
        with self.assertRaises(IngestError) as caught:
            convert(encoded(source), BINDING)
        self.assertEqual(caught.exception.issues[0]["code"], "OBSERVATION_MULTIPLICITY_AMBIGUOUS")

    def test_copied_context_does_not_increase_usage(self):
        source = atif(); source["steps"][1]["is_copied_context"] = True
        _, details = convert(encoded(source), BINDING)
        self.assertIsNone(details["summary"]["observed_tokens"]["input_tokens"])
        self.assertEqual(details["summary"]["tool_executions_observed"], 0)

    def test_binding_is_explicit_and_existing_identity_cannot_be_overridden(self):
        with self.assertRaises(IngestError) as caught:
            convert(encoded(atif()))
        self.assertEqual(caught.exception.issues[0]["code"], "BINDING_REQUIRED")
        with self.assertRaises(IngestError) as caught:
            convert((ROOT / "examples/minimal.trace.json").read_bytes(), {"query_id": "other-case"})
        self.assertEqual(caught.exception.issues[0]["code"], "BINDING_CONFLICT")

    def test_package_is_idempotent_and_keeps_exact_source_bytes(self):
        raw = encoded(atif())
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "bundle"
            first = prepare(raw, target, BINDING)
            second = prepare(raw, target, BINDING)
            self.assertFalse(first["reused"]); self.assertTrue(second["reused"])
            self.assertEqual((target / "source.json").read_bytes(), raw)
            self.assertEqual((target / "source.json").stat().st_mode & 0o777, 0o600)
            self.assertTrue(check_bundle(target, "browse")["ok"])
            with self.assertRaises(IngestError):
                prepare(raw, target, {**BINDING, "env_id": "different-env"})
            self.assertEqual((target / "source.json").read_bytes(), raw)

    def test_package_tamper_and_symlinks_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "bundle"; prepare(encoded(atif()), target, BINDING)
            source = target / "source.json"; source.write_bytes(source.read_bytes() + b" ")
            with self.assertRaises(IngestError) as caught:
                check_bundle(target)
            self.assertEqual(caught.exception.issues[0]["code"], "BUNDLE_HASH_MISMATCH")
            source.unlink(); source.symlink_to(ROOT / "README.md")
            with self.assertRaises(IngestError) as caught:
                check_bundle(target)
            self.assertEqual(caught.exception.issues[0]["code"], "BUNDLE_FILE_INVALID")

    def test_source_pointer_verified_even_if_manifest_hashes_match(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "bundle"; prepare(encoded(atif()), target, BINDING)
            doc = json.loads((target / "trace.json").read_bytes())
            doc["spans"][0]["source_refs"][0]["pointer"] = "/not-present"
            data = encoded(doc); (target / "trace.json").write_bytes(data)
            manifest = json.loads((target / "manifest.json").read_bytes())
            manifest["files"]["trace.json"] = {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
            (target / "manifest.json").write_bytes(encoded(manifest))
            with self.assertRaises(IngestError) as caught:
                check_bundle(target)
            self.assertEqual(caught.exception.issues[0]["code"], "SOURCE_POINTER_MISSING")

    def test_agent_diagnostics_are_located_and_do_not_echo_payloads(self):
        converted, _ = convert(encoded(atif()), BINDING)
        converted.document["spans"][0]["status"] = "SECRET_SENTINEL"
        issues = diagnostics(converted.document)
        self.assertEqual(issues[0]["path"], "/spans/0/status")
        self.assertNotIn("SECRET_SENTINEL", json.dumps(issues))
        converted.document["spans"][0]["status"] = "unknown"
        converted.document["spans"][0]["model"]["context_id"] = "missing-context"
        issues = diagnostics(converted.document)
        self.assertEqual(issues[0]["code"], "REFERENCE_MISSING")
        self.assertEqual(issues[0]["path"], "/spans/0/model")
        self.assertTrue(issues[0]["fix"])

    def test_new_adapter_uses_registry_without_changing_core_contract(self):
        name = "synthetic.vendor/1"
        def adapter(raw, source, binding):
            doc = base(raw, binding, {"name": "vendor", "version": "1"}, name)
            doc["extensions"]["vendor.future_feature"] = source["opaque"]
            return Conversion(doc)
        ADAPTERS[name] = adapter
        try:
            converted, details = convert(encoded({"schema_version": name, "opaque": {"script": "never executed"}}), BINDING)
            self.assertEqual(converted.document["extensions"]["vendor.future_feature"]["script"], "never executed")
            self.assertTrue(details["ok"])
        finally:
            ADAPTERS.pop(name)

    def test_cli_returns_machine_errors_and_profile_failure(self):
        cli = [sys.executable, str(ROOT / "scripts/trace_agent.py")]
        result = subprocess.run(cli + ["prepare"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)["issues"][0]["code"], "ARGUMENT_INVALID")
        self.assertFalse(result.stderr)
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "bundle"; prepare(encoded(atif()), target, BINDING)
            result = subprocess.run(cli + ["check", str(target), "--require", "sft"], capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(json.loads(result.stdout)["issues"][0]["code"], "CAPABILITY_INCOMPLETE")

    def test_malformed_manifest_returns_structured_input_error(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "bundle"
            prepare(encoded(atif()), target, BINDING)
            (target / "manifest.json").write_bytes(encoded({"protocol": "interop-preview/1", "files": None}))
            result = subprocess.run([sys.executable, str(ROOT / "scripts/trace_agent.py"), "check", str(target)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertFalse(result.stderr)
            self.assertEqual(json.loads(result.stdout)["issues"][0]["code"], "BUNDLE_MANIFEST_INVALID")


if __name__ == "__main__":
    unittest.main()
