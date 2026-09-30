"""Bounded body search over rebuildable documents with explicit visibility scope."""
import base64
import binascii
import json
import re
import time
from bisect import bisect_right
from collections import OrderedDict
from itertools import islice
from threading import RLock

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from ..catalog import canonical
from ..traces.index import PROJECTOR_VERSION
from ..traces.service import MAX_REVISION, identifier
from .hot_terms import HotSearchTerms, MAX_TERM_LENGTH, MIN_TERM_LENGTH
from .scope import QueryScope

VERSION = "trace-search/2"
MAX_CANDIDATES = 5000
MAX_SNIPPET_CHARS = 512
MAX_MATCH_RANGES = 20
MAX_CACHED_MATCHES = 50000
MAX_CACHED_KEYS = 100000
CACHED_MATCH_TTL_SECONDS = 300
SNIPPET_CONTEXT_CHARS = 160
KINDS = ("span", "message", "context", "tool_call")
FIELDS = ("name", "input", "output", "content", "request", "arguments")
STRUCTURED_FILTERS = ("skill_name", "skill_action", "name", "operation", "status")
BASE = """
 FROM trace_search_documents d
 JOIN trace_revisions i ON i.project_id=d.project_id AND i.run_id=d.run_id
  AND i.revision=d.revision AND i.projector_version=d.projector_version
 JOIN traces h ON h.project_id=d.project_id AND h.run_id=d.run_id
"""
HOT_BASE = """
 FROM trace_search_hot_postings d
 JOIN trace_revisions i ON i.project_id=d.project_id AND i.run_id=d.run_id
  AND i.revision=d.revision AND i.projector_version=d.projector_version
 JOIN traces h ON h.project_id=d.project_id AND h.run_id=d.run_id
"""


def _check_regex_policy(pattern):
    if (len(pattern) > 256 or re.search(r"\(\?(?!:)", pattern) or
            re.search(r"\\[1-9]", pattern) or re.search(r"\)[*+{]", pattern)):
        raise ValueError("Regex uses unsupported or unbounded constructs")


def _rewrite_boundary_escapes(pattern, aliases):
    """Rewrite only unescaped tokens outside bracket expressions."""
    result = []
    in_class = False
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if char == "\\" and index + 1 < len(pattern):
            escaped = pattern[index + 1]
            result.append(aliases.get(escaped, "\\" + escaped) if not in_class else "\\" + escaped)
            index += 2
            continue
        if char == "[" and not in_class:
            in_class = True
        elif char == "]" and in_class:
            in_class = False
        result.append(char)
        index += 1
    return "".join(result)


def _portable_pattern(pattern):
    return _rewrite_boundary_escapes(pattern, {"b": r"\y", "B": r"\Y"})


def _sqlite_pattern(pattern):
    # Test-only fallback; PostgreSQL remains the authoritative regex engine.
    return _rewrite_boundary_escapes(pattern, {
        "y": r"\b", "Y": r"\B", "m": r"(?<!\w)(?=\w)", "M": r"(?<=\w)(?!\w)",
        "b": r"\x08", "B": r"\\",
    })


def _safe_regex(pattern):
    _check_regex_policy(pattern)
    try:
        return re.compile(pattern)
    except re.error:
        raise ValueError("Invalid regex") from None


def validate_postgres_regex(repository, pattern):
    try:
        with repository.engine.begin() as db:
            db.execute(text("SET LOCAL statement_timeout = '1000ms'"))
            db.execute(text("SELECT '' ~ :pattern"), {"pattern": pattern})
    except DBAPIError as error:
        if getattr(error.orig, "sqlstate", None) == "2201B":
            raise ValueError("Invalid PostgreSQL ARE regular expression") from None
        raise


def _strings(value, name, allowed=None):
    if value is None:
        return []
    if not isinstance(value, list) or not 1 <= len(value) <= 50 or any(
            not isinstance(item, str) or not item or len(item) > 256 or "\x00" in item for item in value):
        raise ValueError(name + " requires 1 to 50 non-empty strings")
    values = sorted(set(value))
    if allowed and any(item not in allowed for item in values):
        raise ValueError("Unsupported " + name)
    return values


def options(value):
    if not isinstance(value, dict) or set(value) - {
            "query", "mode", "scope", "visible_to", "filters", "revisions", "limit", "cursor",
            "purpose", "regex_syntax"}:
        raise ValueError("Unsupported trace search options")
    query = value.get("query")
    if not isinstance(query, str) or not query or len(query) > 512 or "\x00" in query:
        raise ValueError("query must contain 1 to 512 characters")
    mode = value.get("mode", "literal")
    if mode not in ("literal", "regex"):
        raise ValueError("Unsupported trace search mode")
    if value.get("purpose", "interactive") not in ("interactive", "evaluation"):
        raise ValueError("Unsupported trace search purpose")
    regex_syntax = value.get("regex_syntax", "postgresql_are")
    if regex_syntax not in ("postgresql_are", "portable") or (mode != "regex" and regex_syntax != "postgresql_are"):
        raise ValueError("regex_syntax requires regex mode and a supported syntax")
    if mode == "regex":
        _check_regex_policy(query)
        if regex_syntax == "portable" and any(token in query for token in ("[[:", "[[.", "[[=")):
            raise ValueError("Portable word-boundary aliases cannot be mixed with POSIX bracket classes")
    scope = value.get("scope")
    if scope not in ("analysis", "model_context"):
        raise ValueError("scope must be analysis or model_context")
    visible = value.get("visible_to")
    if scope == "model_context":
        if not isinstance(visible, dict) or set(visible) != {"run_id", "revision", "model_span_id"}:
            raise ValueError("model_context scope requires exact visible_to")
        identifier(visible["run_id"], "visible_to.run_id")
        identifier(visible["model_span_id"], "visible_to.model_span_id")
        if type(visible["revision"]) is not int or not 1 <= visible["revision"] <= MAX_REVISION:
            raise ValueError("Invalid visible_to.revision")
    elif visible is not None:
        raise ValueError("visible_to is only valid for model_context scope")
    filters = value.get("filters", {})
    if not isinstance(filters, dict) or set(filters) - {
            "run_id", "object_kind", "field", "span_id", *STRUCTURED_FILTERS}:
        raise ValueError("Unsupported trace search filter")
    normalized = {key: _strings(raw, key, KINDS if key == "object_kind" else (FIELDS if key == "field" else None))
                  for key, raw in filters.items() if raw != []}
    if scope == "model_context" and any(key in normalized for key in STRUCTURED_FILTERS):
        raise ValueError("Structured span metadata filters are not available in model_context scope")
    revisions = value.get("revisions", "latest")
    if revisions not in ("latest", "all") or (scope == "model_context" and revisions != "latest"):
        raise ValueError("Unsupported revision mode for search scope")
    limit = value.get("limit", 50)
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    cursor = value.get("cursor")
    if cursor is not None and (not isinstance(cursor, str) or not 1 <= len(cursor) <= 4096):
        raise ValueError("Invalid search cursor")
    return {"query": query, "mode": mode, "regex_syntax": regex_syntax, "scope": scope, "visible_to": visible,
            "filters": normalized, "revisions": revisions, "limit": limit, "cursor": cursor}


def _binding(project_id, request):
    return canonical({"version": VERSION, "project_id": project_id, "projector": PROJECTOR_VERSION,
                      **{key: request[key] for key in ("query", "mode", "regex_syntax", "scope",
                                                        "visible_to", "filters", "revisions")}})[1]


def _cursor(value, expected):
    try:
        decoded = json.loads(base64.b64decode(value, altchars=b"-_", validate=True))
        if set(decoded) != {"v", "binding", "run_id", "revision", "ordinal"} or \
                decoded["v"] != VERSION or decoded["binding"] != expected:
            raise ValueError()
        identifier(decoded["run_id"], "cursor.run_id")
        if (type(decoded["revision"]) is not int or not 1 <= decoded["revision"] <= MAX_REVISION or
                type(decoded["ordinal"]) is not int or decoded["ordinal"] < 0):
            raise ValueError()
        return decoded
    except (binascii.Error, UnicodeError, ValueError, TypeError):
        raise ValueError("Invalid search cursor or query changed") from None


def _like(value):
    return "%" + value.replace("!", "!!").replace("%", "!%").replace("_", "!_") + "%"


def _exact_ranges(query, mode, value):
    if mode == "literal":
        ranges, start = [], 0
        while len(ranges) < MAX_MATCH_RANGES:
            start = value.find(query, start)
            if start < 0:
                break
            end = start + len(query)
            ranges.append((start, end))
            start = end if end > start else start + 1
        return ranges
    if mode == "regex":
        return [(match.start(), match.end())
                for match in islice(_safe_regex(query).finditer(value), MAX_MATCH_RANGES)]
    return []


def _snippet(query, mode, value, ranges=None):
    ranges = _exact_ranges(query, mode, value) if ranges is None else ranges
    focus = ranges[0][0] if ranges else 0
    if len(value) <= MAX_SNIPPET_CHARS:
        start, snippet = 0, value
    else:
        start = max(0, focus - SNIPPET_CONTEXT_CHARS)
        start = min(start, len(value) - MAX_SNIPPET_CHARS)
        snippet = value[start:start + MAX_SNIPPET_CHARS]
    end = start + len(snippet)
    local = [{"start": left - start, "end": right - start}
             for left, right in ranges if start <= left and right <= end]
    return {"snippet": snippet, "snippet_start": start, "text_length": len(value),
            "snippet_truncated": len(snippet) < len(value), "match_ranges": local}


class TraceSearch:
    def __init__(self, repository, version_provider=None):
        self.repository = repository
        self.scopes = QueryScope(repository)
        self.hot_terms = HotSearchTerms(repository)
        self.version_provider = version_provider
        self._matches = OrderedDict()
        self._match_count = 0
        self._match_lock = RLock()

    def _cached_matches(self, key):
        with self._match_lock:
            entry = self._matches.get(key)
            if entry is None:
                return None
            if entry[0] <= time.monotonic():
                self._match_count -= len(entry[1])
                del self._matches[key]
                return None
            self._matches.move_to_end(key)
            return entry[1:]

    def _remember_matches(self, key, keys, totals):
        if isinstance(keys, str):
            keys = json.loads(keys)
        if key is None or keys is None or len(keys) > MAX_CACHED_MATCHES or len(keys) != totals["total_count"]:
            return
        keys = tuple((run_id, int(revision), int(ordinal)) for run_id, revision, ordinal in keys)
        with self._match_lock:
            old = self._matches.pop(key, None)
            if old is not None:
                self._match_count -= len(old[1])
            self._matches[key] = (time.monotonic() + CACHED_MATCH_TTL_SECONDS, keys, totals)
            self._match_count += len(keys)
            while self._match_count > MAX_CACHED_KEYS or len(self._matches) > 16:
                _, removed = self._matches.popitem(last=False)
                self._match_count -= len(removed[1])

    def _rows_for_keys(self, project_id, keys):
        if not keys:
            return []
        params = {"project": project_id, "projector": PROJECTOR_VERSION}
        values = []
        for position, (run_id, revision, ordinal) in enumerate(keys):
            params.update({f"run_{position}": run_id, f"revision_{position}": revision,
                           f"ordinal_{position}": ordinal})
            values.append(f"(:run_{position},:revision_{position},:ordinal_{position},{position})")
        sql = """
            WITH chosen(run_id,revision,document_ordinal,position) AS (VALUES """ + ",".join(values) + """)
            SELECT d.run_id,d.revision,d.document_ordinal,d.object_kind,d.object_id,d.span_id,
                   d.field,d.text_state,CAST(1.0 AS DOUBLE PRECISION) AS score,d.source_refs,d.text
            FROM chosen c JOIN trace_search_documents d
              ON d.project_id=:project AND d.projector_version=:projector
             AND d.run_id=c.run_id AND d.revision=c.revision
             AND d.document_ordinal=c.document_ordinal
            ORDER BY c.position
        """
        return self.repository.rows(sql, params)

    def _forget_matches(self, key):
        with self._match_lock:
            old = self._matches.pop(key, None)
            if old is not None:
                self._match_count -= len(old[1])

    def warm(self, project_id):
        return None

    def capabilities(self):
        return {"version": VERSION, "modes": ["literal", "regex"],
                "scopes": ["analysis", "model_context"], "object_kinds": list(KINDS),
                "fields": list(FIELDS), "max_page_size": 100,
                "max_scan_candidates": MAX_CANDIDATES, "projector_version": PROJECTOR_VERSION,
                "max_snippet_chars": MAX_SNIPPET_CHARS,
                "max_match_ranges": MAX_MATCH_RANGES,
                "structured_filters": list(STRUCTURED_FILTERS),
                "backend": "postgresql_pg_trgm" if self.repository.postgres else "sqlite_scan_fallback",
                "regex_dialect": "postgresql_are",
                "regex_syntaxes": ["postgresql_are", "portable"],
                "regex_max_chars": 256,
                "regex_examples": [
                    {"intent": "alternative", "pattern": "error|failed"},
                    {"intent": "word_boundary", "pattern": r"\ytool\y"},
                    {"intent": "portable_word_boundary", "pattern": r"\btool\b",
                     "regex_syntax": "portable"},
                ],
                "source_content": "inline_only", "triggers_analysis": False}

    def query(self, project_id, value):
        identifier(project_id, "project_id")
        request = options(value)
        effective_pattern = (request["query"] if request["regex_syntax"] == "postgresql_are"
                             else _portable_pattern(request["query"])) if request["mode"] == "regex" else None
        if effective_pattern is not None and self.repository.postgres:
            validate_postgres_regex(self.repository, effective_pattern)
        digest = _binding(project_id, request)
        resolved_scope = self.scopes.resolve(project_id, {
            "mode": request["scope"], "visible_to": request["visible_to"]})
        visibility = ({"status": resolved_scope["status"],
                       "context_id": resolved_scope["context_id"],
                       "issues": resolved_scope["issues"], **request["visible_to"]}
                      if request["visible_to"] else None)
        if request["visible_to"] and resolved_scope["status"] != "pass":
            return {"items": [], "next_cursor": None, "query_digest": digest,
                    "projector_version": PROJECTOR_VERSION, "visibility": visibility,
                    "effective_pattern": effective_pattern,
                    "candidate_count": 0, "total_count": 0, "matched_trace_count": 0,
                    "truncated": False}
        params = {"project": project_id, "projector": PROJECTOR_VERSION}
        conditions = ["d.project_id=:project", "d.projector_version=:projector", "i.projection_state='complete'"]
        if request["scope"] == "analysis" and request["revisions"] == "latest":
            conditions.append("d.revision=h.latest_revision")
        for key, column in (("run_id", "d.run_id"), ("object_kind", "d.object_kind"),
                            ("field", "d.field"), ("span_id", "d.span_id")):
            if key in request["filters"]:
                names = []
                for index, item in enumerate(request["filters"][key]):
                    name = f"filter_{key}_{index}"; params[name] = item; names.append(":" + name)
                conditions.append(column + " IN (" + ",".join(names) + ")")
        structured = []
        for key in STRUCTURED_FILTERS:
            if key not in request["filters"]:
                continue
            names = []
            for index, item in enumerate(request["filters"][key]):
                name = f"filter_{key}_{index}"; params[name] = item; names.append(":" + name)
            structured.append("o." + key + " IN (" + ",".join(names) + ")")
        if structured:
            conditions.append("EXISTS (SELECT 1 FROM trace_objects o WHERE "
                "o.project_id=d.project_id AND o.run_id=d.run_id AND o.revision=d.revision "
                "AND o.projector_version=d.projector_version AND o.object_kind='span' "
                "AND o.span_id=d.span_id AND " + " AND ".join(structured) + ")")
        if visibility:
            params.update(visible_run=request["visible_to"]["run_id"],
                          visible_revision=request["visible_to"]["revision"],
                          visible_context=resolved_scope["context_id"])
            conditions.extend(["d.run_id=:visible_run", "d.revision=:visible_revision",
                "((d.object_kind='context' AND d.object_id=:visible_context) OR "
                "(d.object_kind='message' AND EXISTS (SELECT 1 FROM trace_edges e "
                "WHERE e.project_id=d.project_id AND e.run_id=d.run_id AND e.revision=d.revision "
                "AND e.projector_version=d.projector_version AND e.relation='context_message' "
                "AND e.source_id=:visible_context AND e.target_id=d.object_id)))"])
        run_order = 'd.run_id COLLATE "C"' if self.repository.postgres else "d.run_id COLLATE BINARY"
        after = _cursor(request["cursor"], digest) if request["cursor"] else None
        expression = None
        score_sql = "CAST(1.0 AS DOUBLE PRECISION)"
        if self.repository.postgres:
            params["query"] = effective_pattern if effective_pattern is not None else request["query"]
            hot = (request["mode"] == "literal" and request["scope"] == "analysis" and
                   MIN_TERM_LENGTH <= len(request["query"]) <= MAX_TERM_LENGTH and
                   self.hot_terms.active(project_id, request["query"]))
            base = HOT_BASE if hot else BASE
            if request["mode"] == "literal":
                params["needle"] = _like(request["query"])
                match_condition = "d.term=:query" if hot else "d.text LIKE :needle ESCAPE '!'"
            else:
                match_condition = "d.text ~ :query"
            conditions.append(match_condition)
            count_conditions = list(conditions)
            if after:
                params.update(after_run=after["run_id"], after_revision=after["revision"],
                              after_ordinal=after["ordinal"])
            params["limit"] = request["limit"] + 1
            cache_key = ((digest, self.version_provider(project_id)) if
                         not hot and self.version_provider is not None and request["scope"] == "analysis" and
                         request["mode"] == "literal" and len(request["query"]) >= 3 else None)
            cached = self._cached_matches(cache_key) if cache_key is not None else None
            if cached is not None:
                keys, totals = cached
                start = bisect_right(keys, (after["run_id"], after["revision"], after["ordinal"])) if after else 0
                page_keys = keys[start:start + params["limit"]]
                rows = self._rows_for_keys(project_id, page_keys)
                if len(rows) != len(page_keys):
                    self._forget_matches(cache_key)
                    cached = None
            if cached is None:
                rows, totals, keys = self._postgres_rows(
                    count_conditions, params, score_sql, after is not None, cache_key is not None, base,
                    request["mode"] == "regex")
                self._remember_matches(cache_key, keys, totals)
            candidate_count = len(rows)
        else:
            try:
                expression = re.compile(_sqlite_pattern(effective_pattern)) if effective_pattern is not None else None
            except re.error:
                raise ValueError("SQLite fallback cannot evaluate this PostgreSQL ARE pattern") from None
            params["limit"] = MAX_CANDIDATES + 1
            sql = self._select(conditions, run_order, "1.0")
            rows = self.repository.rows(sql, params)
            if len(rows) > MAX_CANDIDATES:
                raise ValueError("Search candidate limit exceeded; narrow the structured filters")
            candidate_count = len(rows)
            filtered = []
            for row in rows:
                if request["mode"] == "literal":
                    matched, score = request["query"] in row["text"], 1.0
                else:
                    matched, score = expression.search(row["text"]) is not None, 1.0
                if matched:
                    regex_ranges = ([(match.start(), match.end())
                                     for match in islice(expression.finditer(row["text"]), MAX_MATCH_RANGES)]
                                    if expression is not None else None)
                    filtered.append({**row, "score": score, "regex_ranges": regex_ranges})
            totals = {"total_count": len(rows),
                      "matched_trace_count": len({row["run_id"] for row in filtered})}
            totals["total_count"] = len(filtered)
            if after:
                key = (after["run_id"], after["revision"], after["ordinal"])
                filtered = [row for row in filtered if
                            (row["run_id"], row["revision"], row["document_ordinal"]) > key]
            rows = filtered[:request["limit"] + 1]
            candidate_count = len(rows)
        has_more = len(rows) > request["limit"]
        rows = rows[:request["limit"]]
        items = []
        for row in rows:
            ranges = row.get("regex_ranges") if effective_pattern is not None else None
            if isinstance(ranges, str):
                ranges = json.loads(ranges)
            items.append({**{key: row[key] for key in ("run_id", "revision", "object_kind", "object_id",
                                                       "span_id", "field", "text_state", "source_refs")},
                          "score": float(row["score"]),
                          **_snippet(request["query"], request["mode"], row["text"], ranges)})
        for item in items:
            item["source_refs"] = json.loads(item["source_refs"])
        next_cursor = None
        if has_more and rows:
            last = rows[-1]
            raw = canonical({"v": VERSION, "binding": digest, "run_id": last["run_id"],
                             "revision": last["revision"], "ordinal": last["document_ordinal"]})[0].encode()
            next_cursor = base64.urlsafe_b64encode(raw).decode()
        return {"items": items, "next_cursor": next_cursor, "query_digest": digest,
                "projector_version": PROJECTOR_VERSION, "visibility": visibility,
                "effective_pattern": effective_pattern,
                "candidate_count": candidate_count,
                "total_count": int(totals["total_count"]),
                "matched_trace_count": int(totals["matched_trace_count"]),
                "truncated": has_more}

    @staticmethod
    def _select(conditions, run_order, score_sql):
        return ("SELECT d.run_id,d.revision,d.document_ordinal,d.object_kind,d.object_id,d.span_id,d.field,d.text_state,"
                + score_sql + " AS score,d.source_refs,d.text" + BASE + " WHERE " +
                " AND ".join(conditions) + f" ORDER BY {run_order},d.revision,d.document_ordinal LIMIT :limit")

    def _postgres_rows(self, count_conditions, params, score_sql, has_cursor,
                       capture_keys=False, base=BASE, regex=False):
        if capture_keys:
            params["cache_limit"] = MAX_CACHED_MATCHES + 1
        page_filter = ("WHERE (m.run_id COLLATE \"C\">:after_run OR "
                       "(m.run_id=:after_run AND (m.revision>:after_revision OR "
                       "(m.revision=:after_revision AND m.document_ordinal>:after_ordinal)))) "
                       if has_cursor else "")
        keys_cte = (""", cache_keys AS (
                SELECT json_agg(json_build_array(run_id,revision,document_ordinal)
                                ORDER BY run_id COLLATE "C",revision,document_ordinal) AS keys
                FROM (SELECT run_id,revision,document_ordinal FROM matched
                      ORDER BY run_id COLLATE "C",revision,document_ordinal
                      LIMIT :cache_limit) bounded
            )""" if capture_keys else "")
        keys_select = ",cache_keys.keys AS cache_keys" if capture_keys else ",NULL::json AS cache_keys"
        keys_join = " CROSS JOIN cache_keys" if capture_keys else ""
        regex_ranges_sql = ("""(SELECT json_agg(json_build_array(bounds.start_at-1,bounds.end_at-1)
                                   ORDER BY n.occurrence)
                    FROM generate_series(1,:max_match_ranges) AS n(occurrence)
                    CROSS JOIN LATERAL (
                        SELECT regexp_instr(d.text,:query,1,n.occurrence,0) AS start_at,
                               regexp_instr(d.text,:query,1,n.occurrence,1) AS end_at
                    ) bounds WHERE bounds.start_at>0)""" if regex else "NULL::json")
        if regex:
            params["max_match_ranges"] = MAX_MATCH_RANGES
        sql = """
            WITH matched AS MATERIALIZED (
                SELECT d.project_id,d.run_id,d.revision,d.projector_version,d.document_ordinal
        """ + base + " WHERE " + " AND ".join(count_conditions) + """
            ), totals AS (
                SELECT count(*) AS total_count,count(DISTINCT run_id) AS matched_trace_count
                FROM matched
            )""" + keys_cte + """, page AS (
                SELECT m.project_id,m.run_id,m.revision,m.projector_version,m.document_ordinal
                FROM matched m
        """ + page_filter + """
                ORDER BY m.run_id COLLATE "C",m.revision,m.document_ordinal
                LIMIT :limit
            ), combined AS (
            SELECT 1 AS _summary,t.total_count,t.matched_trace_count""" + keys_select + """,
                   NULL::text AS run_id,NULL::integer AS revision,NULL::integer AS document_ordinal,
                   NULL::text AS object_kind,NULL::text AS object_id,NULL::text AS span_id,
                   NULL::text AS field,NULL::text AS text_state,NULL::double precision AS score,
                   NULL::text AS source_refs,NULL::text AS text,NULL::json AS regex_ranges
            FROM totals t""" + keys_join + """
            UNION ALL
            SELECT 0 AS _summary,t.total_count,t.matched_trace_count,NULL::json AS cache_keys,
                   d.run_id,d.revision,d.document_ordinal,d.object_kind,d.object_id,d.span_id,
                   d.field,d.text_state,""" + score_sql + """ AS score,d.source_refs,d.text,
                   """ + regex_ranges_sql + """ AS regex_ranges
            FROM page p
            JOIN trace_search_documents d
              ON d.project_id=p.project_id AND d.run_id=p.run_id AND d.revision=p.revision
             AND d.projector_version=p.projector_version AND d.document_ordinal=p.document_ordinal
            CROSS JOIN totals t
            )
            SELECT * FROM combined
            ORDER BY _summary DESC,run_id COLLATE "C" NULLS FIRST,revision,document_ordinal
        """
        with self.repository.engine.begin() as db:
            db.execute(text("SET LOCAL statement_timeout = '15000ms'"))
            result = list(db.execute(text(sql), params).mappings())
        summary = result[0]
        totals = {"total_count": summary["total_count"],
                  "matched_trace_count": summary["matched_trace_count"]}
        rows = [{key: value for key, value in row.items()
                 if key not in ("_summary", "total_count", "matched_trace_count", "cache_keys")}
                for row in result[1:]]
        return rows, totals, summary["cache_keys"]


class VisibilityReports:
    def __init__(self, repository):
        self.repository = repository

    def get(self, project_id, run_id, revision):
        identifier(project_id, "project_id"); identifier(run_id, "run_id")
        if type(revision) is not int or not 1 <= revision <= MAX_REVISION:
            raise ValueError("Invalid revision")
        rows = self.repository.rows("""
            SELECT t.span_id,t.context_id,t.visibility_status,t.visibility_issues
            FROM trace_objects t JOIN trace_revisions i
              ON i.project_id=t.project_id AND i.run_id=t.run_id AND i.revision=t.revision
             AND i.projector_version=t.projector_version
            WHERE t.project_id=:project AND t.run_id=:run AND t.revision=:revision
              AND t.projector_version=:projector AND t.object_kind='span'
              AND t.kind IN ('model','model_batch') AND i.projection_state='complete'
            ORDER BY t.source_ordinal
        """, {"project": project_id, "run": run_id, "revision": revision, "projector": PROJECTOR_VERSION})
        if not rows:
            raise KeyError("Visibility projection not found")
        items = [{"model_span_id": row["span_id"], "context_id": row["context_id"],
                  "status": row["visibility_status"] or "unknown",
                  "issues": json.loads(row["visibility_issues"] or "[]")} for row in rows]
        status = "fail" if any(item["status"] == "fail" for item in items) else (
            "unknown" if any(item["status"] == "unknown" for item in items) else "pass")
        return {"run_id": run_id, "revision": revision, "projector_version": PROJECTOR_VERSION,
                "status": status, "items": items}
