"""OTLP trace ingestion into the canonical v2 fact model."""

import hashlib
import io
import json
import base64
from datetime import datetime, timezone

from .catalog import canonical
from .content import ContentRef


def _value(value):
    if not isinstance(value, dict): return value
    for key in ("stringValue", "intValue", "doubleValue", "boolValue", "bytesValue"):
        if key in value: return value[key]
    if "arrayValue" in value: return [_value(item) for item in value["arrayValue"].get("values", [])]
    if "kvlistValue" in value: return _attributes(value["kvlistValue"].get("values", []))
    return None


def _attributes(values):
    result = {}
    for item in values or []:
        if isinstance(item, dict) and isinstance(item.get("key"), str):
            result[item["key"]] = _value(item.get("value", {}))
    return result


def _hex(value):
    if not isinstance(value, str): return ""
    lowered = value.lower()
    if len(lowered) in (16, 32) and all(item in "0123456789abcdef" for item in lowered):
        return lowered
    try:
        return base64.b64decode(value, validate=True).hex()
    except (ValueError, TypeError):
        return ""


def _ns(value):
    try: return int(value)
    except (TypeError, ValueError): return None


def _status(value):
    code = value.get("code") if isinstance(value, dict) else None
    return {1: "ok", 2: "error", "STATUS_CODE_OK": "ok", "STATUS_CODE_ERROR": "error"}.get(code, "unknown")


class OtlpIngestor:
    def __init__(self, store, index):
        self.store, self.index = store, index

    def _documents(self, envelope, raw_digest, media_type):
        traces = {}
        for resource_index, resource_spans in enumerate(envelope.get("resourceSpans", [])):
            resource = _attributes((resource_spans.get("resource") or {}).get("attributes", []))
            scopes = resource_spans.get("scopeSpans") or resource_spans.get("instrumentationLibrarySpans") or []
            for scope_index, scope_spans in enumerate(scopes):
                scope = scope_spans.get("scope") or scope_spans.get("instrumentationLibrary") or {}
                for span_index, span in enumerate(scope_spans.get("spans", [])):
                    trace_id, span_id = _hex(span.get("traceId")), _hex(span.get("spanId"))
                    if not trace_id or not span_id: continue
                    item = traces.setdefault(trace_id, {"spans": [], "resource": resource, "starts": [], "scope": scope})
                    item["spans"].append((span, f"/resourceSpans/{resource_index}/scopeSpans/{scope_index}/spans/{span_index}"))
                    if _ns(span.get("startTimeUnixNano")) is not None: item["starts"].append(_ns(span["startTimeUnixNano"]))
        for trace_id, item in traces.items():
            origin_ns = min(item["starts"]) if item["starts"] else 0
            origin = datetime.fromtimestamp(origin_ns / 1_000_000_000, timezone.utc).isoformat() if origin_ns else None
            source_id = "otlp-" + raw_digest[:16]
            segments, session = [], item["resource"].get("session.id") or item["resource"].get("session_id")
            segment_id = "segment-" + raw_digest[:12]
            clock_id = "clock-" + raw_digest[:12]
            segments.append({"id": segment_id,
                             "session": {"namespace": str(item["resource"].get("service.namespace") or "otlp"), "id": str(session)} if session else None,
                             "source_refs": [{"source_id": source_id, "pointer": ""}],
                             "attributes": {"otel.scope.name": item["scope"].get("name"),
                                            "otel.scope.version": item["scope"].get("version")}})
            spans, events, links = [], [], []
            for span, pointer in item["spans"]:
                attributes = _attributes(span.get("attributes", []))
                status = span.get("status") or {}
                attributes["otel.span.kind"] = span.get("kind")
                attributes["otel.trace_state"] = span.get("traceState")
                attributes["otel.flags"] = span.get("flags")
                attributes["otel.status.code"] = status.get("code")
                attributes["otel.status.message"] = status.get("message")
                attributes["otel.dropped_attributes_count"] = span.get("droppedAttributesCount")
                attributes["otel.dropped_events_count"] = span.get("droppedEventsCount")
                attributes["otel.dropped_links_count"] = span.get("droppedLinksCount")
                attributes["otel.links"] = [{"trace_id": _hex(link.get("traceId")),
                    "span_id": _hex(link.get("spanId")), "trace_state": link.get("traceState"),
                    "attributes": _attributes(link.get("attributes", [])), "flags": link.get("flags")}
                    for link in span.get("links", [])]
                attributes = {key: value for key, value in attributes.items() if value is not None}
                start, end = _ns(span.get("startTimeUnixNano")), _ns(span.get("endTimeUnixNano"))
                start_ms = (start - origin_ns) / 1_000_000 if start is not None and origin_ns else None
                end_ms = (end - origin_ns) / 1_000_000 if end is not None and origin_ns else None
                kind = "model" if any(key.startswith("gen_ai.") for key in attributes) else (
                    "tool" if any(key in attributes for key in ("tool.name", "tool_name", "gen_ai.tool.name")) else "other")
                record = {"id": _hex(span["spanId"]), "kind": kind, "name": span.get("name") or "unnamed",
                          "agent_id": str(item["resource"].get("service.name") or "otlp"), "segment_id": segment_id,
                          "turn_id": None, "phase_id": None, "parent_id": _hex(span.get("parentSpanId")) or None,
                          "status": _status(span.get("status", {})),
                          "timing": {"clock_id": clock_id, "start_ms": start_ms, "end_ms": end_ms,
                                     "duration_ms": (end - start) / 1_000_000 if start is not None and end is not None and end >= start else None,
                                     "duration_basis": "interval_derived", "duration_scope": "elapsed"},
                          "order": {"stream_id": trace_id, "sequence": len(spans)},
                          "source_refs": [{"source_id": source_id, "pointer": pointer}], "attributes": attributes}
                if kind == "model":
                    record["call"] = {"invocation_id": record["id"], "attempt": 1, "provider_request_id": attributes.get("gen_ai.request.id")}
                    record["model"] = {"provider": attributes.get("gen_ai.system"),
                                       "requested_model": attributes.get("gen_ai.request.model"),
                                       "response_model": attributes.get("gen_ai.response.model"),
                                       "request_count": 1, "context_id": None, "output_message_ids": [], "usage": None}
                elif kind == "tool":
                    record["tool"] = {"operation": "other", "call_id": attributes.get("tool.call.id"),
                                      "proposal_id": None}
                spans.append(record)
                for event_index, event in enumerate(span.get("events", [])):
                    at = _ns(event.get("timeUnixNano")); at_ms = (at - origin_ns) / 1_000_000 if at is not None and origin_ns else None
                    events.append({"id": record["id"] + "-event-" + str(event_index), "type": "other",
                                   "segment_id": segment_id, "turn_id": None, "span_id": record["id"],
                                   "clock_id": clock_id, "at_ms": at_ms,
                                   "source_refs": [{"source_id": source_id, "pointer": pointer + f"/events/{event_index}"}],
                                   "data": {"state": "complete", "value": {"name": event.get("name"),
                                            "attributes": _attributes(event.get("attributes", []))}}})
                for linked in span.get("links", []):
                    target = _hex(linked.get("spanId"))
                    if _hex(linked.get("traceId")) == trace_id and target:
                        links.append({"from": record["id"], "to": target, "type": "depends_on",
                                      "source_refs": record["source_refs"]})
            yield trace_id, {"schema_version": "trace-hunter/2.0-draft.2",
                "document": {"id": "otlp-document-" + trace_id + "-" + raw_digest[:12], "revision": 1,
                             "previous_document_id": None, "sealed": True},
                "run": {"id": trace_id, "query_id": str(item["resource"].get("trace.query_id") or trace_id),
                        "env_id": str(item["resource"].get("deployment.environment.name") or "unknown"),
                        "harness": {"name": str(item["resource"].get("service.name") or "otlp"),
                                    "version": str(item["resource"].get("service.version")) if item["resource"].get("service.version") else None},
                        "status": "failed" if any(span["status"] == "error" for span in spans) else "unknown",
                        "environment": {"isolation": "unknown", "network_access": "unknown"},
                        "attributes": item["resource"]},
                "capture": {"collector": {"name": "otlp", "version": "1"},
                            "coverage": {"tools": "partial", "model_requests": "partial", "messages": "missing",
                                         "contexts": "missing", "timing": "complete"}, "notes": []},
                "sources": [{"id": source_id, "sha256": raw_digest, "locator": "content:sha256:" + raw_digest,
                             "media_type": media_type, "state": "available"}],
                "clocks": [{"id": clock_id, "kind": "wall", "origin_at": origin, "uncertainty_ms": None}],
                "segments": segments, "spans": spans, "turns": [], "messages": [], "contexts": [],
                "events": events, "links": links, "artifacts": [], "tool_calls": [], "phases": [],
                "extensions": {"trace_hunter.otlp": {"resource": item["resource"], "scope": item["scope"]}}}

    def ingest(self, project_id, envelope, raw, *, media_type="application/json"):
        if not isinstance(envelope, dict): raise ValueError("OTLP request must be an object")
        digest = hashlib.sha256(raw).hexdigest()
        self.store.content.put(io.BytesIO(raw), media_type=media_type)
        accepted, spans = [], 0
        for trace_id, document in self._documents(envelope, digest, media_type):
            request_key = f"otlp:{digest}:{trace_id}"
            existing = self.store.repository.rows("SELECT run_id,revision FROM trace_revisions "
                "WHERE project_id=:project AND request_key=:request_key",
                {"project": project_id, "request_key": request_key})
            input_spans = len(document["spans"])
            if existing:
                descriptor = self.store.revisions.get(project_id, existing[0]["run_id"], existing[0]["revision"])
                index = self.index.project(project_id, descriptor["run_id"], descriptor["revision"])
                if index["state"] != "complete": raise ValueError("OTLP projection failed")
                accepted.append({"run_id": descriptor["run_id"], "revision": descriptor["revision"]})
                spans += input_spans
                continue
            latest = self.store.repository.rows("SELECT latest_revision FROM traces WHERE project_id=:project AND run_id=:run",
                                                {"project": project_id, "run": trace_id})
            previous = latest[0]["latest_revision"] if latest else 0
            if previous:
                descriptor = self.store.revisions.get(project_id, trace_id, previous)
                with self.store.content.open_verified(ContentRef(**descriptor["content"])) as source:
                    earlier = json.load(source)
                document["document"]["previous_document_id"] = earlier["document"]["id"]
                for key in ("sources", "clocks", "segments", "spans", "turns", "messages", "contexts",
                            "events", "artifacts", "tool_calls", "phases"):
                    merged = {item["id"]: item for item in earlier.get(key, [])}
                    merged.update({item["id"]: item for item in document.get(key, [])})
                    document[key] = list(merged.values())
                previous_links = {canonical(item)[0]: item for item in earlier.get("links", [])}
                previous_links.update({canonical(item)[0]: item for item in document.get("links", [])})
                document["links"] = list(previous_links.values())
                document["run"]["attributes"] = {**earlier.get("run", {}).get("attributes", {}),
                                                  **document["run"].get("attributes", {})}
                if earlier.get("run", {}).get("status") == "failed":
                    document["run"]["status"] = "failed"
            document["document"]["revision"] = previous + 1
            result = self.store.revisions.append(project_id, canonical(document)[0].encode(),
                request_key=request_key, expected_previous=previous,
                derivation="capture" if previous == 0 else "supplement")
            descriptor = result["revision"]
            index = self.index.project(project_id, descriptor["run_id"], descriptor["revision"])
            if index["state"] != "complete": raise ValueError("OTLP projection failed")
            accepted.append({"run_id": descriptor["run_id"], "revision": descriptor["revision"]})
            spans += input_spans
        return {"accepted_traces": accepted, "accepted_spans": spans, "rejected_spans": 0}
