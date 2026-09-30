"""Snapshot-bound ad-hoc object queries, metrics, lineage and sessions."""

import base64
import binascii
import json
import math
from collections import defaultdict, deque
from datetime import datetime, timezone

from sqlalchemy import text

from ..catalog import canonical
from ..traces.index import PROJECTOR_VERSION
from ..traces.service import identifier
from .scope import QueryScope

VERSION = "object-query/1"
METRICS_VERSION = "trace-metrics/1"
LINEAGE_VERSION = "object-lineage/1"
SESSION_VERSION = "trace-sessions/1"
MAX_PAGE = 1000
MAX_PAGE_BYTES = 4 * 1024 * 1024
MAX_NODES = 100000

FIELDS = {
    "run_id": "o.run_id", "revision": "o.revision", "object_kind": "o.object_kind",
    "object_id": "o.object_id", "source_ordinal": "o.source_ordinal", "span_id": "o.span_id",
    "parent_id": "o.parent_id", "kind": "o.kind", "name": "o.name",
    "operation": "o.operation", "status": "o.status", "agent_id": "o.agent_id",
    "segment_id": "o.segment_id", "turn_id": "o.turn_id", "skill_name": "o.skill_name",
    "skill_action": "o.skill_action", "duration_ms": "o.duration_ms",
    "start_at": "o.start_at", "end_at": "o.end_at",
    "session_namespace": "o.session_namespace", "session_id": "o.session_id",
    "attributes": "o.attributes", "trace_attributes": "r.attributes",
    "source_refs": "o.source_refs", "payload": "o.payload",
}
FILTER_FIELDS = {key for key in FIELDS if key not in {"attributes", "trace_attributes", "source_refs", "payload", "duration_ms", "start_at", "end_at"}}
JSON_FIELDS = {"attributes", "trace_attributes", "source_refs", "payload"}
DEFAULT_FIELDS = ["run_id", "revision", "object_kind", "object_id", "span_id", "kind", "name",
                  "status", "skill_name", "skill_action", "duration_ms", "start_at", "source_refs"]


def _utc(value, name):
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(name + " must be an RFC3339 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(name + " must be an RFC3339 timestamp") from None
    if parsed.tzinfo is None:
        raise ValueError(name + " must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat()


def _json(value):
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _wire(value):
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    return value


def _path(value):
    if not isinstance(value, str) or not value or len(value) > 512:
        raise ValueError("attribute path must contain 1 to 512 characters")
    parts = value.split(".")
    if any(not part or len(part) > 128 for part in parts):
        raise ValueError("attribute path contains an invalid segment")
    return parts


def _nested(parts, value):
    result = value
    for part in reversed(parts):
        result = {part: result}
    return result


def _encode(value):
    return base64.urlsafe_b64encode(canonical(value)[0].encode()).decode()


def _decode(value, *, keys):
    try:
        parsed = json.loads(base64.b64decode(value, altchars=b"-_", validate=True))
    except (ValueError, TypeError, UnicodeError, binascii.Error):
        raise ValueError("Invalid cursor or snapshot token") from None
    if not isinstance(parsed, dict) or set(parsed) != set(keys):
        raise ValueError("Invalid cursor or snapshot token")
    return parsed


class AdvancedQuery:
    def __init__(self, repository):
        self.repository = repository
        self.scopes = QueryScope(repository)

    @staticmethod
    def capabilities():
        return {"version": VERSION, "projector_version": PROJECTOR_VERSION,
                "fields": sorted(FIELDS), "filter_fields": sorted(FILTER_FIELDS),
                "attribute_operators": ["eq", "neq", "exists"],
                "orders": ["source", "time"], "snapshot": "watermark",
                "metrics": ["count", "rate", "error_rate", "sum", "avg", "p50", "p95", "p99", "histogram"],
                "max_group_dimensions": 5, "evidence_exports": True,
                "triggers_source_read": False}

    def _watermark(self):
        return datetime.now(timezone.utc).isoformat()

    def _snapshot(self, project_id, request, digest):
        token = request.get("snapshot")
        if token:
            value = _decode(token, keys=("v", "project", "projector", "watermark", "query"))
            if value != {"v": 1, "project": project_id, "projector": PROJECTOR_VERSION,
                         "watermark": value["watermark"], "query": digest}:
                raise ValueError("Snapshot does not match this project or query")
            _utc(value["watermark"], "snapshot.watermark")
        else:
            value = {"v": 1, "project": project_id, "projector": PROJECTOR_VERSION,
                     "watermark": self._watermark(), "query": digest}
        return value, _encode(value)

    @staticmethod
    def _selection(request):
        return {key: request[key] for key in ("scope", "revisions", "filters", "attribute_filters", "time")}

    @staticmethod
    def normalize(value):
        if not isinstance(value, dict):
            raise ValueError("Query body must be an object")
        allowed = {"scope", "revisions", "filters", "attributes", "trace_attributes", "time",
                   "fields", "order", "limit", "cursor", "snapshot"}
        if set(value) - allowed:
            raise ValueError("Unsupported object query option")
        scope = value.get("scope", {"mode": "analysis"})
        revisions = value.get("revisions", "latest")
        if revisions not in ("latest", "all"):
            raise ValueError("revisions must be latest or all")
        filters = value.get("filters", {})
        if not isinstance(filters, dict) or set(filters) - FILTER_FIELDS:
            raise ValueError("Unsupported object filter")
        normalized_filters = {}
        for key, items in filters.items():
            if not isinstance(items, list) or not 1 <= len(items) <= 100:
                raise ValueError("filters." + key + " requires 1 to 100 values")
            normalized_filters[key] = items
        attributes = []
        for scope_name, key in (("object", "attributes"), ("trace", "trace_attributes")):
            predicates = value.get(key, [])
            if not isinstance(predicates, list) or len(predicates) > 20:
                raise ValueError(key + " must contain at most 20 predicates")
            for predicate in predicates:
                if not isinstance(predicate, dict) or set(predicate) - {"path", "op", "value"}:
                    raise ValueError("Invalid attribute predicate")
                op = predicate.get("op", "eq")
                if op not in ("eq", "neq", "exists") or (op != "exists" and "value" not in predicate):
                    raise ValueError("Attribute op must be eq, neq or exists")
                raw_path = predicate.get("path")
                attributes.append({"scope": scope_name, "raw_path": raw_path, "path": _path(raw_path),
                                   "op": op, "value": predicate.get("value")})
        time_value = value.get("time", {})
        if not isinstance(time_value, dict) or set(time_value) - {"from", "to"}:
            raise ValueError("time supports only from and to")
        time_value = {"from": _utc(time_value.get("from"), "time.from"),
                      "to": _utc(time_value.get("to"), "time.to")}
        if time_value["from"] and time_value["to"] and time_value["from"] > time_value["to"]:
            raise ValueError("time.from must not be later than time.to")
        fields = value.get("fields", DEFAULT_FIELDS)
        if not isinstance(fields, list) or not 1 <= len(fields) <= len(FIELDS) or any(item not in FIELDS for item in fields):
            raise ValueError("Unsupported object query fields")
        if len(fields) != len(set(fields)):
            raise ValueError("Object query fields must be unique")
        order = value.get("order", "source")
        if order not in ("source", "time"):
            raise ValueError("order must be source or time")
        limit = value.get("limit", 100)
        if type(limit) is not int or not 1 <= limit <= MAX_PAGE:
            raise ValueError("limit must be between 1 and 1000")
        for key in ("cursor", "snapshot"):
            if value.get(key) is not None and (not isinstance(value[key], str) or len(value[key]) > 8192):
                raise ValueError(key + " is invalid")
        return {"scope": scope, "revisions": revisions, "filters": normalized_filters,
                "attribute_filters": attributes, "time": time_value, "fields": fields,
                "order": order, "limit": limit, "cursor": value.get("cursor"),
                "snapshot": value.get("snapshot")}

    def _compile(self, project_id, request, scope, watermark, *, page=True):
        params = {"project": project_id, "projector": PROJECTOR_VERSION, "watermark": watermark}
        conditions = ["o.project_id=:project", "o.projector_version=:projector",
                      "r.projection_state='complete'", "r.created_at<=:watermark",
                      "r.indexed_at IS NOT NULL", "r.indexed_at<=:watermark"]
        if request["revisions"] == "latest":
            conditions.append("o.revision=(SELECT MAX(rr.revision) FROM trace_revisions rr "
                              "WHERE rr.project_id=o.project_id AND rr.run_id=o.run_id AND rr.created_at<=:watermark)")
        for key, items in request["filters"].items():
            names = []
            for index, item in enumerate(items):
                name = f"f_{key}_{index}"; params[name] = item; names.append(":" + name)
            conditions.append(FIELDS[key] + " IN (" + ",".join(names) + ")")
        for index, predicate in enumerate(request["attribute_filters"]):
            column = "o.attributes" if predicate["scope"] == "object" else "r.attributes"
            name = "attr_" + str(index)
            if self.repository.postgres:
                path_name = name + "_path"; exact_name = name + "_exact"
                params[path_name] = predicate["path"]; params[exact_name] = predicate["raw_path"]
                expression = "COALESCE(" + column + "->:" + exact_name + "," + column + "#>:" + path_name + ")"
                if predicate["op"] == "exists":
                    conditions.append(expression + " IS NOT NULL")
                else:
                    params[name + "_nested"] = json.dumps(_nested(predicate["path"], predicate["value"]),
                                                            ensure_ascii=False, separators=(",", ":"))
                    params[name + "_flat"] = json.dumps({predicate["raw_path"]: predicate["value"]},
                                                          ensure_ascii=False, separators=(",", ":"))
                    contains = "(" + column + "@>CAST(:" + name + "_flat AS jsonb) OR " + column + \
                               "@>CAST(:" + name + "_nested AS jsonb))"
                    conditions.append(contains if predicate["op"] == "eq" else "NOT " + contains)
            else:
                params[name + "_path"] = "$." + ".".join(predicate["path"])
                params[name + "_exact"] = '$."' + predicate["raw_path"].replace('"', '\\"') + '"'
                expression = "COALESCE(json_extract(" + column + ",:" + name + "_exact),json_extract(" + column + ",:" + name + "_path))"
                if predicate["op"] == "exists":
                    conditions.append(expression + " IS NOT NULL")
                else:
                    params[name] = predicate["value"] if not isinstance(predicate["value"], (dict, list)) else json.dumps(predicate["value"])
                    conditions.append(expression + ("=:" if predicate["op"] == "eq" else "<>:") + name)
        if request["time"]["from"]:
            params["time_from"] = request["time"]["from"]
            conditions.append("o.start_at>=:time_from")
        if request["time"]["to"]:
            params["time_to"] = request["time"]["to"]
            conditions.append("o.start_at<=:time_to")
        allowed = scope["allowed_objects"]
        if allowed is not None:
            clauses = []
            for index, (run_id, revision, kind, object_id) in enumerate(sorted(allowed)):
                params.update({f"a_run_{index}": run_id, f"a_rev_{index}": revision,
                               f"a_kind_{index}": kind, f"a_id_{index}": object_id})
                clauses.append(f"(o.run_id=:a_run_{index} AND o.revision=:a_rev_{index} "
                               f"AND o.object_kind=:a_kind_{index} AND o.object_id=:a_id_{index})")
            conditions.append("(" + " OR ".join(clauses or ["1=0"]) + ")")
        order = "o.run_id,o.revision,o.object_ordinal"
        cursor_fields = ("run_id", "revision", "object_ordinal")
        if request["order"] == "time":
            conditions.append("o.start_at IS NOT NULL")
            order = "o.start_at,o.run_id,o.revision,o.object_ordinal"
            cursor_fields = ("start_at", "run_id", "revision", "object_ordinal")
        if page and request["cursor"]:
            binding = canonical({key: request[key] for key in request if key not in ("cursor", "limit", "snapshot")})[1]
            cursor = _decode(request["cursor"], keys=("v", "binding", *cursor_fields))
            if cursor["v"] != 1 or cursor["binding"] != binding:
                raise ValueError("Cursor does not match this query")
            for key in cursor_fields:
                params["after_" + key] = cursor[key]
            left = "(" + ",".join("o." + key for key in cursor_fields) + ")"
            right = "(" + ",".join(":after_" + key for key in cursor_fields) + ")"
            conditions.append(left + ">" + right)
        joins = (" FROM trace_objects o JOIN trace_revisions r ON r.project_id=o.project_id "
                 "AND r.run_id=o.run_id AND r.revision=o.revision AND r.projector_version=o.projector_version")
        return joins + " WHERE " + " AND ".join(conditions), params, order, cursor_fields

    def query(self, project_id, value):
        identifier(project_id, "project_id")
        request = self.normalize(value)
        scope = self.scopes.resolve(project_id, request["scope"])
        digest_request = self._selection(request)
        digest = canonical({"v": VERSION, "project": project_id, "projector": PROJECTOR_VERSION,
                            **digest_request})[1]
        snapshot, token = self._snapshot(project_id, request, digest)
        if scope["status"] != "pass":
            return {"version": VERSION, "items": [], "next_cursor": None, "snapshot": token,
                    "query_digest": digest, "scope": self.scopes.report(scope),
                    "coverage": {"matched": 0, "unknown_status": 0}, "truncated": False}
        clause, params, order, cursor_fields = self._compile(project_id, request, scope, snapshot["watermark"])
        selected = list(dict.fromkeys([*request["fields"], "run_id", "revision", "object_ordinal"] +
                                      (["start_at"] if request["order"] == "time" else [])))
        expressions = [FIELDS[key] + " AS " + key if key in FIELDS else "o." + key for key in selected]
        params["limit"] = request["limit"] + 1
        sql = "SELECT " + ",".join(expressions) + clause + " ORDER BY " + order + " LIMIT :limit"
        rows = self.repository.rows(sql, params)
        has_more = len(rows) > request["limit"]
        rows = rows[:request["limit"]]
        items, size, unknown = [], 0, 0
        for row in rows:
            item = {key: (_json(row[key]) if key in JSON_FIELDS else _wire(row[key])) for key in request["fields"]}
            encoded = canonical(item)[0].encode()
            if size + len(encoded) > MAX_PAGE_BYTES:
                has_more = True; break
            items.append(item); size += len(encoded)
            unknown += item.get("status") == "unknown"
        next_cursor = None
        if has_more and items:
            last = rows[len(items) - 1]
            binding = canonical({key: request[key] for key in request if key not in ("cursor", "limit", "snapshot")})[1]
            next_cursor = _encode({"v": 1, "binding": binding,
                                   **{key: str(last[key]) if key == "start_at" else last[key] for key in cursor_fields}})
        return {"version": VERSION, "projector_version": PROJECTOR_VERSION, "items": items,
                "next_cursor": next_cursor, "snapshot": token, "query_digest": digest,
                "scope": self.scopes.report(scope),
                "coverage": {"matched": len(items), "unknown_status": unknown},
                "truncated": has_more}

    def _all_rows(self, project_id, value, fields, *, run_ids=None):
        request = self.normalize({**value, "fields": fields, "limit": MAX_PAGE, "cursor": None})
        scope = self.scopes.resolve(project_id, request["scope"])
        digest_request = self._selection(request)
        digest = canonical({"v": VERSION, "project": project_id, "projector": PROJECTOR_VERSION,
                            **digest_request})[1]
        snapshot, token = self._snapshot(project_id, request, digest)
        if run_ids is not None:
            selected = list(dict.fromkeys(run_ids))
            existing = request["filters"].get("run_id")
            if existing is not None:
                selected = [run_id for run_id in selected if run_id in existing]
            request = {**request, "filters": {**request["filters"], "run_id": selected}}
            if not selected:
                return iter(()), token, scope
        clause, params, order, _ = self._compile(project_id, request, scope, snapshot["watermark"], page=False)
        select = ",".join(FIELDS[key] + " AS " + key for key in fields)
        sql = "SELECT " + select + clause + " ORDER BY " + order
        def stream():
            with self.repository.engine.connect().execution_options(stream_results=True, yield_per=1000) as db:
                with db.execute(text(sql), params) as result:
                    yield from result.mappings()
        return stream(), token, scope

    def run_ids(self, project_id, value):
        request = self.normalize({**value, "fields": ["run_id"], "limit": MAX_PAGE, "cursor": None})
        scope = self.scopes.resolve(project_id, request["scope"])
        digest = canonical({"v": VERSION, "project": project_id, "projector": PROJECTOR_VERSION,
                            **self._selection(request)})[1]
        snapshot, token = self._snapshot(project_id, request, digest)
        clause, params, _, _ = self._compile(project_id, request, scope, snapshot["watermark"], page=False)
        rows = self.repository.rows("SELECT DISTINCT o.run_id" + clause + " ORDER BY o.run_id", params)
        return [row["run_id"] for row in rows], token, scope

    @staticmethod
    def _metric_options(value):
        if not isinstance(value, dict) or set(value) - {"query", "metrics", "group_by", "interval_seconds", "histogram", "exemplars"}:
            raise ValueError("Unsupported metrics options")
        metrics = value.get("metrics", ["count", "error_rate", "p50", "p95", "p99"])
        allowed = {"count", "rate", "error_rate", "sum", "avg", "p50", "p95", "p99", "histogram"}
        if not isinstance(metrics, list) or not metrics or any(item not in allowed for item in metrics):
            raise ValueError("Unsupported metric")
        group_by = value.get("group_by", [])
        if not isinstance(group_by, list) or len(group_by) > 5:
            raise ValueError("group_by supports at most five dimensions")
        for item in group_by:
            if item not in FIELDS and not item.startswith(("attributes.", "trace_attributes.")):
                raise ValueError("Unsupported metric group dimension")
        interval = value.get("interval_seconds")
        if interval is not None and (type(interval) is not int or not 1 <= interval <= 86400):
            raise ValueError("interval_seconds must be between 1 and 86400")
        exemplar_limit = value.get("exemplars", 3)
        if type(exemplar_limit) is not int or not 0 <= exemplar_limit <= 20:
            raise ValueError("exemplars must be between 0 and 20")
        boundaries = value.get("histogram", [10, 100, 1000, 10000])
        if not isinstance(boundaries, list) or len(boundaries) > 100 or any(
                not isinstance(item, (int, float)) or isinstance(item, bool) for item in boundaries):
            raise ValueError("histogram must contain at most 100 numeric boundaries")
        base_fields = ["run_id", "revision", "object_kind", "object_id", "status", "duration_ms", "start_at",
                       "attributes", "trace_attributes", "source_refs"]
        base_fields.extend(item for item in group_by if item in FIELDS and item not in base_fields)
        return {"query": value.get("query", {}), "metrics": metrics, "group_by": group_by,
                "interval": interval, "exemplar_limit": exemplar_limit,
                "boundaries": boundaries, "fields": base_fields}

    def metric_rows(self, project_id, value, *, run_ids=None):
        options = self._metric_options(value)
        rows, snapshot, scope = self._all_rows(project_id, options["query"], options["fields"],
                                               run_ids=run_ids)
        return rows, snapshot, scope, options

    def metrics_from_rows(self, rows, snapshot, scope, options):
        metrics, group_by = options["metrics"], options["group_by"]
        interval, exemplar_limit = options["interval"], options["exemplar_limit"]
        groups = defaultdict(lambda: {"count": 0, "known": 0, "errors": 0, "durations": [], "examples": []})
        for raw in rows:
            row = dict(raw); row["attributes"] = _json(row["attributes"]); row["trace_attributes"] = _json(row["trace_attributes"])
            row["source_refs"] = _json(row["source_refs"])
            keys = []
            if interval:
                if row["start_at"] is None:
                    keys.append(None)
                else:
                    stamp = datetime.fromisoformat(str(row["start_at"]).replace("Z", "+00:00")).timestamp()
                    keys.append(datetime.fromtimestamp(math.floor(stamp / interval) * interval, timezone.utc).isoformat())
            for dimension in group_by:
                if dimension.startswith(("attributes.", "trace_attributes.")):
                    prefix = "trace_attributes." if dimension.startswith("trace_attributes.") else "attributes."
                    current = row[prefix[:-1]]
                    suffix = dimension.removeprefix(prefix)
                    if isinstance(current, dict) and suffix in current:
                        current = current[suffix]
                    else:
                        for part in suffix.split("."):
                            current = current.get(part) if isinstance(current, dict) else None
                    keys.append(current)
                else:
                    keys.append(row.get(dimension))
            bucket = groups[tuple(json.dumps(item, sort_keys=True) if isinstance(item, (dict, list)) else item for item in keys)]
            bucket["count"] += 1
            if row["status"] in ("ok", "error", "cancelled"):
                bucket["known"] += 1; bucket["errors"] += row["status"] == "error"
            if row["duration_ms"] is not None:
                bucket["durations"].append(float(row["duration_ms"]))
            if len(bucket["examples"]) < exemplar_limit:
                bucket["examples"].append({key: row[key] for key in (
                    "run_id", "revision", "object_kind", "object_id", "source_refs")})
        output = []
        for key, bucket in groups.items():
            durations = sorted(bucket.pop("durations"))
            def percentile(p):
                if not durations: return None
                position = (len(durations) - 1) * p / 100
                low, high = math.floor(position), math.ceil(position)
                return durations[low] if low == high else durations[low] + (durations[high] - durations[low]) * (position - low)
            values = {}
            for metric in metrics:
                if metric == "count": values[metric] = bucket["count"]
                elif metric == "rate": values[metric] = bucket["count"] / interval if interval else None
                elif metric == "error_rate": values[metric] = bucket["errors"] / bucket["known"] if bucket["known"] else None
                elif metric == "sum": values[metric] = sum(durations) if durations else None
                elif metric == "avg": values[metric] = sum(durations) / len(durations) if durations else None
                elif metric.startswith("p"): values[metric] = percentile(int(metric[1:]))
                elif metric == "histogram":
                    boundaries = options["boundaries"]
                    values[metric] = [{"le": bound, "count": sum(item <= bound for item in durations)} for bound in boundaries]
            output.append({"dimensions": list(key), "metrics": values,
                           "known_status": bucket["known"], "unknown_status": bucket["count"] - bucket["known"],
                           "exemplars": bucket["examples"]})
        total = sum(item["metrics"].get("count", 0) if "count" in metrics else
                    item["known_status"] + item["unknown_status"] for item in output)
        unknown = sum(item["unknown_status"] for item in output)
        return {"version": METRICS_VERSION, "projector_version": PROJECTOR_VERSION,
                "snapshot": snapshot, "scope": self.scopes.report(scope), "groups": output,
                "coverage": {"matched": total, "unknown_status": unknown},
                "partial": False, "truncated": False, "warnings": []}

    def metrics(self, project_id, value):
        rows, snapshot, scope, options = self.metric_rows(project_id, value)
        return self.metrics_from_rows(rows, snapshot, scope, options)

    def lineage(self, project_id, value):
        if not isinstance(value, dict) or set(value) - {"scope", "anchor", "direction", "relations", "max_nodes", "max_bytes", "snapshot"}:
            raise ValueError("Unsupported lineage options")
        scope = self.scopes.resolve(project_id, value.get("scope", {"mode": "analysis"}))
        anchor = value.get("anchor")
        if not isinstance(anchor, dict) or set(anchor) != {"run_id", "revision", "object_kind", "object_id"}:
            raise ValueError("lineage.anchor requires run_id, revision, object_kind and object_id")
        direction = value.get("direction", "both")
        if direction not in ("ancestors", "descendants", "both"):
            raise ValueError("direction must be ancestors, descendants or both")
        relations = value.get("relations", ["parent", "retry_of", "proposal_of", "context_message", "invokes", "depends_on"])
        if not isinstance(relations, list) or not 1 <= len(relations) <= 20:
            raise ValueError("relations requires 1 to 20 values")
        max_nodes = value.get("max_nodes", 10000); max_bytes = value.get("max_bytes", 4 * 1024 * 1024)
        if type(max_nodes) is not int or not 1 <= max_nodes <= MAX_NODES:
            raise ValueError("max_nodes must be between 1 and 100000")
        if type(max_bytes) is not int or not 1024 <= max_bytes <= 64 * 1024 * 1024:
            raise ValueError("max_bytes must be between 1024 and 67108864")
        digest = canonical({"v": LINEAGE_VERSION, "project": project_id, "projector": PROJECTOR_VERSION,
                            **{key: value.get(key) for key in ("scope", "anchor", "direction", "relations", "max_nodes", "max_bytes")}})[1]
        snapshot, token = self._snapshot(project_id, value, digest)
        params = {"project": project_id, "run": anchor["run_id"], "revision": anchor["revision"],
                  "projector": PROJECTOR_VERSION, "watermark": snapshot["watermark"]}
        exists = self.repository.rows("SELECT 1 AS ok FROM trace_revisions WHERE project_id=:project "
            "AND run_id=:run AND revision=:revision AND projector_version=:projector "
            "AND projection_state='complete' AND created_at<=:watermark "
            "AND indexed_at IS NOT NULL AND indexed_at<=:watermark", params)
        if not exists:
            raise KeyError("Lineage anchor revision is unavailable in this snapshot")
        names = []
        for index, relation in enumerate(relations): params[f"rel_{index}"] = relation; names.append(f":rel_{index}")
        edges = [dict(row) for row in self.repository.rows("SELECT source_kind,source_id,relation,target_kind,target_id,position,payload "
            "FROM trace_edges WHERE project_id=:project AND run_id=:run AND revision=:revision "
            "AND projector_version=:projector AND relation IN (" + ",".join(names) + ")", params)]
        if scope["allowed_objects"] is not None:
            permitted = {(kind, object_id) for run_id, revision, kind, object_id in scope["allowed_objects"]
                         if run_id == anchor["run_id"] and revision == anchor["revision"]}
            start_key = (anchor["object_kind"], anchor["object_id"])
            if start_key not in permitted:
                raise PermissionError("Lineage anchor is outside the captured model context")
            edges = [edge for edge in edges
                     if (edge["source_kind"], edge["source_id"]) in permitted
                     and (edge["target_kind"], edge["target_id"]) in permitted]
        outgoing, incoming = defaultdict(list), defaultdict(list)
        for edge in edges:
            source = (edge["source_kind"], edge["source_id"]); target = (edge["target_kind"], edge["target_id"])
            outgoing[source].append((target, edge)); incoming[target].append((source, edge))
        start = (anchor["object_kind"], anchor["object_id"]); queue = deque([(start, 0)])
        seen, selected_edges, edge_keys, size, partial = {start: 0}, [], set(), 0, False
        while queue:
            node, depth = queue.popleft()
            candidates = []
            # Core edges point from the observed object to its parent/dependency.
            if direction in ("ancestors", "both"): candidates += outgoing[node]
            if direction in ("descendants", "both"): candidates += incoming[node]
            for target, edge in candidates:
                edge_size = len(canonical(edge)[0].encode())
                if len(seen) >= max_nodes or size + edge_size > max_bytes:
                    partial = True; queue.clear(); break
                edge_key = (edge["source_kind"], edge["source_id"], edge["relation"],
                            edge["target_kind"], edge["target_id"], edge["position"])
                if edge_key not in edge_keys:
                    selected_edges.append({**edge, "payload": _json(edge["payload"]) }); size += edge_size
                    edge_keys.add(edge_key)
                if target not in seen:
                    seen[target] = depth + 1; queue.append((target, depth + 1))
        facts = {}
        keys = list(seen)
        for offset in range(0, len(keys), 500):
            clauses = []
            node_params = dict(params)
            for index, (kind, object_id) in enumerate(keys[offset:offset + 500]):
                node_params[f"kind_{index}"] = kind; node_params[f"id_{index}"] = object_id
                clauses.append(f"(object_kind=:kind_{index} AND object_id=:id_{index})")
            rows = self.repository.rows("SELECT object_kind,object_id,span_id,kind,name,status,source_refs "
                "FROM trace_objects WHERE project_id=:project AND run_id=:run AND revision=:revision "
                "AND projector_version=:projector AND (" + " OR ".join(clauses) + ")", node_params)
            facts.update({(row["object_kind"], row["object_id"]): row for row in rows})
        nodes, unknown = [], 0
        for (kind, object_id), depth in seen.items():
            fact = facts.get((kind, object_id), {})
            status = fact.get("status"); unknown += status in (None, "unknown")
            nodes.append({"object_kind": kind, "object_id": object_id, "depth": depth,
                          "span_id": fact.get("span_id"), "kind": fact.get("kind"),
                          "name": fact.get("name"), "status": status,
                          "source_refs": _json(fact.get("source_refs", "[]"))})
        return {"version": LINEAGE_VERSION, "projector_version": PROJECTOR_VERSION,
                "anchor": anchor, "nodes": nodes, "edges": selected_edges, "partial": partial,
                "snapshot": token, "coverage": {"matched": len(nodes), "unknown_status": unknown},
                "truncated": partial,
                "warnings": ["node_or_byte_budget_reached"] if partial else [],
                "scope": self.scopes.report(scope)}

    def sessions(self, project_id, value):
        query = self.normalize(value or {})
        scope = self.scopes.resolve(project_id, query["scope"])
        digest = canonical({"v": VERSION, "project": project_id, "projector": PROJECTOR_VERSION,
                            **self._selection(query)})[1]
        snapshot, token = self._snapshot(project_id, query, digest)
        clause, params, _, _ = self._compile(project_id, query, scope, snapshot["watermark"], page=False)
        rows = self.repository.rows("SELECT o.session_namespace,o.session_id,o.run_id,o.revision,"
            "o.object_kind,o.object_id,o.status,o.start_at,o.end_at,o.source_refs" + clause +
            " AND o.session_id IS NOT NULL ORDER BY o.start_at,o.run_id,o.revision,o.object_ordinal", params)
        groups = {}
        for row in rows:
            key = (row["session_namespace"], row["session_id"])
            bucket = groups.setdefault(key, {"session_namespace": key[0], "session_id": key[1],
                "object_count": 0, "run_ids": set(), "start_at": None, "end_at": None,
                "unknown_status": 0, "exemplars": []})
            bucket["object_count"] += 1; bucket["run_ids"].add(row["run_id"])
            bucket["unknown_status"] += row["status"] in (None, "unknown")
            start, end = _wire(row["start_at"]), _wire(row["end_at"])
            if start is not None and (bucket["start_at"] is None or start < bucket["start_at"]): bucket["start_at"] = start
            if end is not None and (bucket["end_at"] is None or end > bucket["end_at"]): bucket["end_at"] = end
            if len(bucket["exemplars"]) < 3:
                bucket["exemplars"].append({"run_id": row["run_id"], "revision": row["revision"],
                    "object_kind": row["object_kind"], "object_id": row["object_id"],
                    "source_refs": _json(row["source_refs"])})
        items = []
        for bucket in groups.values():
            bucket["trace_count"] = len(bucket.pop("run_ids")); items.append(bucket)
        items.sort(key=lambda item: (item["start_at"] or "", item["session_namespace"] or "", item["session_id"]))
        truncated = len(items) > query["limit"]; items = items[:query["limit"]]
        return {"version": SESSION_VERSION, "projector_version": PROJECTOR_VERSION,
                "snapshot": token, "items": items, "truncated": truncated,
                "coverage": {"matched": sum(item["object_count"] for item in items),
                             "unknown_status": sum(item["unknown_status"] for item in items)},
                "scope": self.scopes.report(scope)}

    def session_timeline(self, project_id, value):
        if not isinstance(value, dict) or not isinstance(value.get("session_id"), str):
            raise ValueError("session_id is required")
        body = dict(value); session_id = body.pop("session_id"); namespace = body.pop("session_namespace", None)
        filters = dict(body.get("filters", {})); filters["session_id"] = [session_id]
        if namespace is not None: filters["session_namespace"] = [namespace]
        body.update(filters=filters, order="time")
        return self.query(project_id, body)
