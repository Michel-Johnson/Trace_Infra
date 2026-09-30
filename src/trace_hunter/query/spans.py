"""Bounded structured Span queries over immutable revision projections."""
import base64
import binascii
import json

from sqlalchemy import text

from ..catalog import canonical
from ..traces.index import PROJECTOR_VERSION
from ..traces.service import MAX_REVISION, identifier

VERSION = "span-query/1"
WINDOW_VERSION = "span-window/1"
MAX_ITEM_BYTES = 2 * 1024 * 1024
MAX_WINDOW_SIDE = 100
MAX_WINDOW_ATTACHMENTS = 500
MAX_PREVIEW_CHARS = 2048
EXACT_FILTERS = (
    "run_id", "kind", "name", "operation", "status", "agent_id", "segment_id",
    "turn_id", "parent_id", "skill_name", "skill_action", "proposal_id", "call_id",
    "invocation_id", "context_id", "visibility_status",
)
INTERVAL = "CASE WHEN t.start_ms IS NOT NULL AND t.end_ms IS NOT NULL THEN t.end_ms-t.start_ms END"
EFFECTIVE = "COALESCE(" + INTERVAL + ",t.duration_ms)"
FIELDS = {
    "run_id": "t.run_id", "revision": "t.revision", "source_ordinal": "t.source_ordinal",
    "span_id": "t.span_id", "kind": "t.kind", "name": "t.name", "operation": "t.operation",
    "status": "t.status", "agent_id": "t.agent_id", "segment_id": "t.segment_id",
    "turn_id": "t.turn_id", "parent_id": "t.parent_id", "stream_id": "t.stream_id",
    "sequence": "t.source_sequence", "clock_id": "t.clock_id", "start_ms": "t.start_ms",
    "end_ms": "t.end_ms",
    "source_reported_duration_ms": "CASE WHEN t.duration_basis='source_reported' THEN t.duration_ms END",
    "interval_duration_ms": INTERVAL, "duration_ms": EFFECTIVE,
    "duration_basis": "t.duration_basis", "duration_scope": "t.duration_scope",
    "skill_name": "t.skill_name", "skill_action": "t.skill_action",
    "proposal_id": "t.proposal_id", "call_id": "t.call_id",
    "invocation_id": "t.invocation_id", "attempt": "t.attempt", "source_refs": "t.source_refs",
    "context_id": "t.context_id", "visibility_status": "t.visibility_status",
    "visibility_issues": "t.visibility_issues",
}
DEFAULT_FIELDS = (
    "run_id", "revision", "span_id", "kind", "name", "status", "skill_name",
    "skill_action", "duration_ms", "start_ms", "end_ms", "source_refs",
)
BASE_FROM = """
    FROM trace_objects t
    JOIN trace_revisions i ON i.project_id=t.project_id AND i.run_id=t.run_id
        AND i.revision=t.revision AND i.projector_version=t.projector_version
    JOIN traces h ON h.project_id=t.project_id AND h.run_id=t.run_id
"""


def options(value):
    if not isinstance(value, dict) or set(value) - {"filters", "fields", "revisions", "order", "limit", "cursor"}:
        raise ValueError("Unsupported span query options")
    filters = value.get("filters", {})
    allowed = {*EXACT_FILTERS, "min_duration_ms", "max_duration_ms"}
    if not isinstance(filters, dict) or set(filters) - allowed:
        raise ValueError("Unsupported span query filter")
    normalized = {}
    for key, raw in filters.items():
        if key in ("min_duration_ms", "max_duration_ms"):
            if isinstance(raw, bool) or not isinstance(raw, (int, float)) or raw < 0:
                raise ValueError("Duration filter must be a non-negative number")
            normalized[key] = raw
            continue
        if not isinstance(raw, list) or not 1 <= len(raw) <= 50 or any(
                not isinstance(item, str) or not item or len(item) > 512 or "\x00" in item for item in raw):
            raise ValueError("Exact span filter requires 1 to 50 strings")
        normalized[key] = sorted(set(raw))
    if normalized.get("min_duration_ms", 0) > normalized.get("max_duration_ms", float("inf")):
        raise ValueError("Minimum duration exceeds maximum duration")
    fields = value.get("fields", list(DEFAULT_FIELDS))
    if not isinstance(fields, list) or not 1 <= len(fields) <= len(FIELDS) or any(
            not isinstance(field, str) or field not in FIELDS for field in fields) or len(fields) != len(set(fields)):
        raise ValueError("Unsupported span query fields")
    revisions = value.get("revisions", "latest")
    if revisions not in ("latest", "all"):
        raise ValueError("revisions must be latest or all")
    order = value.get("order", "source")
    if order not in ("source", "duration_desc"):
        raise ValueError("Unsupported span query order")
    limit = value.get("limit", 50)
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    cursor = value.get("cursor")
    if cursor is not None and (not isinstance(cursor, str) or not 1 <= len(cursor) <= 4096):
        raise ValueError("Invalid span query cursor")
    return {"filters": normalized, "fields": fields, "revisions": revisions,
            "order": order, "limit": limit, "cursor": cursor}


def binding(project_id, request):
    return canonical({"version": VERSION, "project_id": project_id, "projector": PROJECTOR_VERSION,
                      **{key: request[key] for key in ("filters", "fields", "revisions", "order")}})[1]


def decode_cursor(cursor, expected):
    try:
        value = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
        if not isinstance(value, dict) or set(value) != {"v", "binding", "run_id", "revision", "ordinal", "duration_ms"}:
            raise ValueError()
        if value["v"] != VERSION or value["binding"] != expected:
            raise ValueError()
        identifier(value["run_id"], "cursor run_id")
        if type(value["revision"]) is not int or not 1 <= value["revision"] <= MAX_REVISION:
            raise ValueError()
        if type(value["ordinal"]) is not int or value["ordinal"] < 0:
            raise ValueError()
        if value["duration_ms"] is not None and (isinstance(value["duration_ms"], bool) or
                not isinstance(value["duration_ms"], (int, float)) or value["duration_ms"] < 0):
            raise ValueError()
        return value
    except (binascii.Error, UnicodeError, ValueError, TypeError):
        raise ValueError("Invalid span query cursor or query changed") from None


def identity_after(run_order):
    return f"({run_order}>:after_run OR ({run_order}=:after_run AND (t.revision>:after_revision OR (t.revision=:after_revision AND t.source_ordinal>:after_ordinal))))"


def window_options(value):
    allowed = {"anchor", "before", "after", "fields", "include", "preview_chars"}
    if not isinstance(value, dict) or set(value) - allowed:
        raise ValueError("Unsupported span window options")
    anchor = value.get("anchor")
    if not isinstance(anchor, dict) or set(anchor) != {"run_id", "revision", "span_id"}:
        raise ValueError("anchor requires run_id, revision and span_id")
    identifier(anchor["run_id"], "anchor.run_id")
    identifier(anchor["span_id"], "anchor.span_id")
    if type(anchor["revision"]) is not int or not 1 <= anchor["revision"] <= MAX_REVISION:
        raise ValueError("Invalid anchor revision")
    before, after = value.get("before", 20), value.get("after", 20)
    if any(type(item) is not int or not 0 <= item <= MAX_WINDOW_SIDE for item in (before, after)):
        raise ValueError("before and after must be between 0 and 100")
    fields = value.get("fields", list(DEFAULT_FIELDS))
    if not isinstance(fields, list) or not 1 <= len(fields) <= len(FIELDS) or any(
            not isinstance(field, str) or field not in FIELDS for field in fields) or len(fields) != len(set(fields)):
        raise ValueError("Unsupported span window fields")
    include = value.get("include", [])
    allowed_include = {"documents", "related_objects", "edges"}
    if not isinstance(include, list) or len(include) != len(set(include)) or any(
            item not in allowed_include for item in include):
        raise ValueError("Unsupported span window include")
    preview = value.get("preview_chars", 512)
    if type(preview) is not int or not 1 <= preview <= MAX_PREVIEW_CHARS:
        raise ValueError("preview_chars must be between 1 and 2048")
    return {"anchor": anchor, "before": before, "after": after, "fields": fields,
            "include": sorted(include), "preview_chars": preview}


class SpanQuery:
    def __init__(self, repository):
        self.repository = repository

    @staticmethod
    def capabilities():
        return {"version": VERSION, "fields": list(FIELDS), "filter_fields": list(EXACT_FILTERS),
                "duration_filters": ["min_duration_ms", "max_duration_ms"],
                "orders": ["source", "duration_desc"], "revision_modes": ["latest", "all"],
                "max_filter_values": 50, "max_page_size": 100, "max_item_bytes": MAX_ITEM_BYTES,
                "projector_version": PROJECTOR_VERSION, "source_content": False,
                "triggers_analysis": False, "consistency": "live_keyset",
                "window": {"version": WINDOW_VERSION, "max_before": MAX_WINDOW_SIDE,
                           "max_after": MAX_WINDOW_SIDE,
                           "include": ["documents", "related_objects", "edges"],
                           "max_preview_chars": MAX_PREVIEW_CHARS}}

    def window(self, project_id, value):
        identifier(project_id, "project_id")
        request = window_options(value)
        anchor = request["anchor"]
        params = {"project": project_id, "run": anchor["run_id"],
                  "revision": anchor["revision"], "span": anchor["span_id"],
                  "projector": PROJECTOR_VERSION}
        anchor_sql = """
            SELECT t.source_ordinal
            FROM trace_objects t JOIN trace_revisions i
              ON i.project_id=t.project_id AND i.run_id=t.run_id AND i.revision=t.revision
             AND i.projector_version=t.projector_version
            WHERE t.project_id=:project AND t.run_id=:run AND t.revision=:revision
              AND t.projector_version=:projector AND t.object_kind='span'
              AND t.span_id=:span AND i.projection_state='complete'
        """
        with self.repository.engine.connect() as db:
            found = db.execute(text(anchor_sql), params).mappings().first()
            if found is None:
                raise KeyError("Span window anchor does not exist in the completed projection")
            ordinal = found["source_ordinal"]
            lower = max(0, ordinal - request["before"])
            upper = ordinal + request["after"]
            params.update(lower=lower, upper=upper)
            selected = list(dict.fromkeys(["run_id", "revision", "source_ordinal", "span_id",
                                           *request["fields"]]))
            select = ",".join(FIELDS[key] + " AS " + key for key in selected)
            rows = list(db.execute(text(
                "SELECT " + select + BASE_FROM + " WHERE t.project_id=:project AND t.run_id=:run "
                "AND t.revision=:revision AND t.projector_version=:projector "
                "AND t.object_kind='span' AND i.projection_state='complete' "
                "AND t.source_ordinal BETWEEN :lower AND :upper ORDER BY t.source_ordinal"), params).mappings())
            max_row = db.execute(text(
                "SELECT MAX(source_ordinal) AS maximum FROM trace_objects WHERE project_id=:project "
                "AND run_id=:run AND revision=:revision AND projector_version=:projector "
                "AND object_kind='span'"), params).mappings().first()
            spans = [{key: json.loads(row[key]) if key in ("source_refs", "visibility_issues") and
                      row[key] is not None else row[key] for key in selected} for row in rows]
            span_ids = [row["span_id"] for row in rows]
            documents, related, edges, attachments_truncated = [], [], [], False
            if span_ids and request["include"]:
                documents, related, edges, attachments_truncated = self._window_attachments(
                    db, params, span_ids, request)
        first = spans[0]["source_ordinal"] if spans else ordinal
        last = spans[-1]["source_ordinal"] if spans else ordinal
        maximum = max_row["maximum"] if max_row and max_row["maximum"] is not None else ordinal
        result = {"version": WINDOW_VERSION, "projector_version": PROJECTOR_VERSION,
                  "anchor": anchor, "ordering": {"basis": "source_ordinal", "causal": False},
                  "bounds": {"first_ordinal": first, "last_ordinal": last,
                             "returned_before": ordinal - first, "returned_after": last - ordinal,
                             "has_more_before": first > 0, "has_more_after": last < maximum},
                  "spans": spans, "documents": documents, "related_objects": related,
                  "edges": edges, "attachments_truncated": attachments_truncated}
        base = {**result, "documents": [], "related_objects": [], "edges": []}
        if len(canonical(base)[0].encode()) > MAX_ITEM_BYTES:
            raise ValueError("Selected span window exceeds byte limit; request fewer fields or a smaller window")
        while len(canonical(result)[0].encode()) > MAX_ITEM_BYTES:
            candidates = [key for key in ("documents", "related_objects", "edges") if result[key]]
            if not candidates:
                raise ValueError("Selected span window exceeds byte limit; request fewer fields or a smaller window")
            key = max(candidates, key=lambda item: len(result[item]))
            del result[key][-max(1, len(result[key]) // 10):]
            result["attachments_truncated"] = True
        return result

    def _window_attachments(self, db, base_params, span_ids, request):
        params = dict(base_params)
        span_names = []
        for index, span_id in enumerate(span_ids):
            name = "window_span_" + str(index); params[name] = span_id; span_names.append(":" + name)
        span_set = set(span_ids)
        direct_edges = list(db.execute(text(
            "SELECT source_kind,source_id,relation,target_kind,target_id,position,payload "
            "FROM trace_edges WHERE project_id=:project AND run_id=:run AND revision=:revision "
            "AND projector_version=:projector AND ((source_kind='span' AND source_id IN (" +
            ",".join(span_names) + ")) OR (target_kind='span' AND target_id IN (" +
            ",".join(span_names) + "))) ORDER BY edge_ordinal LIMIT :attachment_limit"),
            {**params, "attachment_limit": MAX_WINDOW_ATTACHMENTS + 1}).mappings())
        identities = set()
        for edge in direct_edges:
            if edge["source_kind"] != "span": identities.add((edge["source_kind"], edge["source_id"]))
            if edge["target_kind"] != "span": identities.add((edge["target_kind"], edge["target_id"]))
        attached_rows = list(db.execute(text(
            "SELECT DISTINCT d.object_kind,d.object_id FROM trace_search_documents d "
            "WHERE d.project_id=:project AND d.run_id=:run AND d.revision=:revision "
            "AND d.projector_version=:projector AND d.object_kind<>'span' AND d.span_id IN (" +
            ",".join(span_names) + ") LIMIT :attachment_limit"),
            {**params, "attachment_limit": MAX_WINDOW_ATTACHMENTS + 1}).mappings())
        identities.update((row["object_kind"], row["object_id"]) for row in attached_rows)
        context_rows = list(db.execute(text(
            "SELECT DISTINCT context_id FROM trace_objects WHERE project_id=:project AND run_id=:run "
            "AND revision=:revision AND projector_version=:projector AND object_kind='span' "
            "AND span_id IN (" + ",".join(span_names) + ") AND context_id IS NOT NULL"),
            params).mappings())
        identities.update(("context", row["context_id"]) for row in context_rows)
        direct_objects = list(db.execute(text(
            "SELECT object_kind,object_id,span_id,name,context_id,source_refs FROM trace_objects "
            "WHERE project_id=:project AND run_id=:run AND revision=:revision "
            "AND projector_version=:projector AND object_kind<>'span' AND span_id IN (" +
            ",".join(span_names) + ") ORDER BY object_ordinal LIMIT :attachment_limit"),
            {**params, "attachment_limit": MAX_WINDOW_ATTACHMENTS + 1}).mappings())
        for row in direct_objects:
            identities.add((row["object_kind"], row["object_id"]))
        identity_truncated = len(identities) > MAX_WINDOW_ATTACHMENTS
        identities = set(sorted(identities)[:MAX_WINDOW_ATTACHMENTS])
        context_ids = sorted(object_id for kind, object_id in identities if kind == "context")
        context_edges = []
        if context_ids:
            names = []
            for index, object_id in enumerate(context_ids):
                name = "window_context_" + str(index); params[name] = object_id; names.append(":" + name)
            context_edges = list(db.execute(text(
                "SELECT source_kind,source_id,relation,target_kind,target_id,position,payload "
                "FROM trace_edges WHERE project_id=:project AND run_id=:run AND revision=:revision "
                "AND projector_version=:projector AND source_kind='context' AND relation='context_message' "
                "AND source_id IN (" + ",".join(names) + ") ORDER BY edge_ordinal LIMIT :attachment_limit"),
                {**params, "attachment_limit": MAX_WINDOW_ATTACHMENTS + 1}).mappings())
            identities.update((edge["target_kind"], edge["target_id"]) for edge in context_edges)
            identity_truncated = identity_truncated or len(identities) > MAX_WINDOW_ATTACHMENTS
            identities = set(sorted(identities)[:MAX_WINDOW_ATTACHMENTS])
        objects, raw_object_count = [], 0
        if "related_objects" in request["include"] and identities:
            clauses = []
            for index, (kind, object_id) in enumerate(sorted(identities)):
                kind_name, id_name = "window_kind_" + str(index), "window_object_" + str(index)
                params[kind_name], params[id_name] = kind, object_id
                clauses.append("(object_kind=:" + kind_name + " AND object_id=:" + id_name + ")")
            raw_objects = list(db.execute(text(
                "SELECT object_kind,object_id,span_id,name,context_id,source_refs FROM trace_objects "
                "WHERE project_id=:project AND run_id=:run AND revision=:revision "
                "AND projector_version=:projector AND (" + " OR ".join(clauses) +
                ") ORDER BY object_ordinal LIMIT :attachment_limit"),
                {**params, "attachment_limit": MAX_WINDOW_ATTACHMENTS + 1}).mappings())
            raw_object_count = len(raw_objects)
            objects = [{**{key: row[key] for key in ("object_kind", "object_id", "span_id", "name", "context_id")},
                        "source_refs": json.loads(row["source_refs"])} for row in raw_objects[:MAX_WINDOW_ATTACHMENTS]]
        documents, raw_document_count = [], 0
        if "documents" in request["include"]:
            object_conditions = ""
            if identities:
                doc_clauses = []
                for index, (kind, object_id) in enumerate(sorted(identities)):
                    kind_name, id_name = "window_doc_kind_" + str(index), "window_doc_object_" + str(index)
                    params[kind_name], params[id_name] = kind, object_id
                    doc_clauses.append("(object_kind=:" + kind_name + " AND object_id=:" + id_name + ")")
                object_conditions = " OR " + " OR ".join(doc_clauses)
            raw_documents = list(db.execute(text(
                "SELECT object_kind,object_id,span_id,field,text,text_state,source_refs "
                "FROM trace_search_documents WHERE project_id=:project AND run_id=:run "
                "AND revision=:revision AND projector_version=:projector AND (span_id IN (" +
                ",".join(span_names) + ")" + object_conditions + ") ORDER BY document_ordinal "
                "LIMIT :attachment_limit"), {**params, "attachment_limit": MAX_WINDOW_ATTACHMENTS + 1}).mappings())
            raw_document_count = len(raw_documents)
            for row in raw_documents[:MAX_WINDOW_ATTACHMENTS]:
                preview = row["text"][:request["preview_chars"]]
                documents.append({"object_kind": row["object_kind"], "object_id": row["object_id"],
                                  "span_id": row["span_id"], "field": row["field"], "preview": preview,
                                  "preview_truncated": len(preview) < len(row["text"]),
                                  "text_state": row["text_state"],
                                  "source_refs": json.loads(row["source_refs"])})
        output_edges = []
        all_edges = [*direct_edges, *context_edges]
        if "edges" in request["include"]:
            output_edges = [{**{key: edge[key] for key in ("source_kind", "source_id", "relation",
                             "target_kind", "target_id", "position")},
                             "payload": json.loads(edge["payload"])}
                            for edge in all_edges[:MAX_WINDOW_ATTACHMENTS]]
        truncated = (len(direct_objects) > MAX_WINDOW_ATTACHMENTS or
                     raw_object_count > MAX_WINDOW_ATTACHMENTS or
                     raw_document_count > MAX_WINDOW_ATTACHMENTS or identity_truncated or
                     len(direct_edges) > MAX_WINDOW_ATTACHMENTS or
                     len(context_edges) > MAX_WINDOW_ATTACHMENTS or
                     len(all_edges) > MAX_WINDOW_ATTACHMENTS)
        return documents, objects, output_edges, truncated

    def query(self, project_id, value):
        identifier(project_id, "project_id")
        request = options(value)
        digest = binding(project_id, request)
        params = {"project": project_id, "projector": PROJECTOR_VERSION,
                  "limit": request["limit"] + 1}
        conditions = ["t.project_id=:project", "t.projector_version=:projector",
                      "t.object_kind='span'", "i.projection_state='complete'"]
        if request["revisions"] == "latest":
            conditions.append("t.revision=h.latest_revision")
        for key in EXACT_FILTERS:
            if key not in request["filters"]:
                continue
            names = []
            for position, item in enumerate(request["filters"][key]):
                name = f"filter_{key}_{position}"
                params[name] = item
                names.append(":" + name)
            conditions.append("t." + key + " IN (" + ",".join(names) + ")")
        if "min_duration_ms" in request["filters"]:
            params["min_duration"] = request["filters"]["min_duration_ms"]
            conditions.append(EFFECTIVE + ">=:min_duration")
        if "max_duration_ms" in request["filters"]:
            params["max_duration"] = request["filters"]["max_duration_ms"]
            conditions.append(EFFECTIVE + "<=:max_duration")
        run_order = 't.run_id COLLATE "C"' if self.repository.postgres else "t.run_id COLLATE BINARY"
        after = decode_cursor(request["cursor"], digest) if request["cursor"] else None
        if after:
            params.update(after_run=after["run_id"], after_revision=after["revision"],
                          after_ordinal=after["ordinal"], after_duration=after["duration_ms"])
            later_identity = identity_after(run_order)
            if request["order"] == "source":
                conditions.append(later_identity)
            elif after["duration_ms"] is None:
                conditions.append(EFFECTIVE + " IS NULL AND " + later_identity)
            else:
                conditions.append("(" + EFFECTIVE + "<:after_duration OR " + EFFECTIVE + " IS NULL OR (" +
                                  EFFECTIVE + "=:after_duration AND " + later_identity + "))")
        selected = list(dict.fromkeys([*request["fields"], "run_id", "revision", "source_ordinal", "duration_ms"]))
        select = ",".join(FIELDS[key] + " AS " + key for key in selected)
        if request["order"] == "source":
            order_sql = f"{run_order},t.revision,t.source_ordinal"
        else:
            order_sql = f"CASE WHEN {EFFECTIVE} IS NULL THEN 1 ELSE 0 END,{EFFECTIVE} DESC,{run_order},t.revision,t.source_ordinal"
        sql = "SELECT " + select + BASE_FROM + " WHERE " + " AND ".join(conditions) + " ORDER BY " + order_sql + " LIMIT :limit"
        items, last, size, has_more = [], None, 0, False
        with self.repository.engine.connect().execution_options(stream_results=True, yield_per=1) as db:
            with db.execute(text(sql), params) as result:
                for row in result.mappings():
                    if len(items) == request["limit"]:
                        has_more = True
                        break
                    item = {key: json.loads(row[key]) if key in ("source_refs", "visibility_issues") and row[key] is not None else row[key]
                            for key in request["fields"]}
                    item_size = len(canonical(item)[0].encode())
                    if size + item_size > MAX_ITEM_BYTES:
                        if not items:
                            raise ValueError("Selected spans exceed page byte limit; request fewer fields")
                        has_more = True
                        break
                    items.append(item); size += item_size; last = row
        next_cursor = None
        if has_more and last is not None:
            raw = canonical({"v": VERSION, "binding": digest, "run_id": last["run_id"],
                             "revision": last["revision"], "ordinal": last["source_ordinal"],
                             "duration_ms": last["duration_ms"]})[0].encode()
            next_cursor = base64.urlsafe_b64encode(raw).decode()
        return {"items": items, "next_cursor": next_cursor, "query_digest": digest,
                "projector_version": PROJECTOR_VERSION, "consistency": "live_keyset"}
