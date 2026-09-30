"""Read-only coverage and record aggregates from one database statement.

Complete projections contribute recorded facts, not a claim of complete capture.
Record kinds overlap the status-based ``unknown`` count. Error rates describe
all indexed records with status ok/error, not deduplicated tool executions.
The snapshot lasts one statement; latest_indexed_at is an observed timestamp,
never a continuous ingestion watermark or a promise about a later request.
"""

from sqlalchemy import text

from ..catalog import canonical
from ..traces.index import COUNT_COLUMNS, PROJECTOR_VERSION
from .service import BASE_FROM, METADATA_FIELDS, field_sql, identifier, lookup_health, options, predicate
from ..traces.lookup import LookupUnavailable, decode

VERSION = "trace-aggregates/1"
GROUP_FIELDS = ("query_id", "env_id", "harness", "model", "status", "index_state")
STATUSES = ("ok", "error", "unknown", "other")


def aggregate_options(value):
    if not isinstance(value, dict) or set(value) - {"filters", "revisions", "group_by", "limit"}:
        raise ValueError("Unsupported aggregate options")
    group_by = value.get("group_by")
    if group_by is not None and (not isinstance(group_by, str) or group_by not in GROUP_FIELDS):
        raise ValueError("Unsupported aggregate group_by")
    limit = value.get("limit", 50)
    if type(limit) is not int or not 1 <= limit <= 50:
        raise ValueError("limit must be between 1 and 50")
    shared = options({key: value[key] for key in ("filters", "revisions") if key in value})
    return {"filters": shared["filters"], "revisions": shared["revisions"],
            "group_by": group_by, "limit": limit}


def _summary_sql():
    sums = ["COUNT(*) AS matched_revisions"]
    for state, key in (("complete", "index_complete"), ("failed", "index_failed"), ("unindexed", "unindexed")):
        sums.append(f"COALESCE(SUM(CASE WHEN index_state='{state}' THEN 1 ELSE 0 END),0) AS {key}")
    for column in (*COUNT_COLUMNS.values(), *("status_" + status for status in STATUSES)):
        sums.append(f"SUM(CASE WHEN index_state='complete' THEN {column} END) AS {column}")
    sums.append("MAX(CASE WHEN index_state='complete' THEN indexed_at END) AS latest_indexed_at")
    return ",".join(sums)


def _statement(group_by, where, postgres, health_sql):
    group = field_sql(group_by, postgres) if group_by else "CAST(NULL AS TEXT)"
    columns = ",".join("r." + column for column in COUNT_COLUMNS.values())
    status_sums = ",".join(
        f"SUM(CASE WHEN t.status {condition} THEN 1 ELSE 0 END) AS status_{status}"
        for status, condition in (("ok", "='ok'"), ("error", "='error'"), ("unknown", "='unknown'"),
                                  ("other", "NOT IN ('ok','error','unknown')")))
    status_columns = ",".join(f"COALESCE(s.status_{status},0) AS status_{status}" for status in STATUSES)
    summary = _summary_sql()
    collation = '"C"' if postgres else 'BINARY'
    # Records are reduced to one row per revision before joining the revision
    # facts. This keeps both revision counts and stored kind totals unduplicated.
    return f"""
        WITH lookup_health AS ({health_sql}), matched AS (
            SELECT r.project_id,r.run_id,r.revision,{group} AS group_value,
                CASE WHEN r.projector_version=:projector THEN r.projection_state ELSE 'unindexed' END AS index_state,
                {columns},CASE WHEN r.projector_version=:projector THEN r.indexed_at END AS indexed_at
            {BASE_FROM} WHERE {where}
        ), status_counts AS (
            SELECT t.project_id,t.run_id,t.revision,{status_sums}
            FROM trace_objects t JOIN matched m
                ON m.project_id=t.project_id AND m.run_id=t.run_id AND m.revision=t.revision
                AND m.index_state='complete'
            WHERE t.projector_version=:projector AND t.object_kind='span'
            GROUP BY t.project_id,t.run_id,t.revision
        ), projected AS (
            SELECT m.*,{status_columns} FROM matched m LEFT JOIN status_counts s
                ON s.project_id=m.project_id AND s.run_id=m.run_id AND s.revision=m.revision
        ), summaries AS (
            SELECT 0 AS is_group,CAST(NULL AS TEXT) AS group_value,{summary} FROM projected
            UNION ALL
            SELECT 1 AS is_group,group_value,{summary} FROM projected
                WHERE :grouped=1 GROUP BY group_value
        ), ranked_groups AS (
            SELECT *,ROW_NUMBER() OVER (ORDER BY matched_revisions DESC,
                CASE WHEN group_value IS NULL THEN 1 ELSE 0 END,group_value COLLATE {collation}) AS group_rank,
                COUNT(*) OVER () AS total_groups FROM summaries WHERE is_group=1
        )
        SELECT *,0 AS group_rank,(SELECT COUNT(*) FROM summaries WHERE is_group=1) AS total_groups,
            (SELECT missing_lookup FROM lookup_health) AS missing_lookup
            FROM summaries WHERE is_group=0
        UNION ALL
        SELECT *,(SELECT missing_lookup FROM lookup_health) AS missing_lookup FROM ranked_groups WHERE group_rank<=:limit
        ORDER BY is_group,group_rank
    """


def _metrics(row):
    matched = int(row["matched_revisions"])
    indexed = int(row["index_complete"])

    def count(column):
        # Empty selections have known zero records; nonempty selections with no
        # complete projection have unknown records, not an invented zero.
        return 0 if matched == 0 else (None if row[column] is None else int(row[column]))

    statuses = {status: count("status_" + status) for status in STATUSES}
    denominator = None if statuses["ok"] is None else statuses["ok"] + statuses["error"]
    return {
        "matched_revisions": matched, "index_complete": indexed,
        "index_failed": int(row["index_failed"]), "unindexed": int(row["unindexed"]),
        "indexed_revision_count": indexed,
        "counts": {key: count(column) for key, column in COUNT_COLUMNS.items()},
        "record_status": statuses,
        "error_rate": {"basis": "indexed_records_with_known_outcome",
                       "numerator": statuses["error"], "denominator": denominator,
                       "value": statuses["error"] / denominator if denominator else None},
        "latest_indexed_at": row["latest_indexed_at"],
    }


class TraceAggregates:
    def __init__(self, repository):
        self.repository = repository

    def summarize(self, project_id, value):
        identifier(project_id, "project_id")
        request = aggregate_options(value)
        where, params = predicate(project_id, request, postgres=self.repository.postgres)
        health_sql, health_params = lookup_health(project_id, request, postgres=self.repository.postgres)
        params.update(health_params)
        params.update(limit=request["limit"], grouped=int(request["group_by"] is not None))
        sql = _statement(request["group_by"], where, self.repository.postgres, health_sql)
        with self.repository.engine.connect() as db:
            rows = db.execute(text(sql), params).mappings().all()
        if rows[0]["missing_lookup"]:
            raise LookupUnavailable()
        totals = _metrics(rows[0])
        groups = [{"value": decode(row["group_value"]) if request["group_by"] in METADATA_FIELDS else row["group_value"],
                   "metrics": _metrics(row)} for row in rows[1:]]
        group_count = int(rows[0]["total_groups"])
        digest = canonical({"version": VERSION, "project_id": project_id, "projector": PROJECTOR_VERSION,
                            **{key: request[key] for key in ("filters", "revisions", "group_by")}})[1]
        return {
            "version": VERSION, "projector_version": PROJECTOR_VERSION,
            "consistency": "statement_snapshot", "query_digest": digest,
            "group_by": request["group_by"], "totals": totals, "groups": groups,
            "group_count": group_count, "remaining_group_count": group_count - len(groups),
            "truncated": group_count > len(groups),
            "watermark": {"kind": "coverage", "matched_revisions": totals["matched_revisions"],
                          "indexed_revision_count": totals["indexed_revision_count"],
                          "latest_indexed_at": totals["latest_indexed_at"]},
        }
