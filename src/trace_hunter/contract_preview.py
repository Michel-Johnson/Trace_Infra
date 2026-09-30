"""Executable RFC checks, isolated from the production API and database.

Does not dereference source locators, verify source bytes, infer missing facts,
perform JCS hashing, accept streaming updates, price tokens, or certify training.
"""
import argparse
import json
import math
from collections import defaultdict, deque
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

ROOT = Path(__file__).resolve().parents[2]
SCHEMA = json.loads((ROOT / "contracts/drafts/trace-v2/trace.schema.json").read_text())
VALIDATOR = Draft202012Validator(SCHEMA, format_checker=FormatChecker())
TABLES = ("sources", "clocks", "segments", "spans", "turns", "messages", "contexts", "events", "artifacts", "phases", "tool_calls")


def object_locations(value, path=""):
    """Walk protocol structures, never reinterpret raw payload or extensions."""
    if isinstance(value, dict):
        yield value, path
        for key, child in value.items():
            if key not in ("value", "extensions"):
                token = key.replace("~", "~0").replace("/", "~1")
                yield from object_locations(child, path + "/" + token)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from object_locations(child, path + "/" + str(index))


def objects(value):
    for item, _ in object_locations(value):
        yield item


class ContractError(ValueError):
    def __init__(self, errors):
        self.issues = errors.issues[:20]
        super().__init__("; ".join(errors[:20]))


class SemanticErrors(list):
    """Keep precise record locations without returning raw payloads to agents."""
    path = ""

    def __init__(self):
        super().__init__()
        self.issues = []

    def append(self, message):
        rules = (
            ("Missing ", "REFERENCE_MISSING", "修正该处引用，使其指向同包中存在的记录；未知关联可留空。"),
            ("Duplicate", "IDENTITY_DUPLICATE", "区分稳定身份与重传；同一次执行只保留一个归一化记录。"),
            ("Duration contradicts", "TIME_DURATION_CONFLICT", "派生时长应等于端点差；独立上报时长须声明 source_reported 并保留来源。"),
            ("Timestamp requires", "CLOCK_REQUIRED", "填写已声明的 clock_id，或移除无法确定时钟域的时间端点。"),
            ("Event timestamp", "CLOCK_REQUIRED", "为事件填写已声明的 clock_id。"),
            ("First response requires", "RESPONSE_BOUNDARY_REQUIRED", "声明首字节、首内容或首 token 的观测边界。"),
            ("Cache counters", "USAGE_SUBSET_CONFLICT", "缓存是 input_tokens 子项，不应超过总输入。"),
            ("Reasoning tokens", "USAGE_SUBSET_CONFLICT", "reasoning_tokens 是 output_tokens 子项。"),
            ("Complete usage", "USAGE_COMPLETENESS_CONFLICT", "补采主计数，或将 completeness 标为 partial；不要填零。"),
            ("Overlapping parent", "USAGE_OWNERSHIP_OVERLAP", "父级汇总与子级明细只能选择一个消费层级。"),
            ("Retry must", "RETRY_IDENTITY_CONFLICT", "retry_of 应指向同 agent、同 invocation 的更早 attempt。"),
        )
        code, fix = "RELATION_INVARIANT_FAILED", "检查此记录的身份、角色、关联方向与无环约束。"
        for prefix, candidate, instruction in rules:
            if message.startswith(prefix):
                code, fix = candidate, instruction
                break
        self.issues.append({"code": code, "path": self.path, "severity": "error", "classification": "invalid",
                            "message": "记录不符合跨字段或跨记录约束。", "fix": fix})
        super().append(message)


def acyclic(nodes, edges):
    degree = dict.fromkeys(nodes, 0)
    outgoing = defaultdict(list)
    for start, end in edges:
        if start in degree and end in degree:
            degree[end] += 1
            outgoing[start].append(end)
    ready = deque(n for n in degree if degree[n] == 0)
    visited = 0
    while ready:
        current = ready.popleft()
        visited += 1
        for child in outgoing[current]:
            degree[child] -= 1
            if degree[child] == 0:
                ready.append(child)
    return visited == len(degree)


def counts_for(usage):
    return [usage["total"]] if usage["accounting"] == "total" else [item["counts"] for item in usage["components"]]


def validate(document, *, schema_validator=None, schema_validated=False):
    # This also rejects non-finite values hidden inside raw inline JSON.
    try:
        json.dumps(document, allow_nan=False).encode("utf-8")
    except (ValueError, UnicodeError, TypeError) as error:
        raise ValueError("Not interoperable JSON") from error
    if not schema_validated:
        errors = [f"/{'/'.join(map(str, e.absolute_path))}: {e.message}" for e in (schema_validator if schema_validator is not None else VALIDATOR).iter_errors(document)]
        if errors:
            raise ValueError("; ".join(errors[:12]))
    errors = SemanticErrors()
    tables = {key: {item["id"]: item for item in document.get(key, [])} for key in TABLES}
    for key in TABLES:
        errors.path = "/" + key
        if len(tables[key]) != len(document.get(key, [])):
            errors.append(f"Duplicate ID in {key}")

    def exists(table, identifier):
        if identifier is not None and identifier not in tables[table]:
            errors.append(f"Missing {table} reference: {identifier}")

    for item, location in object_locations(document):
        errors.path = location
        if "source_id" in item and "pointer" in item:
            exists("sources", item["source_id"])
        for field, table in (("segment_id", "segments"), ("turn_id", "turns"), ("clock_id", "clocks"),
                             ("phase_id", "phases"), ("context_id", "contexts"), ("span_id", "spans"),
                             ("tool_span_id", "spans"), ("resumed_from_segment_id", "segments"),
                             ("proposal_id", "tool_calls"), ("model_span_id", "spans"), ("message_id", "messages")):
            if field in item:
                exists(table, item[field])
        for field, table in (("message_ids", "messages"), ("input_message_ids", "messages"),
                             ("output_message_ids", "messages"), ("span_ids", "spans")):
            for identifier in item.get(field, []):
                exists(table, identifier)
    for index, source in enumerate(tables["sources"].values()):
        errors.path = f"/sources/{index}"
        if source["state"] in ("available", "external") and source["sha256"] is None:
            errors.append("Available or external source requires an expected byte hash")
    doc = document["document"]
    errors.path = "/document"
    if (doc["revision"] == 1) != (doc["previous_document_id"] is None) or doc["id"] == doc["previous_document_id"]:
        errors.append("Invalid document revision lineage")
    if document["run"].get("lineage", {}).get("parent_run_id") == document["run"]["id"]:
        errors.path = "/run/lineage"
        errors.append("A fork must create a new run")
    if not acyclic(tables["segments"], [(s["id"], s.get("resumed_from_segment_id")) for s in tables["segments"].values()]):
        errors.path = "/segments"
        errors.append("Segment resume cycle")
    claimed_messages = set()
    for index, turn in enumerate(tables["turns"].values()):
        errors.path = f"/turns/{index}"
        for mid in turn["input_message_ids"]:
            message = tables["messages"].get(mid)
            if mid in claimed_messages:
                errors.append("One input message cannot create multiple turns")
            claimed_messages.add(mid)
            if message and (message["role"] != "user" or message.get("turn_id") != turn["id"]):
                errors.append("Turn input must be its own user message")
    for index, message in enumerate(tables["messages"].values()):
        errors.path = f"/messages/{index}"
        tool = tables["spans"].get(message.get("tool_span_id"))
        if tool and (tool["kind"] != "tool" or message["role"] != "tool"):
            errors.append("Tool result message must refer to an executed tool")
    for index, proposal in enumerate(tables["tool_calls"].values()):
        errors.path = f"/tool_calls/{index}"
        model = tables["spans"].get(proposal.get("model_span_id"))
        message = tables["messages"].get(proposal.get("message_id"))
        if model and model["kind"] not in ("model", "model_batch"):
            errors.append("Tool proposal model reference must identify a model observation")
        if message and message["role"] != "assistant":
            errors.append("Tool proposal message must be an assistant message")
    parents, identity, orders = [], set(), set()
    for index, span in enumerate(tables["spans"].values()):
        errors.path = f"/spans/{index}"
        exists("spans", span.get("parent_id"))
        parents.append((span.get("parent_id"), span["id"]))
        if "call" in span:
            key = (span["agent_id"], span["kind"], span["call"]["invocation_id"], span["call"]["attempt"])
            if key in identity:
                errors.append("Duplicate execution attempt; merge explicit observations before import")
            identity.add(key)
        if "order" in span:
            key = (span["order"]["stream_id"], span["order"]["sequence"])
            if key in orders:
                errors.append("Duplicate span sequence within producer stream")
            orders.add(key)
        t = span.get("timing", {})
        start, end, duration = t.get("start_ms"), t.get("end_ms"), t.get("duration_ms")
        if any(t.get(k) is not None for k in ("start_ms", "end_ms", "first_response_ms", "last_response_ms")) and t.get("clock_id") is None:
            errors.append("Timestamp requires an explicit clock domain")
        if start is not None and end is not None:
            if end < start:
                errors.append("Negative execution interval")
            if duration is not None and t.get("duration_basis") != "source_reported" and not math.isclose(end - start, duration, rel_tol=1e-9, abs_tol=0.001):
                errors.append("Duration contradicts interval endpoints")
        for field in ("first_response_ms", "last_response_ms"):
            point = t.get(field)
            if point is not None and ((start is not None and point < start) or (end is not None and point > end)):
                errors.append("Response boundary outside request interval")
        if t.get("first_response_ms") is not None and t.get("last_response_ms") is not None and t["last_response_ms"] < t["first_response_ms"]:
            errors.append("Last response precedes first response")
        if t.get("first_response_ms") is not None and "response_boundary" not in t:
            errors.append("First response requires a boundary definition")
        usage = span.get("model", {}).get("usage")
        if not usage:
            continue
        components = usage.get("components", [])
        if len({c["id"] for c in components}) != len(components):
            errors.append("Duplicate usage component")
        for counts in counts_for(usage):
            incoming, outgoing = counts["input_tokens"], counts["output_tokens"]
            cached = [counts.get(k) for k in ("cache_read_tokens", "cache_write_tokens")]
            if incoming is not None and sum(c for c in cached if c is not None) > incoming:
                errors.append("Cache counters are subsets of input tokens")
            reasoning = counts.get("reasoning_tokens")
            if outgoing is not None and reasoning is not None and reasoning > outgoing:
                errors.append("Reasoning tokens are a subset of output tokens")
            if usage["completeness"] == "complete" and (incoming is None or outgoing is None):
                errors.append("Complete usage requires total input and output counters")
        parent, visited = span.get("parent_id"), set()
        while parent in tables["spans"] and parent not in visited:
            visited.add(parent)
            ancestor = tables["spans"][parent]
            if ancestor.get("model", {}).get("usage"):
                errors.append("Overlapping parent and child accounting ownership")
            parent = ancestor.get("parent_id")
    if not acyclic(tables["spans"], parents):
        errors.path = "/spans"
        errors.append("Parent cycle")
    edges, invoked = [], set()
    for index, link in enumerate(document.get("links", [])):
        errors.path = f"/links/{index}"
        exists("spans", link["from"]); exists("spans", link["to"])
        start, end = tables["spans"].get(link["from"]), tables["spans"].get(link["to"])
        if not start or not end:
            continue
        if link["type"] == "invokes":
            if start["kind"] not in ("model", "model_batch") or end["kind"] != "tool":
                errors.append("Invokes must connect a model observation to a tool execution")
            if end["id"] in invoked:
                errors.append("Multiple invoking records for one tool")
            invoked.add(end["id"])
            edges.append((start["id"], end["id"]))
        else:
            edges.append((end["id"], start["id"]))
            if link["type"] == "retry_of":
                a, b = start.get("call", {}), end.get("call", {})
                if not a or not b or a["invocation_id"] != b["invocation_id"] or a["attempt"] <= b["attempt"] or start["kind"] != end["kind"] or start["agent_id"] != end["agent_id"]:
                    errors.append("Retry must reference an earlier attempt of the same invocation")
    if not acyclic(tables["spans"], edges):
        errors.path = "/links"
        errors.append("Causal dependency cycle")
    changes = []
    for index, event in enumerate(tables["events"].values()):
        errors.path = f"/events/{index}"
        if event.get("at_ms") is not None and event.get("clock_id") is None:
            errors.append("Event timestamp requires a clock domain")
        if "context_change" in event:
            change = event["context_change"]
            exists("contexts", change["before"]); exists("contexts", change["after"])
            changes.append((change["before"], change["after"]))
    if not acyclic(tables["contexts"], changes):
        errors.path = "/contexts"
        errors.append("Context transition cycle")
    if errors:
        raise ContractError(errors)
    return document


def union(intervals):
    right, total = None, 0
    for start, end in sorted(intervals):
        total += max(0, end - max(start, right if right is not None else start))
        right = end if right is None else max(right, end)
    return total


def summarize(document, *, schema_validator=None, validated=False):
    """Observed-only smoke projection, not the platform's analysis implementation."""
    if not validated:
        validate(document, schema_validator=schema_validator)
    spans = document["spans"]
    requests = [s for s in spans if s["kind"] in ("model", "model_batch")]
    usages = [s["model"]["usage"] for s in requests if s["model"]["usage"]]
    intervals = defaultdict(list)
    untimed = 0
    for s in (s for s in spans if s["kind"] == "tool"):
        t = s.get("timing", {})
        if t.get("start_ms") is not None and t.get("end_ms") is not None:
            intervals[t["clock_id"]].append((t["start_ms"], t["end_ms"]))
        else:
            untimed += 1
    counters = [counts for usage in usages for counts in counts_for(usage)]
    observed_tokens = {}
    for key in ("input_tokens", "output_tokens"):
        known = [c[key] for c in counters if c[key] is not None]
        observed_tokens[key] = sum(known) if known else None
    return {"run_id": document["run"]["id"], "turns_observed": len(document.get("turns", [])),
            "tool_executions_observed": sum(s["kind"] == "tool" for s in spans),
            "model_requests_observed": sum(s["model"]["request_count"] or 0 for s in requests),
            "unknown_model_batches": sum(s["model"]["request_count"] is None for s in requests),
            "model_records_with_usage": len(usages), "observed_tokens": observed_tokens,
            "tool_interval_union_ms_by_clock": {c: union(i) for c, i in intervals.items()},
            "tools_without_intervals": untimed, "capture_coverage": document["capture"]["coverage"],
            "note": "Observed records only. No global wall time, bill, quality score or training eligibility inferred."}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("files", nargs="+")
    args = parser.parse_args()
    for filename in args.files:
        print(json.dumps(summarize(json.loads(Path(filename).read_text())), ensure_ascii=False))
