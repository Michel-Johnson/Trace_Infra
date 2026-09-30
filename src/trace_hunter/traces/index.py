"""Rebuildable source projections; indexing never evaluates a trace.

``project`` verifies the source on every call and atomically replaces this
projector's rows. ``status`` reads metadata only. Index completeness means all
document spans were projected, not that collection or source evidence is complete.
Counts describe records: a model_batch is not expanded into invented requests;
``unknown`` counts records whose original status is unknown, and overlaps kinds.
``source_ordinal`` is the zero-based position in the immutable spans array.
Source sequence and clock domains remain separate, with no inferred wall time,
durations, user turns, classification, token totals, or body content.
"""

import json
from collections import Counter
from datetime import datetime, timedelta

from sqlalchemy import text

from ..catalog import canonical
from ..content import ContentCorruption, ContentRef
from .formats import inspect_document
from .service import TraceRevisions, utc_now
from .visibility import evaluate as evaluate_visibility

PROJECTOR_VERSION = "trace-index/4"
KEYS = ("project_id", "run_id", "revision", "projector_version")
WHERE = " AND ".join(key + "=:" + key for key in KEYS)
COUNT_COLUMNS = {
    "records": "record_count", "models": "model_count", "model_batches": "model_batch_count",
    "tools": "tool_count", "agents": "agent_count", "waits": "wait_count",
    "other": "other_count", "unknown": "unknown_count",
}
TIMING_FIELDS = (
    "start_ms", "end_ms", "duration_ms", "duration_basis", "duration_scope",
    "first_response_ms", "last_response_ms", "response_boundary",
)
USAGE_FIELDS = ("input_tokens", "output_tokens", "cache_read_tokens",
                "cache_write_tokens", "reasoning_tokens")
COVERAGE_FIELDS = ("tools", "model_requests", "messages", "contexts", "timing")
METADATA_COLUMNS = ("query_id", "env_id", "harness", "model", "title", "run_status")


class TraceIndex:
    def __init__(self, revisions: TraceRevisions):
        self.revisions = revisions
        self.repository = revisions.repository
        self.projector_version = PROJECTOR_VERSION

    def _base(self, descriptor):
        meta = descriptor["metadata"]
        return {
            "project_id": descriptor["project_id"], "run_id": descriptor["run_id"],
            "revision": descriptor["revision"], "projector_version": self.projector_version,
            "content_digest": descriptor["content"]["digest"],
            "format_version": descriptor["format_version"], "state": "unindexed",
            "error_code": None, "query_id": meta.get("query_id"), "env_id": meta.get("env_id"),
            "harness": meta.get("harness"), "model": meta.get("model"),
            "run_status": meta.get("status"), "title": meta.get("title"),
            **{column: None for column in COUNT_COLUMNS.values()},
            "capture_coverage": None,
            "trace_attributes": canonical({})[0],
            "identity_basis": "legacy_compatibility" if descriptor["format_version"] == "trace-hunter/1.0" else "source",
            "projection_digest": None, "indexed_at": None,
        }

    @staticmethod
    def _describe(row, metadata):
        return {
            **{key: row[key] for key in KEYS},
            "content_digest": row["content_digest"], "format_version": row["format_version"],
            "state": row["state"], "error_code": row["error_code"],
            "run": {key: metadata.get(key) for key in ("query_id", "env_id", "harness", "model", "title", "status")},
            "identity_basis": row["identity_basis"],
            "counts": {key: row[column] for key, column in COUNT_COLUMNS.items()},
            "coverage": {
                "index": row["state"],
                "capture": json.loads(row["capture_coverage"]) if row["capture_coverage"] is not None
                    else {key: "unknown" for key in COVERAGE_FIELDS},
                "source_verification": "document_bytes_only",
            },
            "projection_digest": row["projection_digest"], "indexed_at": row["indexed_at"],
        }

    def _descriptor(self, project_id, run_id, revision):
        # Unlike revision discovery, index operations never resolve "latest".
        if revision is None:
            raise ValueError("Index operations require an explicit revision")
        return self.revisions.get(project_id, run_id, revision)

    def status(self, project_id: str, run_id: str, revision: int) -> dict:
        descriptor = self._descriptor(project_id, run_id, revision)
        base = self._base(descriptor)
        rows = self.repository.rows("""
            SELECT project_id,run_id,revision,projector_version,content_digest,format_version,
                projection_state AS state,projection_error AS error_code,record_count,model_count,
                model_batch_count,tool_count,agent_count,wait_count,other_count,unknown_count,
                capture_coverage,identity_basis,projection_digest,indexed_at
            FROM trace_revisions WHERE project_id=:project_id AND run_id=:run_id
                AND revision=:revision AND projector_version=:projector_version
        """, base)
        return self._describe(rows[0] if rows else base, descriptor["metadata"])

    @staticmethod
    def _stored_summary(row):
        # Run metadata already belongs to the immutable revision. Avoid a second
        # free-text copy in the record index (PostgreSQL TEXT cannot hold NUL).
        # Keep the logical projection/hash unchanged for existing valid sources.
        return {**row, **{key: None for key in METADATA_COLUMNS}}

    @staticmethod
    def _records(document, base):
        v2 = document["schema_version"].startswith("trace-hunter/2.0-draft.")
        clocks = {clock["id"]: clock for clock in document.get("clocks", [])} if v2 else {}
        visibility = evaluate_visibility(document) if v2 else {}
        segments = {item["id"]: item for item in document.get("segments", [])} if v2 else {}
        records = []
        output_owner, context_owner, message_context = {}, {}, {}
        if v2:
            for span in document.get("spans", []):
                for message_id in span.get("model", {}).get("output_message_ids", []):
                    output_owner.setdefault(message_id, span["id"])
                context_id = span.get("model", {}).get("context_id")
                if context_id:
                    context_owner.setdefault(context_id, span["id"])
            for context in document.get("contexts", []):
                for message_id in context.get("message_ids", []):
                    message_context.setdefault(message_id, context["id"])
        span_facts = {}

        def absolute(origin, offset):
            if origin is None or offset is None:
                return None
            try:
                value = datetime.fromisoformat(origin.replace("Z", "+00:00")) + timedelta(milliseconds=offset)
            except (TypeError, ValueError, OverflowError):
                return None
            return value.isoformat()

        def model_usage(span):
            model = span.get("model", {}) if v2 else {}
            usage = model.get("usage") or {}
            counts = usage.get("total")
            if counts is None and usage.get("accounting") == "components":
                counts = {}
                for key in USAGE_FIELDS:
                    values = [item.get("counts", {}).get(key) for item in usage.get("components", [])]
                    counts[key] = sum(values) if values and all(item is not None for item in values) else None
            counts = counts or {}
            return {"model_provider": model.get("provider"),
                    "requested_model": model.get("requested_model"),
                    "response_model": model.get("response_model"),
                    "usage_completeness": usage.get("completeness"),
                    **{key: counts.get(key) for key in USAGE_FIELDS}}

        for ordinal, span in enumerate(document["spans"]):
            timing = span.get("timing", {}) if v2 else span
            order = span.get("order", {}) if v2 else {}
            tool = span.get("tool", {}) if v2 else {}
            call = span.get("call", {}) if v2 else {}
            skill = tool.get("skill", {}) if v2 else span.get("skill", {})
            clock_id = timing.get("clock_id") if v2 else None
            clock = clocks.get(clock_id, {})
            session = segments.get(span.get("segment_id"), {}).get("session") or {}
            summary = {key: value for key, value in span.items() if key not in ("input", "output")}
            record = {
                **{key: base[key] for key in KEYS},
                "object_ordinal": len(records), "object_kind": "span", "object_id": span["id"],
                "source_ordinal": ordinal, "span_id": span["id"], "kind": span["kind"],
                "name": span["name"], "operation": tool.get("operation") if v2 else span["operation"],
                "status": span["status"], "agent_id": span.get("agent_id"),
                "segment_id": span.get("segment_id") if v2 else None,
                "turn_id": span.get("turn_id") if v2 else None,
                "parent_id": span.get("parent_id"),
                "proposal_id": tool.get("proposal_id") if v2 else None,
                "call_id": tool.get("call_id") if v2 else span.get("request_id"),
                "invocation_id": call.get("invocation_id"), "attempt": call.get("attempt"),
                "context_id": span.get("model", {}).get("context_id") if v2 else None,
                "visibility_status": visibility.get(span["id"], {}).get("status"),
                "visibility_issues": canonical(visibility.get(span["id"], {}).get("issues", []))[0]
                    if span["id"] in visibility else None,
                "skill_name": skill.get("name"), "skill_action": skill.get("action"),
                "source_refs": canonical(span.get("source_refs", []) if v2 else [span["source"]])[0],
                "stream_id": order.get("stream_id"),
                "source_sequence": order.get("sequence") if v2 else span.get("sequence"),
                "clock_id": clock_id, "clock_kind": clock.get("kind"),
                "clock_origin_at": clock.get("origin_at"),
                "clock_uncertainty_ms": clock.get("uncertainty_ms"),
                "start_at": absolute(clock.get("origin_at"), timing.get("start_ms")),
                "end_at": absolute(clock.get("origin_at"), timing.get("end_ms")),
                "session_namespace": session.get("namespace"), "session_id": session.get("id"),
                "attributes": canonical(span.get("attributes", {}))[0],
                **{key: timing.get(key) for key in TIMING_FIELDS},
                **model_usage(span),
                "payload": canonical(summary)[0],
            }
            records.append(record)
            span_facts[span["id"]] = {key: record[key] for key in (
                "source_ordinal", "start_at", "end_at", "session_namespace", "session_id")}
        if v2:
            for object_kind, values in (("message", document.get("messages", [])),
                                        ("context", document.get("contexts", [])),
                                        ("tool_call", document.get("tool_calls", []))):
                for value in values:
                    content_fields = {"message": ("content",), "context": ("request",),
                                      "tool_call": ("arguments",)}[object_kind]
                    summary = {key: item for key, item in value.items() if key not in content_fields}
                    if object_kind == "message":
                        owner_id = value.get("tool_span_id") or output_owner.get(value["id"])
                        owner_id = owner_id or context_owner.get(message_context.get(value["id"]))
                    elif object_kind == "context":
                        owner_id = context_owner.get(value["id"])
                    else:
                        owner_id = value.get("model_span_id") or value.get("tool_span_id")
                    owner = span_facts.get(owner_id, {})
                    records.append({
                        **{key: base[key] for key in KEYS}, "object_ordinal": len(records),
                        "object_kind": object_kind, "object_id": value["id"],
                        "source_ordinal": owner.get("source_ordinal"), "span_id": owner_id,
                        "parent_id": None, "kind": None, "name": value.get("name"),
                        "operation": None, "status": None, "agent_id": None, "segment_id": None,
                        "turn_id": None, "proposal_id": None, "call_id": None,
                        "invocation_id": None, "attempt": None,
                        "context_id": value["id"] if object_kind == "context" else None,
                        "visibility_status": None, "visibility_issues": None,
                        "skill_name": None, "skill_action": None,
                        "source_refs": canonical(value.get("source_refs", []))[0],
                        "stream_id": None, "source_sequence": None, "clock_id": None,
                        "clock_kind": None, "clock_origin_at": None, "clock_uncertainty_ms": None,
                        "start_at": owner.get("start_at"), "end_at": owner.get("end_at"),
                        "session_namespace": owner.get("session_namespace"), "session_id": owner.get("session_id"),
                        "attributes": canonical(value.get("attributes", {}))[0],
                        **{key: None for key in TIMING_FIELDS}, "payload": canonical(summary)[0],
                        **{key: None for key in ("model_provider", "requested_model", "response_model",
                                                "usage_completeness", *USAGE_FIELDS)},
                    })
        return records

    @staticmethod
    def _search_projection(document, base):
        v2 = document["schema_version"].startswith("trace-hunter/2.0-draft.")
        output_owner = {}
        context_owner = {}
        for span in document.get("spans", []):
            for message_id in span.get("model", {}).get("output_message_ids", []):
                output_owner.setdefault(message_id, span["id"])
            context_id = span.get("model", {}).get("context_id")
            if context_id:
                context_owner.setdefault(context_id, span["id"])
        documents = []

        def add(kind, object_id, span_id, field, value, refs, text_state="exact"):
            if value is None:
                return
            text_value = value if isinstance(value, str) else canonical(value)[0]
            # PostgreSQL TEXT rejects U+0000. Keep the immutable source intact,
            # but make the rebuildable search projection safe and explicitly
            # non-exact so callers do not mistake the replacement for source.
            if "\x00" in text_value:
                text_value = text_value.replace("\x00", "\ufffd")
                text_state = "truncated"
            documents.append({
                **{key: base[key] for key in KEYS}, "document_ordinal": len(documents),
                "object_kind": kind, "object_id": object_id, "span_id": span_id,
                "field": field, "text": text_value, "text_state": text_state,
                "source_refs": canonical(refs)[0],
            })

        def content(kind, object_id, span_id, field, value, refs):
            if not isinstance(value, dict) or value.get("state") not in ("complete", "partial"):
                return
            if "value" in value:
                add(kind, object_id, span_id, field, value["value"], refs)
            elif "search_text" in value:
                add(kind, object_id, span_id, field, value["search_text"], refs,
                    value.get("search_text_state", "truncated"))

        for span in document.get("spans", []):
            refs = span.get("source_refs", [])
            if not v2:
                refs = [span["source"]]
            add("span", span["id"], span["id"], "name", span["name"], refs)
            if v2:
                content("span", span["id"], span["id"], "input", span.get("input"), refs)
                content("span", span["id"], span["id"], "output", span.get("output"), refs)
            else:
                add("span", span["id"], span["id"], "input", span.get("input"), refs)
                add("span", span["id"], span["id"], "output", span.get("output"), refs)
        if not v2:
            return documents, []
        for message in document.get("messages", []):
            span_id = message.get("tool_span_id") or output_owner.get(message["id"])
            content("message", message["id"], span_id, "content", message["content"], message["source_refs"])
        for context in document.get("contexts", []):
            content("context", context["id"], context_owner.get(context["id"]), "request",
                    context["request"], context["source_refs"])
        for proposal in document.get("tool_calls", []):
            add("tool_call", proposal["id"], proposal.get("model_span_id"), "name",
                proposal["name"], proposal["source_refs"])
            content("tool_call", proposal["id"], proposal.get("model_span_id"), "arguments",
                    proposal["arguments"], proposal["source_refs"])
        edges = []
        def edge(source_kind, source_id, relation, target_kind, target_id, position=None, payload=None):
            edges.append({**{key: base[key] for key in KEYS}, "edge_ordinal": len(edges),
                          "source_kind": source_kind, "source_id": source_id, "relation": relation,
                          "target_kind": target_kind, "target_id": target_id, "position": position,
                          "payload": canonical(payload or {})[0]})
        for span in document.get("spans", []):
            if span.get("parent_id"):
                edge("span", span["id"], "parent", "span", span["parent_id"])
            proposal = span.get("tool", {}).get("proposal_id")
            if proposal:
                edge("span", span["id"], "proposal_of", "tool_call", proposal)
        for context in document.get("contexts", []):
            for position, message_id in enumerate(context["message_ids"]):
                edge("context", context["id"], "context_message", "message", message_id, position)
        for link in document.get("links", []):
            edge("span", link["from"], link["type"], "span", link["to"], payload={"source_refs": link["source_refs"]})
        return documents, edges

    def _project(self, base, descriptor):
        # The descriptor was resolved through the project-scoped revision service.
        # Read its verified content without borrowing another DB connection while
        # holding the projection lock (which can exhaust a concurrent pool).
        with self.revisions.content.open_verified(ContentRef(**descriptor["content"])) as source:
            raw = source.read()
        document, meta = inspect_document(raw)
        records = self._records(document, base)
        search_documents, edges = self._search_projection(document, base)
        kinds = Counter(record["kind"] for record in records if record["object_kind"] == "span")
        coverage = document.get("capture", {}).get("coverage", {}) if meta["format_stability"] == "experimental" else document["coverage"]
        row = {
            **base, "state": "complete", "record_count": len(document["spans"]),
            **{key: meta.get(key) for key in ("query_id", "env_id", "harness", "model", "title")},
            "run_status": meta.get("status"),
            "model_count": kinds["model"], "model_batch_count": kinds["model_batch"],
            "tool_count": kinds["tool"], "agent_count": kinds["agent"],
            "wait_count": kinds["wait"], "other_count": kinds["other"],
            "unknown_count": sum(record["status"] == "unknown" for record in records
                                 if record["object_kind"] == "span"),
            "capture_coverage": canonical({key: coverage.get(key, "unknown") for key in COVERAGE_FIELDS})[0],
            "trace_attributes": canonical(document.get("run", {}).get("attributes", {}))[0],
        }
        row["projection_digest"] = canonical({"summary": row, "records": records,
                                              "search_documents": search_documents,
                                              "edges": edges})[1]
        return row, records, search_documents, edges

    @staticmethod
    def _replace(db, row, records, search_documents=(), edges=()):
        db.execute(text("DELETE FROM trace_edges WHERE " + WHERE), row)
        db.execute(text("DELETE FROM trace_search_hot_postings WHERE " + WHERE), row)
        db.execute(text("DELETE FROM trace_search_documents WHERE " + WHERE), row)
        db.execute(text("DELETE FROM trace_objects WHERE " + WHERE), row)
        if records:
            columns = ",".join(records[0])
            db.execute(text("INSERT INTO trace_objects(" + columns + ") VALUES(" +
                ",".join(":" + key for key in records[0]) + ")"), records)
        for table, values in (("trace_search_documents", search_documents),
                              ("trace_edges", edges)):
            if values:
                columns = ",".join(values[0])
                db.execute(text("INSERT INTO " + table + "(" + columns + ") VALUES(" +
                    ",".join(":" + key for key in values[0]) + ")"), values)
        if search_documents:
            terms = db.execute(text("""SELECT term FROM trace_search_hot_terms
                WHERE project_id=:project_id AND active"""), row).scalars().all()
            postings = [{"project_id": document["project_id"], "term": term,
                         "run_id": document["run_id"], "revision": document["revision"],
                         "projector_version": document["projector_version"],
                         "document_ordinal": document["document_ordinal"],
                         "object_kind": document["object_kind"], "field": document["field"],
                         "span_id": document["span_id"]}
                        for document in search_documents for term in terms
                        if term in document["text"]]
            if postings:
                columns = ",".join(postings[0])
                db.execute(text("INSERT INTO trace_search_hot_postings(" + columns + ") VALUES(" +
                    ",".join(":" + key for key in postings[0]) + ")"), postings)
        values = TraceIndex._stored_summary(row)
        assignments = {
            "projector_version": row["projector_version"], "projection_state": row["state"],
            "projection_error": row["error_code"], "projection_digest": row["projection_digest"],
            "indexed_at": row["indexed_at"], "capture_coverage": row["capture_coverage"],
            "identity_basis": row["identity_basis"],
            "attributes": row["trace_attributes"],
            **{column: row[column] for column in COUNT_COLUMNS.values()},
        }
        db.execute(text("UPDATE trace_revisions SET " + ",".join(key + "=:" + key for key in assignments) +
            " WHERE project_id=:project_id AND run_id=:run_id AND revision=:revision"),
            {**row, **assignments})

    def project(self, project_id: str, run_id: str, revision: int) -> dict:
        descriptor = self._descriptor(project_id, run_id, revision)
        base = self._base(descriptor)
        with self.repository.engine.begin() as db:
            if not self.repository.postgres:
                db.execute(text("""UPDATE trace_revisions SET projector_version=projector_version
                    WHERE project_id=:project_id AND run_id=:run_id AND revision=:revision"""), base)
            lock = " FOR UPDATE" if self.repository.postgres else ""
            previous = db.execute(text("""SELECT projection_state AS state,projection_digest,indexed_at
                FROM trace_revisions WHERE project_id=:project_id AND run_id=:run_id AND revision=:revision""" + lock), base).mappings().one()
            try:
                row, records, search_documents, edges = self._project(base, descriptor)
            except Exception as error:
                # Do not persist parser messages, filesystem paths or source body.
                code = "content_integrity_failed" if isinstance(error, ContentCorruption) else (
                    "content_missing" if isinstance(error, FileNotFoundError) else (
                    "content_unavailable" if isinstance(error, OSError) else (
                    "source_invalid" if isinstance(error, ValueError) else "projection_failed")))
                row, records, search_documents, edges = ({**base, "state": "failed", "error_code": code}, [], [], [])
            if row["state"] == "complete":
                row["indexed_at"] = previous["indexed_at"] if previous["state"] == "complete" and previous["projection_digest"] == row["projection_digest"] else utc_now()
            try:
                with db.begin_nested():
                    self._replace(db, row, records, search_documents, edges)
            except Exception:
                # Roll back partial inserts before publishing an explicit failure.
                # If the database itself is unavailable, this write raises rather
                # than claiming the failure status was durably saved.
                row = {**base, "state": "failed", "error_code": "index_write_failed"}
                self._replace(db, row, [])
        return self._describe(row, descriptor["metadata"])
