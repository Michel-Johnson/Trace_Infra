"""Offline platform contract checks, not a production admission or plugin runner."""
import json
from pathlib import Path

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[2]
SCHEMA = json.loads((ROOT / "contracts/drafts/platform-v1/platform.schema.json").read_text())
VALIDATOR = Draft202012Validator(SCHEMA)


def validate(value):
    try:
        json.dumps(value, allow_nan=False)
    except (ValueError, TypeError, RecursionError):
        raise ValueError("PLATFORM_JSON_INVALID") from None
    errors = list(VALIDATOR.iter_errors(value))
    if errors:
        # Avoid including untrusted content in error text.
        raise ValueError("PLATFORM_SCHEMA_INVALID")
    kind = value["kind"]
    if kind == "run_metadata":
        for domain, item in value["coverage"].items():
            check_coverage(item)
            if item["state"] != "complete" and not any(g["domain"] == domain for g in value["gaps"]):
                raise ValueError("COVERAGE_GAP_REQUIRED")
            if item["state"] == "complete" and any(g["domain"] == domain for g in value["gaps"]):
                raise ValueError("COMPLETE_DOMAIN_HAS_GAPS")
        if value["case_ref"] and value["case_ref"]["id"] != value["query_id"]:
            raise ValueError("CASE_BINDING_CONFLICT")
        if value["environment_ref"] and value["environment_ref"]["id"] != value["env_id"]:
            raise ValueError("ENVIRONMENT_BINDING_CONFLICT")
        match = value["environment_match"]
        if match["status"] != "unverified" and (not value["environment_ref"] or not match["evidence_refs"]):
            raise ValueError("ENVIRONMENT_MATCH_EVIDENCE_REQUIRED")
        if value["coverage"]["versions"]["state"] == "complete" and any(
            component is None or component["version"] is None or component["digest"] is None
            for component in value["versions"].values()
        ):
            raise ValueError("COMPLETE_VERSIONS_MISSING_COMPONENT")
    elif kind == "environment":
        network = value["network"]
        if network["tool_egress"] == "allowlist" and not network["allowlist"]:
            raise ValueError("NETWORK_ALLOWLIST_REQUIRED")
        reset = value["reset"]
        if reset["mode"] in ("script", "restore_snapshot") and reset["procedure_ref"] is None:
            raise ValueError("RESET_PROCEDURE_REQUIRED")
        if reset["mode"] == "none" and not reset["reason"]:
            raise ValueError("RESET_NONE_REASON_REQUIRED")
    elif kind == "benchmark":
        policy = value["execution_policy"]
        if policy["seeds"] and len(policy["seeds"]) != policy["repeats"]:
            raise ValueError("REPEAT_SEED_COUNT_MISMATCH")
        keys = [(c["query_id"], c["revision"]) for c in value["cases"]]
        if len(set(keys)) != len(keys):
            raise ValueError("DUPLICATE_CASE_REVISION")
    elif kind == "plugin":
        Draft202012Validator.check_schema(value["config_schema"])
        keys = [m["key"] for m in value["metrics"]]
        if len(set(keys)) != len(keys):
            raise ValueError("DUPLICATE_METRIC_KEY")
        for metric in value["metrics"]:
            low, high = metric["minimum"], metric["maximum"]
            if low is not None and high is not None and low > high:
                raise ValueError("METRIC_RANGE_INVALID")
            if metric["type"] != "number" and (low is not None or high is not None):
                raise ValueError("NON_NUMERIC_METRIC_RANGE")
            allowed = {"number": {"none", "mean", "sum", "distribution"}, "boolean": {"none", "pass_rate", "distribution"}, "string": {"none", "distribution"}}
            if metric["aggregation"] not in allowed[metric["type"]]:
                raise ValueError("METRIC_AGGREGATION_INVALID")
    elif kind == "evaluation_result":
        keys = [m["key"] for m in value["metrics"]]
        if len(set(keys)) != len(keys):
            raise ValueError("DUPLICATE_METRIC_KEY")
        if value["status"] == "evaluated" and any(m["status"] != "evaluated" for m in value["metrics"]):
            raise ValueError("RESULT_STATUS_CONFLICT")
        if value["status"] in ("insufficient_data", "skipped", "error") and any(m["value"] is not None for m in value["metrics"]):
            raise ValueError("UNSCORED_RESULT_HAS_VALUE")
        documents = {r["id"] for r in value["input_refs"]}
        evidence = [e for m in value["metrics"] for e in m["evidence"]]
        evidence += [e for f in value["findings"] for e in f["evidence"]]
        if any(e["document_id"] not in documents for e in evidence):
            raise ValueError("EVIDENCE_OUTSIDE_INPUT_SCOPE")
        for metric in value["metrics"]:
            check_coverage(metric["coverage"])
            if metric["status"] == "evaluated" and metric["coverage"]["state"] != "complete":
                raise ValueError("FULL_SCORE_WITH_INCOMPLETE_COVERAGE")
    return value


def check_coverage(item):
    recorded, expected = item["recorded_count"], item["expected_count"]
    if expected is not None and recorded > expected:
        raise ValueError("COVERAGE_COUNT_CONFLICT")
    if item["state"] == "complete" and recorded != expected:
        raise ValueError("COMPLETE_COVERAGE_COUNT_CONFLICT")


def preflight(plugin, run):
    """Evaluate declared requirements only; storage must verify actual records too."""
    validate(plugin)
    validate(run)
    if plugin["kind"] != "plugin" or run["kind"] != "run_metadata":
        raise ValueError("PREFLIGHT_KIND_INVALID")
    issues, decision = [], "eligible"
    priority = {"eligible": 0, "partial": 1, "skipped": 2, "insufficient_data": 3}
    for requirement in plugin["requirements"]:
        item = run["coverage"][requirement["domain"]]
        state = item["state"]
        if state == "complete" or (state == "partial" and requirement["minimum"] == "partial"):
            next_decision = "partial" if state == "partial" else "eligible"
        elif state == "not_applicable" or requirement["on_missing"] == "skip":
            next_decision = "skipped"
        elif state == "partial" and item["recorded_count"] > 0 and requirement["on_missing"] == "allow_partial":
            next_decision = "partial"
        else:
            next_decision = "insufficient_data"
        if next_decision != "eligible":
            issues.append({"domain": requirement["domain"], "state": state, "decision": next_decision,
                           "path": "/coverage/" + requirement["domain"], "reason": item["reason"]})
        if priority[next_decision] > priority[decision]:
            decision = next_decision
    return {"decision": decision, "issues": issues, "basis": "declared_coverage_only", "live_execution": False}


def validate_result(result, plugin, inputs):
    """Bind output to a manifest and immutable inputs; no DB/auth/lease proof."""
    validate(result)
    validate(plugin)
    if result["kind"] != "evaluation_result" or plugin["kind"] != "plugin":
        raise ValueError("RESULT_KIND_INVALID")
    expected_plugin = {"id": plugin["plugin_id"], "revision": plugin["version"], "digest": plugin["package_digest"]}
    if result["plugin_ref"] != expected_plugin:
        raise ValueError("PLUGIN_VERSION_CONFLICT")
    if result["input_refs"] != inputs:
        raise ValueError("RESULT_INPUT_CONFLICT")
    definitions = {m["key"]: m for m in plugin["metrics"]}
    if {m["key"] for m in result["metrics"]} != set(definitions):
        raise ValueError("METRIC_SET_CONFLICT")
    if result["scope"]["kind"] not in plugin["scopes"]:
        raise ValueError("PLUGIN_SCOPE_UNSUPPORTED")
    for metric in result["metrics"]:
        if metric["value"] is None:
            continue
        definition, actual = definitions[metric["key"]], metric["value"]
        expected_type = definition["type"]
        if not ((expected_type == "number" and type(actual) in (int, float)) or
                (expected_type == "boolean" and type(actual) is bool) or
                (expected_type == "string" and isinstance(actual, str))):
            raise ValueError("METRIC_TYPE_CONFLICT")
        if expected_type == "number":
            if (definition["minimum"] is not None and actual < definition["minimum"]) or (definition["maximum"] is not None and actual > definition["maximum"]):
                raise ValueError("METRIC_OUT_OF_RANGE")
    return result
