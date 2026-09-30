"""Bounded server-side object search, traversal, batching and aggregation."""

import json
import re
import math
import statistics
from collections import defaultdict

from sqlalchemy import text

from ..catalog import canonical
from ..traces.index import PROJECTOR_VERSION
from ..traces.lookup import decode, encode
from ..traces.service import identifier
from .scope import QueryScope
from .search import (_check_regex_policy, _like, _portable_pattern,
                     _snippet, _sqlite_pattern, validate_postgres_regex)
from .spans import EFFECTIVE

VERSION = "object-analysis/2"
FACET_VERSION = "object-facets/1"
MAX_RUNS = 5000
MAX_OBJECTS = 50_000
MAX_RETURN_OBJECTS = 1000
MAX_EVIDENCE = 20
MAX_STEPS = 4
MAX_SCAN_DOCUMENTS = 100_000
STATEMENT_TIMEOUT_MS = 2_000
OBJECT_KINDS = ("span", "message", "context", "tool_call", "annotation")
FILTER_FIELDS = (
    "run_id", "object_kind", "span_id", "kind", "name", "operation", "status",
    "parent_id", "agent_id", "segment_id", "turn_id", "skill_name", "skill_action",
    "proposal_id", "call_id", "invocation_id",
    "model_provider", "requested_model", "response_model", "usage_completeness",
)
TRACE_FILTER_FIELDS = ("trace_query_id", "trace_env_id", "trace_harness", "trace_model", "trace_status")
COVERAGE_FILTER_FIELDS = ("coverage_tools", "coverage_model_requests", "coverage_messages",
                          "coverage_contexts", "coverage_timing")
GROUP_FIELDS = (
    "run_id", "object_kind", "kind", "name", "operation", "status", "agent_id",
    "segment_id", "turn_id", "skill_name",
    "skill_action", "skill_presence", "path", "text_match",
    "trace_query_id", "trace_env_id", "trace_harness", "trace_model", "trace_status",
    "model_provider", "requested_model", "response_model", "usage_completeness",
    *COVERAGE_FILTER_FIELDS,
)
FACET_FIELDS = tuple(field for field in GROUP_FIELDS if field not in ("path", "text_match"))
EDGE_RELATIONS = ("parent", "retry_of", "proposal_of", "context_message", "invokes", "depends_on")
ROW_FIELDS = (
    "run_id", "revision", "object_kind", "object_id", "span_id", "source_ordinal", "kind",
    "name", "operation", "status", "parent_id", "agent_id", "segment_id", "turn_id",
    "skill_name", "skill_action", "proposal_id",
    "call_id", "invocation_id", "attempt", "start_ms", "end_ms", "duration_ms", "source_refs",
    "trace_query_id", "trace_env_id", "trace_harness", "trace_model", "trace_status",
    "capture_coverage",
    "model_provider", "requested_model", "response_model", "usage_completeness",
    "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "reasoning_tokens",
)
SELECT_FIELDS = tuple(key for key in ROW_FIELDS if key not in (
    "duration_ms", "source_refs", "trace_query_id", "trace_env_id", "trace_harness",
    "trace_model", "trace_status", "capture_coverage"))
TRACE_SELECT = ("i.query_id_hex AS trace_query_id,i.env_id_hex AS trace_env_id,"
                "i.harness_hex AS trace_harness,i.model_hex AS trace_model,"
                "i.status_hex AS trace_status,i.capture_coverage")


def _strings(value, name, *, allowed=None, maximum=50):
    if value is None:
        return []
    if not isinstance(value, list) or not 1 <= len(value) <= maximum or any(
            not isinstance(item, str) or not item or len(item) > 512 or "\x00" in item for item in value):
        raise ValueError(name + " requires 1 to " + str(maximum) + " non-empty strings")
    result = sorted(set(value))
    if allowed and any(item not in allowed for item in result):
        raise ValueError("Unsupported " + name)
    return result


def _filters(value):
    if value is None:
        return {}
    allowed = {*FILTER_FIELDS, *TRACE_FILTER_FIELDS, *COVERAGE_FILTER_FIELDS,
               "has_skill", "min_duration_ms", "max_duration_ms"}
    if not isinstance(value, dict) or set(value) - allowed:
        raise ValueError("Unsupported object filter")
    result = {}
    for key, raw in value.items():
        if key == "has_skill":
            if type(raw) is not bool:
                raise ValueError("has_skill must be boolean")
            result[key] = raw
        elif key in ("min_duration_ms", "max_duration_ms"):
            if isinstance(raw, bool) or not isinstance(raw, (int, float)) or raw < 0:
                raise ValueError("Duration filter must be non-negative")
            result[key] = raw
        else:
            result[key] = _strings(raw, key, allowed=(OBJECT_KINDS if key == "object_kind" else
                ("complete", "partial", "missing", "unknown") if key in COVERAGE_FILTER_FIELDS else None))
    if result.get("min_duration_ms", 0) > result.get("max_duration_ms", float("inf")):
        raise ValueError("Minimum duration exceeds maximum duration")
    return result


def _text_predicate(value):
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) - {"query", "mode", "regex_syntax", "fields", "text_states"}:
        raise ValueError("Unsupported text predicate")
    query = value.get("query")
    if not isinstance(query, str) or not query or len(query) > 512 or "\x00" in query:
        raise ValueError("Text query must contain 1 to 512 characters")
    mode = value.get("mode", "literal")
    if mode not in ("literal", "regex"):
        raise ValueError("Unsupported text mode")
    regex_syntax = value.get("regex_syntax", "postgresql_are")
    if regex_syntax not in ("postgresql_are", "portable") or (mode != "regex" and regex_syntax != "postgresql_are"):
        raise ValueError("regex_syntax requires regex mode and a supported syntax")
    if mode == "regex":
        _check_regex_policy(query)
        if regex_syntax == "portable" and any(token in query for token in ("[[:", "[[.", "[[=")):
            raise ValueError("Portable word-boundary aliases cannot be mixed with POSIX bracket classes")
    fields = _strings(value.get("fields"), "text fields",
                      allowed=("name", "input", "output", "content", "request", "arguments"),
                      maximum=6) if value.get("fields") is not None else []
    states = _strings(value.get("text_states"), "text states", allowed=("exact", "truncated"),
                      maximum=2) if value.get("text_states") is not None else []
    effective_query = None
    if mode == "regex":
        effective_query = query if regex_syntax == "postgresql_are" else _portable_pattern(query)
    return {"query": query, "mode": mode, "regex_syntax": regex_syntax,
            "effective_query": effective_query, "fields": fields, "text_states": states}


def _first_regex_range(row, expression, text_value):
    if expression is not None:
        match = expression.search(text_value)
        return [(match.start(), match.end())] if match else []
    start = row.get("regex_start")
    return [(start - 1, row["regex_end"] - 1)] if start and row.get("regex_end") else []


def _sqlite_expression(predicate):
    if not predicate or predicate["effective_query"] is None:
        return None
    try:
        return re.compile(_sqlite_pattern(predicate["effective_query"]))
    except re.error:
        raise ValueError("SQLite fallback cannot evaluate this PostgreSQL ARE pattern") from None


def options(value):
    allowed = {"scope", "revisions", "filters", "text", "text_all", "steps", "group_by", "percentiles",
               "max_runs", "max_objects", "return_limit", "evidence_limit", "persist"}
    if not isinstance(value, dict) or set(value) - allowed:
        raise ValueError("Unsupported object analysis options")
    scope = value.get("scope")
    if not isinstance(scope, dict):
        raise ValueError("scope is required")
    revisions = value.get("revisions", "latest")
    if revisions not in ("latest", "all"):
        raise ValueError("revisions must be latest or all")
    if scope.get("mode") == "model_context" and revisions != "latest":
        raise ValueError("model_context only supports latest")
    steps = value.get("steps", [])
    if not isinstance(steps, list) or len(steps) > MAX_STEPS:
        raise ValueError("steps must contain at most " + str(MAX_STEPS) + " traversal steps")
    normalized_steps = []
    for step in steps:
        if not isinstance(step, dict) or set(step) - {"relation", "direction", "filters", "text", "text_all"}:
            raise ValueError("Unsupported traversal step")
        relation = step.get("relation")
        if relation not in (*EDGE_RELATIONS, "next_source"):
            raise ValueError("Unsupported traversal relation")
        direction = step.get("direction", "outgoing")
        if direction not in ("outgoing", "incoming") or (relation == "next_source" and direction != "outgoing"):
            raise ValueError("Unsupported traversal direction")
        text_all = step.get("text_all", [])
        if not isinstance(text_all, list) or len(text_all) > 4:
            raise ValueError("text_all must contain at most four predicates")
        normalized_steps.append({"relation": relation, "direction": direction,
                                 "filters": _filters(step.get("filters")),
                                 "text": _text_predicate(step.get("text")),
                                 "text_all": [_text_predicate(item) for item in text_all]})
    group_by = value.get("group_by", [])
    if not isinstance(group_by, list) or len(group_by) > 2 or len(group_by) != len(set(group_by)) or any(
            item not in GROUP_FIELDS for item in group_by):
        raise ValueError("group_by supports at most two known fields")
    percentiles = value.get("percentiles", [50, 95, 99])
    if not isinstance(percentiles, list) or not 1 <= len(percentiles) <= 5 or any(
            type(item) is not int or not 1 <= item <= 99 for item in percentiles):
        raise ValueError("percentiles must contain 1 to 5 integers between 1 and 99")
    numeric = {
        "max_runs": (value.get("max_runs", MAX_RUNS), 1, MAX_RUNS),
        "max_objects": (value.get("max_objects", 5000), 1, MAX_OBJECTS),
        "return_limit": (value.get("return_limit", 100), 0, MAX_RETURN_OBJECTS),
        "evidence_limit": (value.get("evidence_limit", 5), 0, MAX_EVIDENCE),
    }
    for name, (item, minimum, maximum) in numeric.items():
        if type(item) is not int or not minimum <= item <= maximum:
            raise ValueError(f"{name} must be between {minimum} and {maximum}")
    persist = value.get("persist")
    if persist is not None:
        if not isinstance(persist, dict) or set(persist) != {"analyzer", "analyzer_version"}:
            raise ValueError("persist requires analyzer and analyzer_version")
        for key in ("analyzer", "analyzer_version"):
            identifier(persist[key], "persist." + key)
    text_all = value.get("text_all", [])
    if not isinstance(text_all, list) or len(text_all) > 4:
        raise ValueError("text_all must contain at most four predicates")
    return {"scope": scope, "revisions": revisions, "filters": _filters(value.get("filters")),
            "text": _text_predicate(value.get("text")),
            "text_all": [_text_predicate(item) for item in text_all], "steps": normalized_steps,
            "group_by": group_by, "percentiles": sorted(set(percentiles)),
            **{key: item[0] for key, item in numeric.items()}, "persist": persist}


def facet_options(value):
    allowed = {"revisions", "field", "filters", "limit", "max_runs"}
    if not isinstance(value, dict) or set(value) - allowed:
        raise ValueError("Unsupported object facet options")
    revisions = value.get("revisions", "latest")
    if revisions not in ("latest", "all"):
        raise ValueError("revisions must be latest or all")
    field = value.get("field")
    custom = isinstance(field, str) and field.startswith(("attributes.", "trace_attributes."))
    if field not in FACET_FIELDS and not custom:
        raise ValueError("Unsupported object facet field")
    if custom and (len(field) > 512 or not re.fullmatch(r"(?:attributes|trace_attributes)\.[A-Za-z0-9_.:-]+", field)):
        raise ValueError("Invalid attribute facet path")
    limit = value.get("limit", 50)
    max_runs = value.get("max_runs", MAX_RUNS)
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("facet limit must be between 1 and 100")
    if type(max_runs) is not int or not 1 <= max_runs <= MAX_RUNS:
        raise ValueError("max_runs must be between 1 and 5000")
    return {"revisions": revisions, "field": field, "filters": _filters(value.get("filters")),
            "limit": limit, "max_runs": max_runs}


def _add_filter_conditions(filters, alias, params, conditions, prefix):
    for key in FILTER_FIELDS:
        if key not in filters:
            continue
        names = []
        for index, item in enumerate(filters[key]):
            name = f"{prefix}_{key}_{index}"; params[name] = item; names.append(":" + name)
        conditions.append(f"{alias}.{key} IN (" + ",".join(names) + ")")
    if "has_skill" in filters:
        conditions.append(f"{alias}.skill_name IS " + ("NOT NULL" if filters["has_skill"] else "NULL"))
    if "min_duration_ms" in filters:
        params[prefix + "_min_duration"] = filters["min_duration_ms"]
        conditions.append(EFFECTIVE.replace("t.", alias + ".") + ">=:" + prefix + "_min_duration")
    if "max_duration_ms" in filters:
        params[prefix + "_max_duration"] = filters["max_duration_ms"]
        conditions.append(EFFECTIVE.replace("t.", alias + ".") + "<=:" + prefix + "_max_duration")


def _add_trace_conditions(filters, params, conditions, prefix, postgres):
    columns = {"trace_query_id": "i.query_id_hex", "trace_env_id": "i.env_id_hex",
               "trace_harness": "i.harness_hex", "trace_model": "i.model_hex",
               "trace_status": "i.status_hex"}
    for key, column in columns.items():
        if key not in filters:
            continue
        names = []
        for index, item in enumerate(filters[key]):
            name = f"{prefix}_{key}_{index}"; params[name] = encode(item); names.append(":" + name)
        conditions.append(column + " IN (" + ",".join(names) + ")")
    for key in COVERAGE_FILTER_FIELDS:
        if key not in filters:
            continue
        field = key.removeprefix("coverage_")
        names = []
        for index, item in enumerate(filters[key]):
            name = f"{prefix}_{key}_{index}"; params[name] = item; names.append(":" + name)
        expression = ("CAST(i.capture_coverage AS jsonb)->>'" + field + "'" if postgres else
                      "json_extract(i.capture_coverage,'$." + field + "')")
        conditions.append(expression + " IN (" + ",".join(names) + ")")


def _item(row):
    value = {name: row[name] for name in ROW_FIELDS}
    for key in TRACE_FILTER_FIELDS:
        value[key] = decode(value[key])
    value["capture_coverage"] = json.loads(value["capture_coverage"] or "{}")
    value["source_refs"] = json.loads(value["source_refs"])
    value.update(text_matches=[], path=[])
    return value


def _identity(row):
    return row["run_id"], row["revision"], row["object_kind"], row["object_id"]


def _public(row):
    return {key: row.get(key) for key in ROW_FIELDS if key not in ("source_ordinal",)} | {
        "source_refs": row.get("source_refs", []), "text_matches": row.get("text_matches", []),
        "path": row.get("path", []),
    }


class ObjectAnalysis:
    def __init__(self, repository, results=None):
        self.repository = repository
        self.scopes = QueryScope(repository)
        self.results = results

    @staticmethod
    def capabilities():
        return {"version": VERSION, "scope_modes": ["analysis", "model_context"],
                "filter_fields": [*FILTER_FIELDS, *TRACE_FILTER_FIELDS, *COVERAGE_FILTER_FIELDS,
                                  "has_skill", "min_duration_ms", "max_duration_ms"],
                "text_modes": ["literal", "regex"],
                "regex_dialect": "postgresql_are",
                "regex_syntaxes": ["postgresql_are", "portable"],
                "relations": [*EDGE_RELATIONS, "next_source"], "group_fields": list(GROUP_FIELDS),
                "max_steps": MAX_STEPS, "max_text_predicates": 5,
                "max_runs": MAX_RUNS, "max_objects": MAX_OBJECTS,
                "max_return_objects": MAX_RETURN_OBJECTS, "max_evidence_per_group": MAX_EVIDENCE,
                "run_sets": True, "funnels": True,
                "facets": {"version": FACET_VERSION, "fields": list(FACET_FIELDS),
                           "max_values": 100},
                "usage_metrics": ["input_tokens", "output_tokens", "cache_read_tokens",
                                  "cache_write_tokens", "reasoning_tokens"],
                "projector_version": PROJECTOR_VERSION, "triggers_source_read": False}

    def facets(self, project_id, value):
        identifier(project_id, "project_id")
        request = facet_options(value)
        scope = {"mode": "analysis", "scope_digest": "analysis", "allowed_objects": None}
        revisions = self._resolve_revisions(project_id, request, scope)
        digest = canonical({"version": FACET_VERSION, "project_id": project_id,
                            "projector": PROJECTOR_VERSION, **request})[1]
        if not revisions:
            return {"version": FACET_VERSION, "projector_version": PROJECTOR_VERSION,
                    "query_digest": digest, "field": request["field"], "items": [],
                    "matched_objects": 0, "distinct_runs": 0, "group_count": 0,
                    "missing_count": 0, "truncated": False, "resolved_revisions": []}
        params = {"project": project_id, "projector": PROJECTOR_VERSION,
                  "limit": request["limit"] + 1}
        conditions = ["t.project_id=:project", "t.projector_version=:projector",
                      "i.projection_state='complete'"]
        if request["revisions"] == "latest":
            conditions.append("t.revision=h.latest_revision")
        _add_filter_conditions(request["filters"], "t", params, conditions, "facet")
        _add_trace_conditions(request["filters"], params, conditions, "facet", self.repository.postgres)
        field = request["field"]
        if field == "skill_presence":
            expression = "CASE WHEN t.skill_name IS NULL THEN 'without_skill' ELSE 'with_skill' END"
        elif field in TRACE_FILTER_FIELDS:
            expression = {"trace_query_id": "i.query_id_hex", "trace_env_id": "i.env_id_hex",
                          "trace_harness": "i.harness_hex", "trace_model": "i.model_hex",
                          "trace_status": "i.status_hex"}[field]
        elif field in COVERAGE_FILTER_FIELDS:
            key = field.removeprefix("coverage_")
            expression = ("CAST(i.capture_coverage AS jsonb)->>'" + key + "'" if self.repository.postgres
                          else "json_extract(i.capture_coverage,'$." + key + "')")
        elif field.startswith(("attributes.", "trace_attributes.")):
            trace_attribute = field.startswith("trace_attributes.")
            raw_path = field.split(".", 1)[1]
            path = raw_path.split(".")
            column = "i.attributes" if trace_attribute else "t.attributes"
            params["facet_attribute_path"] = path if self.repository.postgres else "$." + ".".join(path)
            params["facet_attribute_exact"] = raw_path if self.repository.postgres else '$."' + raw_path.replace('"', '\\"') + '"'
            expression = ("COALESCE(" + column + "->>:facet_attribute_exact," + column + "#>>:facet_attribute_path)"
                          if self.repository.postgres else "COALESCE(json_extract(" + column + ",:facet_attribute_exact),"
                          "json_extract(" + column + ",:facet_attribute_path))")
        else:
            expression = "t." + field
        joined = (" FROM trace_objects t JOIN trace_revisions i ON i.project_id=t.project_id "
                  "AND i.run_id=t.run_id AND i.revision=t.revision "
                  "AND i.projector_version=t.projector_version JOIN traces h "
                  "ON h.project_id=t.project_id AND h.run_id=t.run_id WHERE " +
                  " AND ".join(conditions))
        summary = self._rows("SELECT COUNT(*) AS matched_objects,COUNT(DISTINCT t.run_id) AS distinct_runs," +
                             "SUM(CASE WHEN " + expression + " IS NULL THEN 1 ELSE 0 END) AS missing_count," +
                             "COUNT(DISTINCT " + expression + ") AS group_count" + joined, params)[0]
        rows = self._rows("SELECT " + expression + " AS value,COUNT(*) AS count," +
                          "COUNT(DISTINCT t.run_id) AS distinct_runs" + joined + " AND " +
                          expression + " IS NOT NULL GROUP BY " + expression +
                          " ORDER BY count DESC,value LIMIT :limit", params)
        items = []
        for row in rows[:request["limit"]]:
            facet_value = decode(row["value"]) if field in TRACE_FILTER_FIELDS else row["value"]
            items.append({"value": facet_value, "count": row["count"],
                          "distinct_runs": row["distinct_runs"]})
        return {"version": FACET_VERSION, "projector_version": PROJECTOR_VERSION,
                "query_digest": digest, "field": field, "items": items,
                "matched_objects": summary["matched_objects"],
                "distinct_runs": summary["distinct_runs"], "group_count": summary["group_count"],
                "missing_count": summary["missing_count"] or 0,
                "truncated": summary["group_count"] > len(items),
                "resolved_revisions": revisions}

    def analyze(self, project_id, value):
        identifier(project_id, "project_id")
        request = options(value)
        if self.repository.postgres:
            predicates = [request["text"], *request["text_all"]]
            for step in request["steps"]:
                predicates.extend([step["text"], *step["text_all"]])
            for predicate in predicates:
                if predicate and predicate["effective_query"] is not None:
                    validate_postgres_regex(self.repository, predicate["effective_query"])
        scope = self.scopes.resolve(project_id, request["scope"])
        digest_request = {key: request[key] for key in request if key != "persist"}
        query_digest = canonical({"version": VERSION, "project_id": project_id,
                                  "projector_version": PROJECTOR_VERSION, **digest_request})[1]
        revisions = self._resolve_revisions(project_id, request, scope)
        if scope["status"] != "pass":
            result = self._response(request, scope, query_digest, [], revisions, 0)
            return self._persist(project_id, request, result)
        objects = self._seed(project_id, request, scope)
        for predicate in request["text_all"]:
            objects = self._filter_text(project_id, request, scope, objects, predicate)
        seed_count = len(objects)
        for step in request["steps"]:
            objects = self._traverse(project_id, request, scope, objects, step)
        result = self._response(request, scope, query_digest, objects, revisions, seed_count)
        return self._persist(project_id, request, result)

    def resolve_runs(self, project_id, value):
        if not isinstance(value, dict) or set(value) - {
                "scope", "revisions", "contains", "not_contains", "max_runs"}:
            raise ValueError("Unsupported run set options")
        contains = value.get("contains", [])
        excludes = value.get("not_contains", [])
        if (not isinstance(contains, list) or not isinstance(excludes, list) or
                len(contains) > 4 or len(excludes) > 4 or
                any(not isinstance(item, dict) for item in [*contains, *excludes])):
            raise ValueError("contains and not_contains support at most four object patterns")
        base = {"scope": value.get("scope"), "revisions": value.get("revisions", "latest"),
                "max_runs": value.get("max_runs", MAX_RUNS), "max_objects": MAX_OBJECTS,
                "return_limit": 0, "evidence_limit": 0}
        request = options(base)
        scope = self.scopes.resolve(project_id, request["scope"])
        universe = {(item["run_id"], item["revision"])
                    for item in self._resolve_revisions(project_id, request, scope)}

        def matches(pattern):
            query = {**base, **pattern}
            result = self.analyze(project_id, query)
            return {(item["run_id"], item["revision"])
                    for item in result["coverage"]["matched_revisions"]}

        selected = set(universe)
        for pattern in contains:
            selected &= matches(pattern)
        for pattern in excludes:
            selected -= matches(pattern)
        members = [{"run_id": run_id, "revision": revision}
                   for run_id, revision in sorted(selected)]
        return {"version": VERSION, "scope": QueryScope.report(scope),
                "candidate_count": len(universe), "member_count": len(members),
                "members": members, "truncated": False}

    def funnel(self, project_id, value):
        if not isinstance(value, dict) or set(value) - {
                "scope", "revisions", "seed", "stages", "max_runs", "max_objects",
                "sample_limit"}:
            raise ValueError("Unsupported funnel options")
        seed = value.get("seed")
        stages = value.get("stages", [])
        sample_limit = value.get("sample_limit", 20)
        if not isinstance(seed, dict) or not isinstance(stages, list) or not 1 <= len(stages) <= MAX_STEPS:
            raise ValueError("Funnel requires a seed and 1 to 4 stages")
        if type(sample_limit) is not int or not 0 <= sample_limit <= 100:
            raise ValueError("sample_limit must be between 0 and 100")
        base = {"scope": value.get("scope"), "revisions": value.get("revisions", "latest"),
                "max_runs": value.get("max_runs", MAX_RUNS),
                "max_objects": value.get("max_objects", MAX_OBJECTS),
                "return_limit": 0, "evidence_limit": 0}
        sets, rows = [], []
        for count in range(len(stages) + 1):
            result = self.analyze(project_id, {**base, **seed, "steps": stages[:count]})
            members = {(item["run_id"], item["revision"])
                       for item in result["coverage"]["matched_revisions"]}
            sets.append(members)
            rows.append({"stage": count, "matched_runs": len(members),
                         "members": [{"run_id": run, "revision": revision}
                                     for run, revision in sorted(members)[:sample_limit]]})
        interruptions = []
        for index in range(len(sets) - 1):
            missing = sorted(sets[index] - sets[index + 1])
            interruptions.append({"after_stage": index, "count": len(missing),
                                  "members": [{"run_id": run, "revision": revision}
                                              for run, revision in missing[:sample_limit]]})
        return {"version": VERSION, "stages": rows, "interruptions": interruptions,
                "completed_runs": len(sets[-1])}

    def _resolve_revisions(self, project_id, request, scope):
        if scope["mode"] == "model_context":
            target = scope["visible_to"]
            return [{"run_id": target["run_id"], "revision": target["revision"]}]
        params = {"project": project_id, "projector": PROJECTOR_VERSION}
        conditions = ["r.project_id=:project", "r.projector_version=:projector",
                      "r.projection_state='complete'"]
        if request["revisions"] == "latest":
            conditions.append("r.revision=h.latest_revision")
        run_ids = request["filters"].get("run_id", [])
        if run_ids:
            names = []
            for index, run_id in enumerate(run_ids):
                name = "revision_run_" + str(index); params[name] = run_id; names.append(":" + name)
            conditions.append("r.run_id IN (" + ",".join(names) + ")")
        rows = self._rows("SELECT r.run_id,r.revision FROM trace_revisions r JOIN traces h "
                          "ON h.project_id=r.project_id AND h.run_id=r.run_id WHERE " +
                          " AND ".join(conditions) + " ORDER BY r.run_id,r.revision", params)
        if len({row["run_id"] for row in rows}) > request["max_runs"]:
            raise ValueError("Trace batch limit exceeded; narrow the filters")
        return [{"run_id": row["run_id"], "revision": row["revision"]} for row in rows]

    def _rows(self, sql, params):
        with self.repository.engine.begin() as db:
            if self.repository.postgres:
                db.execute(text("SET LOCAL statement_timeout = '" + str(STATEMENT_TIMEOUT_MS) + "ms'"))
            return list(db.execute(text(sql), params).mappings())

    def _persist(self, project_id, request, result):
        if request["persist"] is None:
            return result
        if self.results is None:
            raise RuntimeError("Analysis result service is unavailable")
        receipt = self.results.save(project_id, request["persist"], request, result)
        return {**result, "persisted": receipt}

    def _seed(self, project_id, request, scope):
        params = {"project": project_id, "projector": PROJECTOR_VERSION}
        conditions = ["t.project_id=:project", "t.projector_version=:projector",
                      "i.projection_state='complete'"]
        if scope["mode"] == "model_context":
            params.update(scope_run=scope["visible_to"]["run_id"],
                          scope_revision=scope["visible_to"]["revision"])
            conditions.extend(["t.run_id=:scope_run", "t.revision=:scope_revision"])
        if request["revisions"] == "latest" and scope["mode"] == "analysis":
            conditions.append("t.revision=h.latest_revision")
        _add_filter_conditions(request["filters"], "t", params, conditions, "seed")
        _add_trace_conditions(request["filters"], params, conditions, "seed", self.repository.postgres)
        predicate = request["text"]
        select = ",".join("t." + key for key in SELECT_FIELDS) + "," + EFFECTIVE + \
            " AS duration_ms,t.source_refs," + TRACE_SELECT
        join = ""
        if predicate:
            join = " JOIN trace_search_documents d ON d.project_id=t.project_id AND d.run_id=t.run_id AND d.revision=t.revision AND d.projector_version=t.projector_version AND d.object_kind=t.object_kind AND d.object_id=t.object_id"
            if predicate["fields"]:
                names = []
                for index, field in enumerate(predicate["fields"]):
                    name = "text_field_" + str(index); params[name] = field; names.append(":" + name)
                conditions.append("d.field IN (" + ",".join(names) + ")")
            if predicate["text_states"]:
                names = []
                for index, state in enumerate(predicate["text_states"]):
                    name = "text_state_" + str(index); params[name] = state; names.append(":" + name)
                conditions.append("d.text_state IN (" + ",".join(names) + ")")
            params["text_query"] = predicate["effective_query"] or predicate["query"]
            if self.repository.postgres:
                if predicate["mode"] == "literal":
                    params["text_needle"] = _like(predicate["query"])
                    conditions.append("d.text LIKE :text_needle ESCAPE '!'")
                else:
                    conditions.append("d.text ~ :text_query")
        if predicate:
            select += ",d.field AS match_field,d.text AS match_text,d.text_state AS match_text_state,d.source_refs AS match_source_refs"
            if self.repository.postgres and predicate["mode"] == "regex":
                select += (",regexp_instr(d.text,:text_query,1,1,0) AS regex_start"
                           ",regexp_instr(d.text,:text_query,1,1,1) AS regex_end")
        sql = "SELECT " + select + " FROM trace_objects t JOIN trace_revisions i ON i.project_id=t.project_id AND i.run_id=t.run_id AND i.revision=t.revision AND i.projector_version=t.projector_version JOIN traces h ON h.project_id=t.project_id AND h.run_id=t.run_id" + join + " WHERE " + " AND ".join(conditions) + " ORDER BY t.run_id,t.revision,t.object_ordinal"
        found, found_runs, scanned = {}, set(), 0
        expression = _sqlite_expression(predicate) if predicate and not self.repository.postgres else None
        with self.repository.engine.begin() as db:
            if self.repository.postgres:
                db.execute(text("SET LOCAL statement_timeout = '" + str(STATEMENT_TIMEOUT_MS) + "ms'"))
            db = db.execution_options(stream_results=True, yield_per=100)
            for raw in db.execute(text(sql), params).mappings():
                scanned += 1
                if scanned > MAX_SCAN_DOCUMENTS:
                    raise ValueError("Text candidate limit exceeded; narrow the filters")
                row = dict(raw)
                if not QueryScope.permits(scope, row):
                    continue
                if predicate and not self.repository.postgres:
                    value = row["match_text"]
                    matched = (predicate["query"] in value if predicate["mode"] == "literal" else
                               expression.search(value) is not None)
                    if not matched:
                        continue
                key = _identity(row)
                item = found.get(key)
                if item is None:
                    item = _item(row)
                    found[key] = item
                    found_runs.add(item["run_id"])
                    self._check_bounds(found, request, found_runs)
                if predicate and len(item["text_matches"]) < 5:
                    ranges = (_first_regex_range(row, expression, row["match_text"])
                              if predicate["mode"] == "regex" else None)
                    item["text_matches"].append({"field": row["match_field"], "score": 1.0,
                        "text_state": row["match_text_state"],
                        "source_refs": json.loads(row["match_source_refs"]),
                        "effective_pattern": predicate["effective_query"],
                        **_snippet(predicate["query"], predicate["mode"], row["match_text"], ranges)})
        return list(found.values())

    @staticmethod
    def _check_bounds(objects, request, run_ids=None):
        if len(objects) > request["max_objects"]:
            raise ValueError("Object batch limit exceeded; narrow the filters")
        if len(run_ids if run_ids is not None else {item["run_id"] for item in objects}) > request["max_runs"]:
            raise ValueError("Trace batch limit exceeded; narrow the filters")

    def _traverse(self, project_id, request, scope, current, step):
        if not current:
            return []
        if step["relation"] == "next_source":
            return self._next_source(project_id, request, scope, current, step["filters"],
                                     [item for item in [step["text"], *step["text_all"]] if item])
        relation, direction = step["relation"], step["direction"]
        current_map = {_identity(item): item for item in current}
        runs = sorted({item["run_id"] for item in current})
        found, found_runs = {}, set()
        for offset in range(0, len(runs), 50):
            params = {"project": project_id, "projector": PROJECTOR_VERSION, "relation": relation}
            names = []
            for index, run in enumerate(runs[offset:offset + 50]):
                name = "run_" + str(index); params[name] = run; names.append(":" + name)
            conditions = ["e.project_id=:project", "e.projector_version=:projector",
                          "e.relation=:relation", "e.run_id IN (" + ",".join(names) + ")",
                          "i.projection_state='complete'"]
            if request["revisions"] == "latest" and scope["mode"] == "analysis":
                conditions.append("e.revision=h.latest_revision")
            source_kind = "e.source_kind" if direction == "outgoing" else "e.target_kind"
            source_id = "e.source_id" if direction == "outgoing" else "e.target_id"
            target_kind = "e.target_kind" if direction == "outgoing" else "e.source_kind"
            target_id = "e.target_id" if direction == "outgoing" else "e.source_id"
            conditions.extend(["t.object_kind=" + target_kind, "t.object_id=" + target_id])
            _add_filter_conditions(step["filters"], "t", params, conditions, "target")
            _add_trace_conditions(step["filters"], params, conditions, "target", self.repository.postgres)
            select = ",".join("t." + key for key in SELECT_FIELDS) + "," + EFFECTIVE + \
                " AS duration_ms,t.source_refs," + TRACE_SELECT
            sql = "SELECT " + select + ",e.source_kind,e.source_id,e.target_kind,e.target_id,e.relation,e.position,e.payload FROM trace_edges e JOIN trace_revisions i ON i.project_id=e.project_id AND i.run_id=e.run_id AND i.revision=e.revision AND i.projector_version=e.projector_version JOIN traces h ON h.project_id=e.project_id AND h.run_id=e.run_id JOIN trace_objects t ON t.project_id=e.project_id AND t.run_id=e.run_id AND t.revision=e.revision AND t.projector_version=e.projector_version WHERE " + " AND ".join(conditions) + " ORDER BY e.run_id,e.revision,e.edge_ordinal"
            for raw in self._rows(sql, params):
                row = dict(raw)
                current_kind = row["source_kind"] if direction == "outgoing" else row["target_kind"]
                current_id = row["source_id"] if direction == "outgoing" else row["target_id"]
                parent = current_map.get((row["run_id"], row["revision"], current_kind, current_id))
                if parent is None or not QueryScope.permits(scope, row):
                    continue
                item = _item(row)
                edge = {"relation": relation, "direction": direction, "source_kind": row["source_kind"],
                        "source_id": row["source_id"], "target_kind": row["target_kind"],
                        "target_id": row["target_id"], "position": row["position"],
                        "evidence": json.loads(row["payload"])}
                item.update(text_matches=[], path=[*parent.get("path", []), edge])
                found.setdefault(_identity(item), item)
                found_runs.add(item["run_id"])
                self._check_bounds(found, request, found_runs)
        objects = list(found.values())
        for predicate in [item for item in [step["text"], *step["text_all"]] if item]:
            objects = self._filter_text(project_id, request, scope, objects, predicate)
        return objects

    def _next_source(self, project_id, request, scope, current, filters, predicates):
        by_run = defaultdict(list)
        for item in current:
            if item["source_ordinal"] is not None:
                by_run[item["run_id"]].append(item)
        found, found_runs = {}, set()
        runs = sorted(by_run)
        for offset in range(0, len(runs), 50):
            params = {"project": project_id, "projector": PROJECTOR_VERSION}
            names = []
            for index, run in enumerate(runs[offset:offset + 50]):
                name = "run_" + str(index); params[name] = run; names.append(":" + name)
            conditions = ["t.project_id=:project", "t.projector_version=:projector",
                          "t.object_kind='span'", "t.run_id IN (" + ",".join(names) + ")",
                          "i.projection_state='complete'"]
            if request["revisions"] == "latest" and scope["mode"] == "analysis":
                conditions.append("t.revision=h.latest_revision")
            _add_filter_conditions(filters, "t", params, conditions, "next")
            _add_trace_conditions(filters, params, conditions, "next", self.repository.postgres)
            select = ",".join("t." + key for key in SELECT_FIELDS) + "," + EFFECTIVE + \
                " AS duration_ms,t.source_refs," + TRACE_SELECT
            sql = "SELECT " + select + " FROM trace_objects t JOIN trace_revisions i ON i.project_id=t.project_id AND i.run_id=t.run_id AND i.revision=t.revision AND i.projector_version=t.projector_version JOIN traces h ON h.project_id=t.project_id AND h.run_id=t.run_id WHERE " + " AND ".join(conditions) + " ORDER BY t.run_id,t.revision,t.source_ordinal"
            candidates = defaultdict(list)
            for row in self._rows(sql, params):
                row = dict(row)
                if QueryScope.permits(scope, row):
                    row = _item(row)
                    candidates[(row["run_id"], row["revision"])].append(row)
            flat = [item for values in candidates.values() for item in values]
            for predicate in predicates:
                flat = self._filter_text(project_id, request, scope, flat, predicate)
            candidates = defaultdict(list)
            for item in flat:
                candidates[(item["run_id"], item["revision"])].append(item)
            for run in runs[offset:offset + 50]:
                for parent in by_run[run]:
                    target = next((item for item in candidates[(parent["run_id"], parent["revision"])]
                                   if item["source_ordinal"] is not None and
                                   item["source_ordinal"] > parent["source_ordinal"]), None)
                    if target is None:
                        continue
                    item = {name: target[name] for name in ROW_FIELDS}
                    edge = {"relation": "next_source", "direction": "outgoing",
                            "source_kind": parent["object_kind"], "source_id": parent["object_id"],
                            "target_kind": item["object_kind"], "target_id": item["object_id"],
                            "position": None, "evidence": {"basis": "source_ordinal",
                                "source_ordinal": parent["source_ordinal"],
                                "target_ordinal": target["source_ordinal"]}}
                    item.update(text_matches=target.get("text_matches", []),
                                path=[*parent.get("path", []), edge])
                    found.setdefault(_identity(item), item)
                    found_runs.add(item["run_id"])
                    self._check_bounds(found, request, found_runs)
        return list(found.values())

    def _filter_text(self, project_id, request, scope, objects, predicate):
        if not predicate or not objects:
            return objects
        identities = {_identity(item): item for item in objects}
        runs = sorted({item["run_id"] for item in objects})
        matched, scanned = {}, 0
        expression = _sqlite_expression(predicate) if not self.repository.postgres else None
        for offset in range(0, len(runs), 50):
            params = {"project": project_id, "projector": PROJECTOR_VERSION,
                      "text_query": predicate["effective_query"] or predicate["query"]}
            names = []
            for index, run in enumerate(runs[offset:offset + 50]):
                name = "run_" + str(index); params[name] = run; names.append(":" + name)
            conditions = ["d.project_id=:project", "d.projector_version=:projector",
                          "d.run_id IN (" + ",".join(names) + ")", "i.projection_state='complete'"]
            if request["revisions"] == "latest" and scope["mode"] == "analysis":
                conditions.append("d.revision=h.latest_revision")
            if predicate["fields"]:
                fields = []
                for index, field in enumerate(predicate["fields"]):
                    name = "field_" + str(index); params[name] = field; fields.append(":" + name)
                conditions.append("d.field IN (" + ",".join(fields) + ")")
            if predicate["text_states"]:
                states = []
                for index, state in enumerate(predicate["text_states"]):
                    name = "text_state_" + str(index); params[name] = state; states.append(":" + name)
                conditions.append("d.text_state IN (" + ",".join(states) + ")")
            if self.repository.postgres:
                if predicate["mode"] == "literal":
                    params["text_needle"] = _like(predicate["query"])
                    conditions.append("d.text LIKE :text_needle ESCAPE '!'")
                elif predicate["mode"] == "regex":
                    conditions.append("d.text ~ :text_query")
                else:
                    conditions.append(":text_query <% d.text")
            range_select = (",regexp_instr(d.text,:text_query,1,1,0) AS regex_start"
                            ",regexp_instr(d.text,:text_query,1,1,1) AS regex_end"
                            if self.repository.postgres and predicate["mode"] == "regex" else "")
            rows = self._rows("""SELECT d.run_id,d.revision,d.object_kind,d.object_id,
                d.field,d.text,d.text_state,d.source_refs""" + range_select + """ FROM trace_search_documents d
                JOIN trace_revisions i ON i.project_id=d.project_id AND i.run_id=d.run_id
                 AND i.revision=d.revision AND i.projector_version=d.projector_version
                JOIN traces h ON h.project_id=d.project_id AND h.run_id=d.run_id WHERE """ +
                " AND ".join(conditions) + " ORDER BY d.run_id,d.revision,d.document_ordinal", params)
            for row in rows:
                scanned += 1
                if scanned > MAX_SCAN_DOCUMENTS:
                    raise ValueError("Text candidate limit exceeded; narrow the filters")
                key = (row["run_id"], row["revision"], row["object_kind"], row["object_id"])
                item = identities.get(key)
                if item is None:
                    continue
                if not self.repository.postgres:
                    value = row["text"]
                    ok = (predicate["query"] in value if predicate["mode"] == "literal" else
                          expression.search(value) is not None)
                    if not ok:
                        continue
                if key not in matched:
                    matched[key] = item
                if len(item["text_matches"]) < 5:
                    ranges = (_first_regex_range(row, expression, row["text"])
                              if predicate["mode"] == "regex" else None)
                    item["text_matches"].append({"field": row["field"], "score": 1.0,
                        "text_state": row["text_state"], "source_refs": json.loads(row["source_refs"]),
                        "effective_pattern": predicate["effective_query"],
                        **_snippet(predicate["query"], predicate["mode"], row["text"], ranges)})
        return list(matched.values())

    @staticmethod
    def _group_value(item, field):
        if field == "skill_presence":
            return "with_skill" if item.get("skill_name") else "without_skill"
        if field == "path":
            return " -> ".join(edge["relation"] for edge in item.get("path", [])) or "seed"
        if field == "text_match":
            for match in reversed(item.get("text_matches", [])):
                if match.get("match_ranges"):
                    region = match["match_ranges"][0]
                    return match["snippet"][region["start"]:region["end"]]
            return None
        if field in COVERAGE_FILTER_FIELDS:
            return item.get("capture_coverage", {}).get(field.removeprefix("coverage_"))
        return item.get(field)

    def _groups(self, objects, request):
        grouped = defaultdict(list)
        fields = request["group_by"]
        for item in objects:
            key = tuple(self._group_value(item, field) for field in fields) if fields else ("all",)
            grouped[key].append(item)
        groups = []
        for key, values in sorted(grouped.items(), key=lambda pair: tuple("" if item is None else str(item) for item in pair[0])):
            durations = sorted(item["duration_ms"] for item in values if item.get("duration_ms") is not None)
            known_statuses = [item.get("status") for item in values
                              if item.get("status") in ("ok", "error", "running", "cancelled")]
            unknown_statuses = len(values) - len(known_statuses)
            errors = sum(status == "error" for status in known_statuses)
            status_issues = []
            if not known_statuses:
                status_issues.append({
                    "code": "STATUS_DATA_MISSING",
                    "message": "原始数据未包含可确认的执行状态，无法计算错误率。",
                })
            elif unknown_statuses:
                status_issues.append({
                    "code": "STATUS_DATA_PARTIAL",
                    "message": f"有 {unknown_statuses} 条执行状态未知；错误率仅按 {len(known_statuses)} 条状态明确的记录计算。",
                })
            duration = {"count": len(durations), "min_ms": min(durations) if durations else None,
                        "max_ms": max(durations) if durations else None,
                        "mean_ms": statistics.fmean(durations) if durations else None,
                        "percentiles_ms": {}}
            for percentile in request["percentiles"]:
                index = max(0, math.ceil(percentile / 100 * len(durations)) - 1) if durations else None
                duration["percentiles_ms"]["p" + str(percentile)] = durations[index] if index is not None else None
            usage = {}
            for field in ("input_tokens", "output_tokens", "cache_read_tokens",
                          "cache_write_tokens", "reasoning_tokens"):
                known = [item[field] for item in values if item.get(field) is not None]
                usage[field] = {"known_count": len(known), "sum": sum(known) if known else None}
            evidence = [_public(item) for item in values[:request["evidence_limit"]]]
            groups.append({"key": {field: value for field, value in zip(fields, key)} if fields else {},
                           "count": len(values), "distinct_runs": len({item["run_id"] for item in values}),
                           "error_count": errors,
                           "error_rate": errors / len(known_statuses) if known_statuses else None,
                           "status_coverage": {"known_count": len(known_statuses),
                                               "unknown_count": unknown_statuses,
                                               "total_count": len(values)},
                           "issues": status_issues,
                           "duration": duration, "usage": usage, "evidence": evidence})
        return groups

    def _response(self, request, scope, query_digest, objects, revisions, seed_count):
        returned = objects[:request["return_limit"]]
        matched_revisions = [{"run_id": run, "revision": revision} for run, revision in sorted(
            {(item["run_id"], item["revision"]) for item in objects})]
        return {"version": VERSION, "projector_version": PROJECTOR_VERSION,
                "query_digest": query_digest, "scope": QueryScope.report(scope),
                "coverage": {"seed_objects": seed_count, "matched_objects": len(objects),
                             "distinct_runs": len({item["run_id"] for item in objects}),
                             "resolved_revisions": revisions,
                             "matched_revisions": matched_revisions},
                "groups": self._groups(objects, request), "objects": [_public(item) for item in returned],
                "objects_truncated": len(returned) < len(objects), "persisted": None}
