"""Create synthetic RFC fixtures only, outside the live example import directory."""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "examples/drafts/trace-v2"
SOURCE_BYTES = b'{"synthetic":true,"purpose":"Trace Hunter v2 contract examples; no real execution"}\n'
REF = {"source_id": "fixture", "pointer": ""}


def content(value):
    return {"state": "complete", "value": value}


def usage(incoming, outgoing, **parts):
    return {"accounting": "total", "completeness": "complete", "source_refs": [REF],
            "total": {"input_tokens": incoming, "output_tokens": outgoing, **parts}}


def base(identifier):
    return {"schema_version": "trace-hunter/2.0-draft.1",
            "document": {"id": identifier + "-doc-1", "revision": 1, "previous_document_id": None, "sealed": True},
            "run": {"id": identifier, "query_id": "synthetic-ledger", "env_id": "synthetic-vm-v1",
                    "harness": {"name": "Synthetic harness", "version": "1.0"}, "status": "completed",
                    "environment": {"isolation": "sandbox", "network_access": "blocked"}},
            "capture": {"collector": {"name": "synthetic-contract-fixture", "version": "1"},
                        "coverage": {"tools": "partial", "model_requests": "partial", "messages": "partial", "contexts": "partial", "timing": "partial"},
                        "notes": ["All values are synthetic and illustrate contract semantics, not measured performance."]},
            "sources": [{"id": "fixture", "sha256": hashlib.sha256(SOURCE_BYTES).hexdigest(), "locator": "synthetic-source.json", "media_type": "application/json", "state": "available"}],
            "segments": [{"id": "segment-1", "session": None, "source_refs": [REF]}],
            "clocks": [{"id": "clock-1", "kind": "monotonic", "origin_at": None, "uncertainty_ms": 0}],
            "spans": [], "messages": [], "turns": [], "contexts": [], "links": [], "events": []}


def span(identifier, kind, seq, start=None, end=None, segment="segment-1", clock="clock-1", **fields):
    item = {"id": identifier, "kind": kind, "name": identifier, "agent_id": "root", "segment_id": segment,
            "status": "ok", "source_refs": [REF], "order": {"stream_id": "collector-main", "sequence": seq}, **fields}
    if start is not None or end is not None:
        item["timing"] = {"clock_id": clock, "start_ms": start, "end_ms": end}
    return item


def model(identifier, seq, start, end, incoming, outgoing, context_id, **fields):
    return span(identifier, "model", seq, start, end,
                call={"invocation_id": identifier, "attempt": 1, "provider_request_id": None},
                model={"provider": "synthetic", "requested_model": "demo-model", "response_model": "demo-model",
                       "request_count": 1, "context_id": context_id, "usage": usage(incoming, outgoing)}, **fields)


def examples():
    minimal = base("minimal-partial")
    minimal["capture"]["coverage"].update(model_requests="missing", messages="missing", contexts="missing", timing="missing")
    minimal["spans"] = [span("read-1", "tool", 0, tool={"operation": "read", "skill": {"name": "ledger-skill", "action": "load"}},
                              input=content({"path": "SKILL.md"}), output={"state": "missing"})]

    multi = base("multiturn-resume")
    multi["segments"].append({"id": "segment-2", "session": {"namespace": "synthetic-provider", "id": "continued-session"},
                               "resumed_from_segment_id": "segment-1", "checkpoint_ref": REF, "source_refs": [REF],
                               "environment": {"isolation": "sandbox", "network_access": "blocked"}})
    multi["clocks"].append({"id": "clock-2", "kind": "monotonic", "origin_at": None, "uncertainty_ms": None})
    multi["messages"] = [
        {"id": "user-1", "role": "user", "turn_id": "turn-1", "content": content("读取库存和供应商"), "source_refs": [REF]},
        {"id": "assistant-1", "role": "assistant", "turn_id": "turn-1", "content": content("数据已读取，请确认是否入账"), "source_refs": [REF]},
        {"id": "user-2", "role": "user", "turn_id": "turn-2", "content": content("确认入账"), "source_refs": [REF]},
        {"id": "summary-1", "role": "system", "content": content("摘要：库存与供应商已确认，用户批准入账"), "source_refs": [REF]},
        {"id": "tool-result-1", "role": "tool", "turn_id": "turn-1", "tool_span_id": "read-1", "content": content({"quantity": 10}), "source_refs": [REF]}]
    multi["turns"] = [{"id": "turn-1", "input_message_ids": ["user-1"], "status": "ok", "source_refs": [REF]},
                      {"id": "turn-2", "input_message_ids": ["user-2"], "status": "ok", "source_refs": [REF]}]
    multi["contexts"] = [
        {"id": "context-1", "message_ids": ["user-1"], "request": content({"messages": [{"role": "user", "content": "读取库存和供应商"}], "tools": []}), "source_refs": [REF]},
        {"id": "context-2", "message_ids": ["user-1", "tool-result-1", "assistant-1", "user-2"], "request": {"state": "partial", "value": {"note": "Synthetic request excerpt; full tool definitions omitted"}}, "source_refs": [REF]},
        {"id": "context-3", "message_ids": ["summary-1", "user-2"], "request": {"state": "missing"}, "source_refs": [REF]}]
    first = model("model-1", 0, 0, 1000, 100, 20, "context-1", turn_id="turn-1")
    first["model"]["usage"] = usage(100, 20, cache_read_tokens=80, cache_write_tokens=10, reasoning_tokens=5)
    first["timing"].update(first_response_ms=600, last_response_ms=950, response_boundary="first_content")
    failure = model("model-failed", 5, 301500, 302000, 10, 2, "context-2", turn_id="turn-2", status="error")
    retry = model("model-retry", 6, 302100, 303100, 120, 30, "context-2", turn_id="turn-2")
    retry["call"] = {"invocation_id": "model-failed", "attempt": 2}
    # The request receives the pre-compaction context; the event records the
    # new context created internally. Per-sampling training context is not in
    # this prototype, so this example deliberately claims only partial capture.
    resumed = model("model-after-resume", 8, 0, 1000, 25, 5, "context-2", turn_id="turn-2", segment="segment-2", clock="clock-2")
    resumed["model"]["usage"] = {"accounting": "components", "completeness": "complete", "source_refs": [REF], "components": [
        {"id": "compaction", "purpose": "compaction", "counts": {"input_tokens": 20, "output_tokens": 4}, "source_refs": [REF]},
        {"id": "generation", "purpose": "generation", "counts": {"input_tokens": 5, "output_tokens": 1}, "source_refs": [REF]}]}
    multi["spans"] = [first,
        span("read-1", "tool", 1, 1000, 1500, turn_id="turn-1", tool={"operation": "read", "call_id": "call-a"}),
        span("read-2", "tool", 2, 1000, 1300, turn_id="turn-1", tool={"operation": "read", "call_id": "call-b"}),
        span("unknown-tool", "tool", 3, tool={"operation": "other"}, output={"state": "redacted"}),
        span("user-wait", "wait", 4, 1500, 301500, name="等待用户确认", turn_id="turn-1", wait={"reason": "human_input"}),
        failure, retry,
        span("write-1", "tool", 7, 303100, 303300, turn_id="turn-2", tool={"operation": "write"}),
        resumed]
    multi["links"] = [{"from": "model-1", "to": tool, "type": "invokes", "source_refs": [REF]} for tool in ("read-1", "read-2")]
    multi["links"] += [{"from": "model-retry", "to": "model-failed", "type": "retry_of", "source_refs": [REF]},
                       {"from": "model-retry", "to": "write-1", "type": "invokes", "source_refs": [REF]}]
    multi["events"] = [
        {"id": "compact-1", "type": "compaction", "segment_id": "segment-2", "span_id": "model-after-resume", "source_refs": [REF],
         "context_change": {"before": "context-2", "after": "context-3", "summary": content("库存与供应商已确认，用户批准入账")}},
        {"id": "resume-1", "type": "resume", "segment_id": "segment-2", "source_refs": [REF], "data": content({"checkpoint": "synthetic-checkpoint"})},
        {"id": "gap-1", "type": "capture_gap", "segment_id": "segment-1", "source_refs": [REF], "data": content({"reason": "Some messages and request context are unavailable in this synthetic partial capture"})}]
    batch = base("opaque-batch")
    batch["spans"] = [span("opaque", "model_batch", 0,
                           model={"provider": None, "requested_model": None, "request_count": None, "context_id": None, "usage": usage(200, 60)})]
    fork = base("fork-at-checkpoint")
    fork["run"]["lineage"] = {"parent_run_id": "multiturn-resume", "parent_document_id": "multiturn-resume-doc-1", "checkpoint_ref": REF}
    fork["spans"] = [model("fresh-model", 0, 0, 400, 30, 6, None)]
    return {"minimal-partial": minimal, "multiturn-resume": multi, "opaque-batch": batch, "fork-at-checkpoint": fork}


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "synthetic-source.json").write_bytes(SOURCE_BYTES)
    for name, document in examples().items():
        (OUT / (name + ".json")).write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n")
    print("Wrote four synthetic draft examples")
