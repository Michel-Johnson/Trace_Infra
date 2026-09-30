"""Build the isolated v2 design schema; never modifies the live import contract."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def obj(properties, required=()):
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


def ref(name):
    return {"$ref": f"#/$defs/{name}"}


def array(items, limit=20000):
    return {"type": "array", "items": items, "maxItems": limit}


def nullable(value):
    return {"anyOf": [value, {"type": "null"}]}


def enum(*values):
    return {"enum": list(values)}


ID = {"type": "string", "minLength": 1, "maxLength": 256}
TEXT = {"type": "string", "maxLength": 100000}
NUMBER = {"type": "number", "minimum": 0}
COUNT = {"type": "integer", "minimum": 0}
STATUS = enum("ok", "error", "running", "cancelled", "unknown")
SOURCES = {**array(ref("source_ref"), 100), "minItems": 1}


def build():
    defs = {}
    defs["source_ref"] = obj({"source_id": ID, "pointer": {"type": "string", "maxLength": 4096}}, ("source_id", "pointer"))
    defs["source"] = obj({"id": ID, "sha256": nullable({"type": "string", "pattern": "^[0-9a-f]{64}$"}),
                          "locator": TEXT, "media_type": ID, "state": enum("available", "external", "missing", "redacted")},
                         ("id", "sha256", "state"))
    defs["content"] = obj({"state": enum("complete", "partial", "missing", "redacted"),
                           "value": {}, "ref": ref("source_ref")}, ("state",))
    defs["content"]["oneOf"] = [
        {"properties": {"state": enum("complete", "partial")}, "oneOf": [
            {"required": ["value"], "not": {"required": ["ref"]}},
            {"required": ["ref"], "not": {"required": ["value"]}}]},
        {"properties": {"state": enum("missing", "redacted")},
         "not": {"anyOf": [{"required": ["value"]}, {"required": ["ref"]}]}}]
    defs["environment"] = obj({"isolation": enum("sandbox", "non_sandbox", "unknown"),
                               "network_access": enum("allowed", "blocked", "unknown"),
                               "snapshot_ref": nullable(ref("source_ref")),
                               "observed_at": nullable({"type": "string", "format": "date-time"}),
                               "tool_versions": {"type": "object", "additionalProperties": nullable(ID)}},
                              ("isolation", "network_access"))
    defs["clock"] = obj({"id": ID, "kind": enum("monotonic", "wall", "unknown"),
                         "origin_at": nullable({"type": "string", "format": "date-time"}),
                         "uncertainty_ms": nullable(NUMBER)}, ("id", "kind"))
    defs["timing"] = obj({"clock_id": nullable(ID), "start_ms": nullable(NUMBER), "end_ms": nullable(NUMBER),
                          "duration_ms": nullable(NUMBER), "first_response_ms": nullable(NUMBER),
                          "duration_basis": enum("interval_derived", "source_reported"),
                          "duration_scope": enum("elapsed", "active", "unknown"),
                          "last_response_ms": nullable(NUMBER),
                          "response_boundary": enum("first_byte", "first_content", "first_token", "unknown")},
                         ("clock_id", "start_ms", "end_ms"))
    defs["order"] = obj({"stream_id": ID, "sequence": COUNT}, ("stream_id", "sequence"))
    defs["counts"] = obj({key: nullable(COUNT) for key in
                          ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "reasoning_tokens")},
                         ("input_tokens", "output_tokens"))
    defs["usage_component"] = obj({"id": ID, "purpose": enum("generation", "compaction", "other"),
                                   "counts": ref("counts"), "provider": nullable(ID), "model": nullable(ID),
                                   "source_refs": SOURCES}, ("id", "purpose", "counts", "source_refs"))
    defs["usage"] = obj({"accounting": enum("total", "components"), "completeness": enum("complete", "partial"),
                         "total": ref("counts"), "components": {**array(ref("usage_component"), 1000), "minItems": 1},
                         "source_refs": SOURCES}, ("accounting", "completeness", "source_refs"))
    defs["usage"]["oneOf"] = [
        {"properties": {"accounting": {"const": "total"}}, "required": ["total"], "not": {"required": ["components"]}},
        {"properties": {"accounting": {"const": "components"}}, "required": ["components"], "not": {"required": ["total"]}}]
    defs["call"] = obj({"invocation_id": ID, "attempt": {"type": "integer", "minimum": 1},
                        "provider_request_id": nullable(ID)}, ("invocation_id", "attempt"))
    defs["model"] = obj({"provider": nullable(ID), "requested_model": nullable(ID), "response_model": nullable(ID),
                         "request_count": nullable({"type": "integer", "minimum": 1}),
                         "context_id": nullable(ID), "output_message_ids": array(ID), "usage": nullable(ref("usage"))},
                        ("provider", "requested_model", "request_count", "context_id", "usage"))
    defs["tool_call"] = obj({"id": ID, "call_id": nullable(ID), "name": ID, "arguments": ref("content"),
                             "model_span_id": nullable(ID), "message_id": nullable(ID), "source_refs": SOURCES},
                            ("id", "name", "arguments", "source_refs"))
    defs["tool"] = obj({"operation": enum("read", "write", "bash", "human", "other"), "call_id": nullable(ID), "proposal_id": nullable(ID),
                        "skill": obj({"name": nullable(ID), "action": enum("load", "invoke")}, ("name", "action"))},
                       ("operation",))
    defs["wait"] = obj({"reason": enum("human_input", "approval", "rate_limit", "queue", "external", "unknown")}, ("reason",))
    defs["span"] = obj({"id": ID, "kind": enum("model", "model_batch", "tool", "agent", "wait", "other"),
                        "name": ID, "agent_id": ID, "segment_id": ID, "turn_id": nullable(ID), "phase_id": nullable(ID),
                        "parent_id": nullable(ID), "status": STATUS, "timing": ref("timing"), "order": ref("order"),
                        "input": ref("content"), "output": ref("content"), "call": ref("call"),
                        "model": ref("model"), "tool": ref("tool"), "wait": ref("wait"), "source_refs": SOURCES,
                        "extensions": ref("extensions")}, ("id", "kind", "name", "agent_id", "segment_id", "status", "source_refs"))
    defs["span"]["allOf"] = [
        {"if": {"properties": {"kind": enum("model", "model_batch")}}, "then": {"required": ["model"]}, "else": {"not": {"required": ["model"]}}},
        {"if": {"properties": {"kind": {"const": "model"}}}, "then": {"required": ["call"], "properties": {"model": {"properties": {"request_count": {"const": 1}}}}}},
        {"if": {"properties": {"kind": {"const": "model_batch"}}}, "then": {"properties": {"model": {"properties": {"request_count": nullable({"type": "integer", "minimum": 2})}}}}},
        {"if": {"properties": {"kind": {"const": "tool"}}}, "then": {"required": ["tool"]}, "else": {"not": {"required": ["tool"]}}},
        {"if": {"properties": {"kind": {"const": "wait"}}}, "then": {"required": ["wait"]}, "else": {"not": {"required": ["wait"]}}}]
    defs["segment"] = obj({"id": ID, "session": nullable(obj({"namespace": ID, "id": ID}, ("namespace", "id"))),
                           "resumed_from_segment_id": nullable(ID), "checkpoint_ref": nullable(ref("source_ref")),
                           "environment": ref("environment"), "source_refs": SOURCES}, ("id", "source_refs"))
    defs["turn"] = obj({"id": ID, "input_message_ids": {**array(ID), "minItems": 1}, "status": STATUS,
                        "source_refs": SOURCES}, ("id", "input_message_ids", "status", "source_refs"))
    defs["message"] = obj({"id": ID, "role": enum("system", "developer", "user", "assistant", "tool", "other"),
                           "turn_id": nullable(ID), "content": ref("content"), "tool_span_id": nullable(ID),
                           "source_refs": SOURCES}, ("id", "role", "content", "source_refs"))
    defs["context"] = obj({"id": ID, "message_ids": array(ID), "request": ref("content"), "source_refs": SOURCES},
                          ("id", "message_ids", "request", "source_refs"))
    defs["event"] = obj({"id": ID, "type": enum("compaction", "checkpoint", "resume", "capture_gap", "truncation", "other"),
                         "segment_id": ID, "turn_id": nullable(ID), "span_id": nullable(ID), "clock_id": nullable(ID),
                         "at_ms": nullable(NUMBER), "order": ref("order"), "source_refs": SOURCES,
                         "context_change": obj({"before": nullable(ID), "after": nullable(ID), "summary": ref("content")}, ("before", "after", "summary")),
                         "data": ref("content")}, ("id", "type", "segment_id", "source_refs"))
    defs["event"]["allOf"] = [{"if": {"properties": {"type": {"const": "compaction"}}}, "then": {"required": ["context_change"]}}]
    defs["link"] = obj({"from": ID, "to": ID, "type": enum("invokes", "depends_on", "retry_of"), "source_refs": SOURCES},
                       ("from", "to", "type", "source_refs"))
    defs["artifact"] = obj({"id": ID, "kind": enum("output", "initial_state", "final_state", "checkpoint", "other"),
                            "content": ref("content"), "span_ids": array(ID), "source_refs": SOURCES},
                           ("id", "kind", "content", "source_refs"))
    defs["extensions"] = {"type": "object", "patternProperties": {"^[a-z][a-z0-9_-]*\\.[a-zA-Z0-9_.-]+$": {}}, "additionalProperties": False}
    coverage = obj({key: enum("complete", "partial", "missing", "unknown") for key in
                    ("tools", "model_requests", "messages", "contexts", "timing")},
                   ("tools", "model_requests", "messages", "contexts", "timing"))
    run = obj({"id": ID, "query_id": nullable(ID), "env_id": nullable(ID),
               "harness": obj({"name": ID, "version": nullable(ID)}, ("name", "version")),
               "status": enum("running", "completed", "failed", "cancelled", "unknown"),
               "environment": ref("environment"),
               "lineage": obj({"parent_run_id": ID, "parent_document_id": ID, "checkpoint_ref": ref("source_ref")},
                              ("parent_run_id", "parent_document_id", "checkpoint_ref"))},
              ("id", "harness", "status", "environment"))
    properties = {
        "schema_version": {"const": "trace-hunter/2.0-draft.1"},
        "document": obj({"id": ID, "revision": {"type": "integer", "minimum": 1}, "previous_document_id": nullable(ID), "sealed": {"type": "boolean"}},
                        ("id", "revision", "previous_document_id", "sealed")),
        "run": run,
        "capture": obj({"collector": obj({"name": ID, "version": ID}, ("name", "version")), "coverage": coverage, "notes": array(TEXT, 100)}, ("collector", "coverage")),
        **{key: array(ref(name)) for key, name in (("sources", "source"), ("clocks", "clock"), ("segments", "segment"),
           ("spans", "span"), ("turns", "turn"), ("messages", "message"), ("contexts", "context"), ("events", "event"), ("links", "link"), ("artifacts", "artifact"), ("tool_calls", "tool_call"))},
        "phases": array(obj({"id": ID, "purpose": enum("task", "setup", "export"), "name": ID, "timing": ref("timing"), "source_refs": SOURCES}, ("id", "purpose")), 100),
        "extensions": ref("extensions")}
    return {"$schema": "https://json-schema.org/draft/2020-12/schema", "$id": "urn:trace-hunter:trace:2.0-draft.1",
            "title": "Trace Hunter v2 design prototype — not accepted by the live API",
            **obj(properties, ("schema_version", "document", "run", "capture", "sources", "segments", "spans")), "$defs": defs}


if __name__ == "__main__":
    target = ROOT / "contracts/drafts/trace-v2/trace.schema.json"
    target.write_text(json.dumps(build(), ensure_ascii=False, indent=2) + "\n")
    print(target)
