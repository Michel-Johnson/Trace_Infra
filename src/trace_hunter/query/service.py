"""Live keyset queries; immutable selections are a separate operation.

Cursor values are seek positions, not credentials. Every call still requires
project authorization. Predicates are compiled from a fixed field allowlist;
no caller SQL, source bodies, analysis jobs or implicit index rebuilds.
"""

import base64
import binascii
import json

from sqlalchemy import text

from ..catalog import canonical
from ..traces.index import PROJECTOR_VERSION
from ..traces.service import MAX_REVISION, identifier
from ..traces.lookup import FIELDS as LOOKUP_FIELDS, INDEXED_FIELDS, PREFIX_HEX_LENGTH, LookupUnavailable, decode, encode

VERSION = "trace-query/1"
DEFAULT_ORDER = "run_id_asc_revision_asc"
ORDERS = (DEFAULT_ORDER, "created_at_desc")
MAX_ITEM_BYTES = 2 * 1024 * 1024
METADATA_FIELDS = frozenset(LOOKUP_FIELDS)
FIELDS = {
    "run_id": "r.run_id", "revision": "r.revision",
    "content_digest": "r.content_digest", "format_version": "r.format_version",
    "created_at": "r.created_at", "query_id": "r.query_id_hex", "env_id": "r.env_id_hex",
    "harness": "r.harness_hex", "model": "r.model_hex", "status": "r.status_hex",
    "index_state": "CASE WHEN r.projector_version=:projector THEN r.projection_state ELSE 'unindexed' END",
    "record_count": "CASE WHEN r.projector_version=:projector THEN r.record_count END",
    "model_count": "CASE WHEN r.projector_version=:projector THEN r.model_count END",
    "tool_count": "CASE WHEN r.projector_version=:projector THEN r.tool_count END",
    "indexed_at": "CASE WHEN r.projector_version=:projector THEN r.indexed_at END",
}
DEFAULT_FIELDS = ("run_id", "revision", "content_digest", "index_state", "record_count")
FILTERS = {key: FIELDS[key] for key in (
    "run_id", "content_digest", "query_id", "env_id", "harness", "model", "status", "index_state",
)}
BASE_FROM = """
    FROM trace_revisions r
    JOIN traces h ON h.project_id=r.project_id AND h.run_id=r.run_id
"""


def field_sql(key, postgres):
    return FIELDS[key]


def options(value):
    if not isinstance(value, dict) or set(value) - {"filters", "fields", "revisions", "limit", "cursor", "order"}:
        raise ValueError("Unsupported query options")
    filters = value.get("filters", {})
    if not isinstance(filters, dict) or set(filters) - FILTERS.keys():
        raise ValueError("Unsupported query filter")
    normalized = {}
    for key, items in filters.items():
        if not isinstance(items, list) or not 1 <= len(items) <= 50:
            raise ValueError("Filter requires 1 to 50 values")
        for item in items:
            if key in METADATA_FIELDS:
                if item is not None and (not isinstance(item, str) or len(item) > 4096):
                    raise ValueError("Metadata filter requires null or at most 4096 characters")
                encode(item)  # Validate UTF-8 without stripping legal NUL/whitespace.
            else:
                identifier(item, "filter value")
        normalized[key] = sorted(set(items), key=lambda item: (item is not None, item or ""))
    fields = value.get("fields", list(DEFAULT_FIELDS))
    if not isinstance(fields, list) or not 1 <= len(fields) <= len(FIELDS) or any(
            not isinstance(field, str) or field not in FIELDS for field in fields):
        raise ValueError("Unsupported query fields")
    if len(fields) != len(set(fields)):
        raise ValueError("Query fields must be unique")
    revisions = value.get("revisions", "latest")
    if revisions not in ("latest", "all"):
        raise ValueError("revisions must be latest or all")
    order = value.get("order", DEFAULT_ORDER)
    if order not in ORDERS:
        raise ValueError("Unsupported trace query order")
    limit = value.get("limit", 50)
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    cursor = value.get("cursor")
    if cursor is not None and (not isinstance(cursor, str) or not 1 <= len(cursor) <= 4096):
        raise ValueError("Invalid query cursor")
    return {"filters": normalized, "fields": fields, "revisions": revisions,
            "limit": limit, "cursor": cursor, "order": order}


def predicate(project_id, request, *, postgres=False):
    params = {"project": project_id, "projector": PROJECTOR_VERSION}
    conditions = ["r.project_id=:project"]
    if request["revisions"] == "latest":
        conditions.append("r.revision=h.latest_revision")
    for key, items in sorted(request["filters"].items()):
        names, prefixes = [], []
        for ordinal, item in enumerate(items):
            if item is None:
                continue
            name = f"filter_{key}_{ordinal}"
            params[name] = encode(item) if key in METADATA_FIELDS else item
            names.append(":" + name)
            if key in INDEXED_FIELDS:
                params[name + "_prefix"] = params[name][:PREFIX_HEX_LENGTH]
                prefixes.append(":" + name + "_prefix")
        column = field_sql(key, postgres)
        matches = [column + " IN (" + ",".join(names) + ")"] if names else []
        if None in items:
            matches.append(column + " IS NULL")
        conditions.append("(" + " OR ".join(matches) + ")")
        if prefixes:
            prefix_match = "substr(" + column + ",1," + str(PREFIX_HEX_LENGTH) + ") IN (" + ",".join(prefixes) + ")"
            if None in items:
                prefix_match += " OR " + column + " IS NULL"
            conditions.append("(" + prefix_match + ")")
    return " AND ".join(conditions), params


def lookup_health(project_id, request, *, postgres=False):
    """Metadata is part of the trace identity row, so no side lookup can be absent."""
    return "SELECT 0 AS missing_lookup", {"project": project_id, "projector": PROJECTOR_VERSION}


def binding(project_id, request):
    return canonical({"version": VERSION, "project_id": project_id, "projector": PROJECTOR_VERSION,
                      **{key: request[key] for key in ("filters", "fields", "revisions")},
                      **({"order": request["order"]} if request["order"] != DEFAULT_ORDER else {})})[1]


def decode_cursor(cursor, expected, order=DEFAULT_ORDER):
    try:
        raw = base64.b64decode(cursor, altchars=b"-_", validate=True)
        value = json.loads(raw)
        fields = {"v", "binding", "run_id", "revision"}
        if order == "created_at_desc":
            fields.add("created_at")
        if not isinstance(value, dict) or set(value) != fields:
            raise ValueError()
        if value["v"] != VERSION or value["binding"] != expected:
            raise ValueError()
        identifier(value["run_id"], "cursor run_id")
        if type(value["revision"]) is not int or not 1 <= value["revision"] <= MAX_REVISION:
            raise ValueError()
        if order == "created_at_desc" and (not isinstance(value["created_at"], str)
                                            or not value["created_at"] or len(value["created_at"]) > 64):
            raise ValueError()
        return value
    except (binascii.Error, UnicodeError, ValueError, TypeError):
        raise ValueError("Invalid query cursor or query changed") from None


class TraceQuery:
    def __init__(self, repository):
        self.repository = repository

    @staticmethod
    def capabilities():
        return {"version": VERSION, "fields": list(FIELDS), "filter_fields": list(FILTERS),
                "filter_operator": "any_of", "max_filter_values": 50, "max_page_size": 100,
                "revision_modes": ["latest", "all"], "order": DEFAULT_ORDER, "orders": list(ORDERS),
                "consistency": "live_keyset", "projector_version": PROJECTOR_VERSION,
                "max_item_bytes": MAX_ITEM_BYTES,
                "source_content": False, "triggers_analysis": False}

    def query(self, project_id, value):
        identifier(project_id, "project_id")
        request = options(value)
        query_binding = binding(project_id, request)
        where, params = predicate(project_id, request, postgres=self.repository.postgres)
        # Explicit binary collation gives Python/Rust and PostgreSQL/SQLite the
        # same cursor ordering even for opaque Unicode source identities.
        run_order = 'r.run_id COLLATE "C"' if self.repository.postgres else 'r.run_id COLLATE BINARY'
        newest = request["order"] == "created_at_desc"
        if request["cursor"] is not None:
            after = decode_cursor(request["cursor"], query_binding, request["order"])
            params.update(after_run=after["run_id"], after_revision=after["revision"])
            if newest:
                params["after_created"] = after["created_at"]
                where += (f" AND (r.created_at<:after_created OR "
                          f"(r.created_at=:after_created AND ({run_order}>:after_run OR "
                          "(r.run_id=:after_run AND r.revision>:after_revision))))")
            elif request["revisions"] == "latest":
                where += f" AND {run_order}>:after_run"
            else:
                where += f" AND ({run_order}>:after_run OR (r.run_id=:after_run AND r.revision>:after_revision))"
        params["limit"] = request["limit"] + 1
        fields = list(dict.fromkeys([*request["fields"], "run_id", "revision",
                                     *(["created_at"] if newest else [])]))
        select = ",".join(field_sql(key, self.repository.postgres) + " AS " + key for key in fields)
        page_order = (f"r.created_at DESC,{run_order},r.revision ASC" if newest
                      else f"{run_order},r.revision ASC")
        sql = "SELECT " + select + BASE_FROM + " WHERE " + where + \
            f" ORDER BY {page_order} LIMIT :limit"
        health_sql, health_params = lookup_health(project_id, request, postgres=self.repository.postgres)
        params.update(health_params)
        # An unconditional health row survives even when metadata filters match
        # nothing. Guard and page share one statement snapshot and never repair.
        collation = '"C"' if self.repository.postgres else "BINARY"
        final_order = ("created_at DESC," if newest else "") + \
            "run_id COLLATE " + collation + ",revision ASC"
        sql = "WITH lookup_health AS (" + health_sql + "),page AS (" + sql + ") SELECT * FROM (" + \
            "SELECT 1 AS _health,missing_lookup AS _missing," + ",".join("NULL AS " + key for key in fields) + \
            " FROM lookup_health UNION ALL SELECT 0 AS _health,0 AS _missing," + ",".join(fields) + \
            " FROM page) checked ORDER BY _health DESC," + final_order
        items, last, has_more, size = [], None, False, 0
        # Stream large metadata pages so the response bound is also a memory
        # bound across rows. A single trace still has its ingestion size bound.
        with self.repository.engine.connect().execution_options(stream_results=True, yield_per=1) as db:
            with db.execute(text(sql), params) as result:
                for row in result.mappings():
                    if row["_health"]:
                        if row["_missing"]:
                            raise LookupUnavailable()
                        continue
                    if len(items) == request["limit"]:
                        has_more = True
                        break
                    item = {key: decode(row[key]) if key in METADATA_FIELDS else row[key] for key in request["fields"]}
                    item_size = len(canonical(item)[0].encode())
                    if size + item_size > MAX_ITEM_BYTES:
                        if not items:
                            raise ValueError("Selected metadata exceeds page byte limit; request fewer fields")
                        has_more = True
                        break
                    items.append(item)
                    size += item_size
                    last = row
        next_cursor = None
        if has_more:
            raw = canonical({"v": VERSION, "binding": query_binding,
                             "run_id": last["run_id"], "revision": last["revision"],
                             **({"created_at": last["created_at"]} if newest else {})})[0].encode()
            next_cursor = base64.urlsafe_b64encode(raw).decode()
        return {"items": items, "next_cursor": next_cursor, "query_digest": query_binding,
                "projector_version": PROJECTOR_VERSION, "consistency": "live_keyset"}
