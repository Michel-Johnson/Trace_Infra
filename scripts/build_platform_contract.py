"""Generate the offline platform companion contract; no live API registration."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION = "trace-hunter-platform/1.0-draft.1"
DOMAINS = ("versions", "environment", "messages", "model_requests", "contexts", "tool_io",
           "tool_timing", "model_timing", "model_usage", "events", "artifacts", "sources")
STATES = ("complete", "partial", "missing", "unknown", "redacted", "not_applicable")


def obj(props, required=None):
    return {"type": "object", "properties": props, "required": list(props if required is None else required), "additionalProperties": False}


def enum(*values):
    return {"enum": list(values)}


def ref(name):
    return {"$ref": "#/$defs/" + name}


def arr(items, minimum=0):
    return {"type": "array", "items": items, "minItems": minimum, "maxItems": 20000}


def nullable(schema):
    return {"anyOf": [schema, {"type": "null"}]}


ID = {"type": "string", "minLength": 1, "maxLength": 256}
TEXT = {"type": "string", "maxLength": 10000}
HASH = {"type": "string", "pattern": "^[a-f0-9]{64}$"}
COUNT = {"type": "integer", "minimum": 0}
POSITIVE = {"type": "integer", "minimum": 1}


def build():
    d = {}
    d["version_ref"] = obj({"id": ID, "revision": ID, "digest": HASH})
    d["extensions"] = {"type": "object", "patternProperties": {"^[a-z][a-z0-9_-]*\\.[a-zA-Z0-9_.-]+$": {}}, "additionalProperties": False}
    d["metadata"] = obj({"tags": {**arr(ID), "uniqueItems": True},
                         "labels": {"type": "object", "maxProperties": 100, "additionalProperties": {"type": ["string", "number", "boolean"]}},
                         "extensions": ref("extensions")})
    d["coverage_item"] = obj({"state": enum(*STATES), "recorded_count": COUNT,
                              "expected_count": nullable(COUNT), "reason": nullable(ID)})
    d["coverage_item"]["allOf"] = [
        {"if": {"properties": {"state": enum("missing", "redacted", "not_applicable")}},
         "then": {"properties": {"recorded_count": {"const": 0}, "reason": ID}}},
        {"if": {"properties": {"state": enum("partial", "unknown")}}, "then": {"properties": {"reason": ID}}},
        {"if": {"properties": {"state": {"const": "complete"}}}, "then": {"properties": {"expected_count": COUNT}}},
        {"if": {"properties": {"state": {"const": "not_applicable"}}}, "then": {"properties": {"expected_count": {"const": 0}}}},
    ]
    d["coverage"] = obj({domain: ref("coverage_item") for domain in DOMAINS})
    d["gap"] = obj({"domain": enum(*DOMAINS), "record_id": nullable(ID), "path": {"type": "string", "pattern": "^(/|$)"},
                    "state": enum("partial", "missing", "unknown", "redacted", "not_applicable"), "reason": ID, "detail": TEXT})
    d["environment"] = obj({
        "schema_version": {"const": VERSION}, "kind": {"const": "environment"}, "env_id": ID, "revision": ID,
        "isolation": enum("sandbox", "non_sandbox"), "runtime": enum("vm", "container", "host", "remote_service"),
        "runtime_ref": ref("version_ref"), "initial_state_ref": ref("version_ref"), "toolset_ref": ref("version_ref"),
        "reset": obj({"mode": enum("fresh_instance", "restore_snapshot", "script", "none"), "procedure_ref": nullable(ref("version_ref")), "reason": nullable(TEXT)}),
        "network": obj({"model_gateway": enum("allowed", "blocked"), "tool_egress": enum("blocked", "allowlist", "unrestricted"),
                        "allowlist": arr(ID), "external_state_policy": enum("frozen", "recorded_snapshot", "live")}),
        "constraints": obj({"resources_ref": ref("version_ref"), "permissions_ref": ref("version_ref"),
                            "time_policy": enum("frozen", "recorded_wall_clock"), "seed_policy": enum("fixed", "recorded", "unsupported")}),
        "metadata": ref("metadata")})
    d["case"] = obj({"query_id": ID, "revision": ID, "title": ID, "input_ref": ref("version_ref"), "goal_ref": ref("version_ref"),
                     "interaction": obj({"mode": enum("single", "scripted_multi", "interactive_multi"),
                                         "policy_ref": ref("version_ref"), "max_turns": POSITIVE}),
                     "oracle_ref": nullable(ref("version_ref")), "environment_refs": arr(ref("version_ref"), 1), "metadata": ref("metadata")})
    d["benchmark"] = obj({"schema_version": {"const": VERSION}, "kind": {"const": "benchmark"}, "id": ID, "revision": ID,
                          "cases": arr(ref("case"), 1),
                          "execution_policy": obj({"repeats": POSITIVE, "seeds": arr({"type": "integer"}), "timeout_s": POSITIVE,
                                                   "max_tokens": POSITIVE, "max_tool_calls": COUNT, "reset_per_attempt": {"const": True}}),
                          "default_plugin_refs": arr(ref("version_ref")), "metadata": ref("metadata")})
    d["component"] = obj({"name": ID, "version": nullable(ID), "digest": nullable(HASH)})
    d["run_metadata"] = obj({"schema_version": {"const": VERSION}, "kind": {"const": "run_metadata"},
                             "run_id": ID, "query_id": nullable(ID), "env_id": nullable(ID),
                             "trace_ref": ref("version_ref"), "case_ref": nullable(ref("version_ref")),
                             "environment_ref": nullable(ref("version_ref")), "benchmark_ref": nullable(ref("version_ref")),
                             "experiment_ref": nullable(ref("version_ref")), "sample_origin": enum("real", "synthetic"),
                             "versions": obj({name: nullable(ref("component")) for name in ("harness", "collector", "adapter", "code", "config", "prompt", "toolset")}),
                             "environment_match": obj({"status": enum("matched", "mismatched", "unverified"), "evidence_refs": arr(ref("version_ref")), "reason": nullable(TEXT)}),
                             "coverage": ref("coverage"), "gaps": arr(ref("gap")), "metadata": ref("metadata")})
    d["requirement"] = obj({"domain": enum(*DOMAINS), "minimum": enum("complete", "partial"),
                            "on_missing": enum("block", "skip", "allow_partial")})
    d["metric_definition"] = obj({"key": ID, "type": enum("number", "boolean", "string"), "unit": nullable(ID),
                                  "minimum": nullable({"type": "number"}), "maximum": nullable({"type": "number"}),
                                  "direction": enum("higher", "lower", "none"), "aggregation": enum("none", "mean", "sum", "pass_rate", "distribution")})
    d["plugin"] = obj({"schema_version": {"const": VERSION}, "kind": {"const": "plugin"}, "plugin_id": ID, "version": ID, "package_digest": HASH,
                       "trace_versions": arr(ID, 1), "entrypoint": obj({"kind": enum("external_program", "agent_skill"), "ref": ID}),
                       "scopes": arr(enum("run", "turn", "span", "comparison"), 1), "requirements": arr(ref("requirement")),
                       "permissions": {**arr(enum("trace_read", "artifact_read", "oracle_read", "result_submit", "model_gateway")), "uniqueItems": True},
                       "config_schema": {"type": "object"}, "metrics": arr(ref("metric_definition"), 1), "metadata": ref("metadata")})
    d["evidence"] = obj({"document_id": ID, "record_id": nullable(ID), "path": {"type": "string", "pattern": "^(/|$)"}})
    d["metric_value"] = obj({"key": ID, "status": enum("evaluated", "partial", "insufficient_data", "skipped", "error"),
                             "value": {"type": ["number", "boolean", "string", "null"]}, "reason": nullable(TEXT),
                             "coverage": ref("coverage_item"), "evidence": arr(ref("evidence"))})
    d["metric_value"]["allOf"] = [
        {"if": {"properties": {"status": enum("insufficient_data", "skipped", "error")}},
         "then": {"properties": {"value": {"type": "null"}, "reason": TEXT}}},
        {"if": {"properties": {"status": enum("evaluated", "partial")}},
         "then": {"properties": {"value": {"type": ["number", "boolean", "string"]}, "evidence": arr(ref("evidence"), 1)}}},
    ]
    d["evaluation_result"] = obj({"schema_version": {"const": VERSION}, "kind": {"const": "evaluation_result"},
                                  "job_id": ID, "attempt": POSITIVE, "plugin_ref": ref("version_ref"),
                                  "input_refs": arr(ref("version_ref"), 1), "config_digest": HASH,
                                  "dependency_refs": arr(ref("version_ref")),
                                  "scope": obj({"kind": enum("run", "turn", "span", "comparison"), "ids": arr(ID, 1)}),
                                  "status": enum("evaluated", "partial", "insufficient_data", "skipped", "error"),
                                  "metrics": arr(ref("metric_value"), 1),
                                  "findings": arr(obj({"code": ID, "severity": enum("info", "warning", "error"), "message": TEXT,
                                                       "evidence": arr(ref("evidence")), "repair_suggestion": nullable(TEXT)})),
                                  "execution_trace_ref": nullable(ref("version_ref")), "metadata": ref("metadata")})
    return {"$schema": "https://json-schema.org/draft/2020-12/schema", "$id": "urn:trace-hunter:platform:1.0-draft.1",
            "title": "Trace Hunter platform companion design — offline only", "$defs": d,
            "oneOf": [ref(name) for name in ("environment", "benchmark", "run_metadata", "plugin", "evaluation_result")]}


def examples():
    # Explicit placeholders denote synthetic definitions; never performance evidence.
    def version(id, revision="1"):
        return {"id": id, "revision": revision, "digest": "0" * 64}
    metadata = {"tags": ["contract-demo"], "labels": {"synthetic": True}, "extensions": {"example.note": "Synthetic contract values, not measured execution or verified content hashes."}}
    def root(kind):
        return {"schema_version": VERSION, "kind": kind, "metadata": metadata}
    environment = {**root("environment"), "env_id": "ledger-vm", "revision": "1", "isolation": "sandbox", "runtime": "vm",
                   "runtime_ref": version("vm-image"), "initial_state_ref": version("empty-ledger"), "toolset_ref": version("ledger-tools"),
                   "reset": {"mode": "restore_snapshot", "procedure_ref": version("reset-ledger"), "reason": None},
                   "network": {"model_gateway": "allowed", "tool_egress": "blocked", "allowlist": [], "external_state_policy": "frozen"},
                   "constraints": {"resources_ref": version("vm-resources"), "permissions_ref": version("test-user-permissions"), "time_policy": "frozen", "seed_policy": "fixed"}}
    case = {"query_id": "ledger-create", "revision": "1", "title": "搭建进货台账", "input_ref": version("ledger-input"), "goal_ref": version("ledger-goal"),
            "interaction": {"mode": "scripted_multi", "policy_ref": version("two-turn-script"), "max_turns": 2},
            "oracle_ref": version("private-ledger-oracle"), "environment_refs": [version("ledger-vm")], "metadata": metadata}
    benchmark = {**root("benchmark"), "id": "ledger-benchmark", "revision": "1", "cases": [case],
                 "execution_policy": {"repeats": 3, "seeds": [11, 12, 13], "timeout_s": 600, "max_tokens": 100000, "max_tool_calls": 100, "reset_per_attempt": True},
                 "default_plugin_refs": [version("official.task-success")]}
    coverage = {key: {"state": "complete", "recorded_count": 2, "expected_count": 2, "reason": None} for key in DOMAINS}
    coverage["events"] = {"state": "complete", "recorded_count": 0, "expected_count": 0, "reason": None}
    run = {**root("run_metadata"), "run_id": "demo-run", "query_id": "ledger-create", "env_id": "ledger-vm", "trace_ref": version("demo-trace"),
           "case_ref": version("ledger-create"), "environment_ref": version("ledger-vm"), "benchmark_ref": version("ledger-benchmark"),
           "experiment_ref": version("demo-experiment"), "sample_origin": "synthetic",
           "versions": {name: {"name": "example-" + name, "version": "1", "digest": "0" * 64} for name in ("harness", "collector", "adapter", "code", "config", "prompt", "toolset")},
           "environment_match": {"status": "matched", "evidence_refs": [version("environment-check")], "reason": None}, "coverage": coverage, "gaps": []}
    plugin = {**root("plugin"), "plugin_id": "official.task-success", "version": "1", "package_digest": "0" * 64,
              "trace_versions": ["trace-hunter/2.0-draft.1"], "entrypoint": {"kind": "external_program", "ref": "registry:official.task-success/1"},
              "scopes": ["run"], "requirements": [{"domain": "artifacts", "minimum": "complete", "on_missing": "block"}],
              "permissions": ["trace_read", "artifact_read", "oracle_read", "result_submit"], "config_schema": {"type": "object", "additionalProperties": False},
              "metrics": [{"key": "task_passed", "type": "boolean", "unit": None, "minimum": None, "maximum": None, "direction": "higher", "aggregation": "pass_rate"}]}
    result = {**root("evaluation_result"), "job_id": "demo-evaluation", "attempt": 1, "plugin_ref": version("official.task-success"),
              "input_refs": [version("demo-trace")], "config_digest": "0" * 64, "dependency_refs": [version("private-ledger-oracle")],
              "scope": {"kind": "run", "ids": ["demo-run"]}, "status": "evaluated",
              "metrics": [{"key": "task_passed", "status": "evaluated", "value": True, "reason": None,
                           "coverage": coverage["artifacts"], "evidence": [{"document_id": "demo-trace", "record_id": "final-state", "path": "/artifacts/0"}]}],
              "findings": [], "execution_trace_ref": None}
    import copy
    partial = copy.deepcopy(run)
    partial["coverage"]["artifacts"] = {"state": "missing", "recorded_count": 0, "expected_count": None, "reason": "not_recorded"}
    partial["gaps"] = [{"domain": "artifacts", "record_id": None, "path": "/artifacts", "state": "missing", "reason": "not_recorded", "detail": "终态未采集；任务是否完成尚不能判断。"}]
    unavailable = copy.deepcopy(result)
    unavailable["status"] = "insufficient_data"
    unavailable["metrics"][0].update(status="insufficient_data", value=None, reason="Final-state artifact is unavailable.", coverage=partial["coverage"]["artifacts"], evidence=[])
    return {"environment": environment, "benchmark": benchmark, "run-complete-declaration": run, "run-missing-artifacts": partial,
            "plugin-task-success": plugin, "result-evaluated": result, "result-insufficient-data": unavailable}


if __name__ == "__main__":
    schema_dir = ROOT / "contracts/drafts/platform-v1"
    examples_dir = ROOT / "examples/drafts/platform-v1"
    schema_dir.mkdir(parents=True, exist_ok=True)
    examples_dir.mkdir(parents=True, exist_ok=True)
    (schema_dir / "platform.schema.json").write_text(json.dumps(build(), ensure_ascii=False, indent=2) + "\n")
    for name, value in examples().items():
        (examples_dir / (name + ".json")).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
