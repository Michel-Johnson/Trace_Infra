"""Deterministic duration outliers over structured Span projections."""
import json
import statistics

from sqlalchemy import text

from ..catalog import canonical
from ..traces.index import PROJECTOR_VERSION
from ..traces.service import identifier
from .spans import EFFECTIVE

VERSION = "duration-anomalies/1"


def options(value):
    if not isinstance(value, dict) or set(value) - {
            "skill_name", "skill_action", "statuses", "revisions", "min_samples", "max_samples"}:
        raise ValueError("Unsupported duration analysis options")
    skill_name = value.get("skill_name")
    if not isinstance(skill_name, str) or not skill_name or len(skill_name) > 512 or "\x00" in skill_name:
        raise ValueError("skill_name is required")
    skill_action = value.get("skill_action")
    if skill_action not in (None, "load", "invoke"):
        raise ValueError("Unsupported skill action")
    statuses = value.get("statuses", ["ok", "error", "unknown"])
    if not isinstance(statuses, list) or not 1 <= len(statuses) <= 5 or any(
            item not in ("ok", "error", "running", "cancelled", "unknown") for item in statuses):
        raise ValueError("Unsupported statuses")
    revisions = value.get("revisions", "latest")
    if revisions not in ("latest", "all"):
        raise ValueError("revisions must be latest or all")
    min_samples = value.get("min_samples", 5)
    max_samples = value.get("max_samples", 5000)
    if type(min_samples) is not int or not 3 <= min_samples <= 100:
        raise ValueError("min_samples must be between 3 and 100")
    if type(max_samples) is not int or not min_samples <= max_samples <= 5000:
        raise ValueError("max_samples must be between min_samples and 5000")
    return {"skill_name": skill_name, "skill_action": skill_action,
            "statuses": sorted(set(statuses)), "revisions": revisions,
            "min_samples": min_samples, "max_samples": max_samples}


class DurationAnomalies:
    def __init__(self, repository):
        self.repository = repository

    def analyze(self, project_id, value):
        identifier(project_id, "project_id")
        request = options(value)
        params = {"project": project_id, "projector": PROJECTOR_VERSION,
                  "skill": request["skill_name"], "limit": request["max_samples"] + 1}
        statuses = []
        for index, status in enumerate(request["statuses"]):
            key = "status_" + str(index); params[key] = status; statuses.append(":" + key)
        conditions = ["t.project_id=:project", "t.projector_version=:projector", "t.object_kind='span'",
                      "i.projection_state='complete'",
                      "t.skill_name=:skill", "t.status IN (" + ",".join(statuses) + ")",
                      EFFECTIVE + " IS NOT NULL"]
        if request["skill_action"]:
            params["action"] = request["skill_action"]
            conditions.append("t.skill_action=:action")
        if request["revisions"] == "latest":
            conditions.append("t.revision=h.latest_revision")
        run_order = 't.run_id COLLATE "C"' if self.repository.postgres else "t.run_id COLLATE BINARY"
        rows = self.repository.rows("SELECT t.run_id,t.revision,t.span_id,t.skill_name,t.skill_action,t.status," +
            EFFECTIVE + " AS duration_ms,t.source_refs FROM trace_objects t " +
            "JOIN trace_revisions i ON i.project_id=t.project_id AND i.run_id=t.run_id AND i.revision=t.revision AND i.projector_version=t.projector_version " +
            "JOIN traces h ON h.project_id=t.project_id AND h.run_id=t.run_id WHERE " +
            " AND ".join(conditions) + f" ORDER BY {run_order},t.revision,t.source_ordinal LIMIT :limit", params)
        if len(rows) > request["max_samples"]:
            raise ValueError("Duration analysis sample limit exceeded; narrow the cohort")
        durations = [row["duration_ms"] for row in rows]
        digest = canonical({"version": VERSION, "project_id": project_id,
                            "projector_version": PROJECTOR_VERSION, **request})[1]
        revisions = [{"run_id": run_id, "revision": revision} for run_id, revision in sorted(
            {(row["run_id"], row["revision"]) for row in rows})]
        base = {"version": VERSION, "projector_version": PROJECTOR_VERSION,
                "query_digest": digest, "sample_count": len(rows), "resolved_revisions": revisions,
                "cohort": {key: request[key] for key in ("skill_name", "skill_action", "statuses", "revisions")}}
        if len(rows) < request["min_samples"]:
            return {**base, "status": "insufficient_data", "median_ms": None, "mad_ms": None,
                    "threshold_ms": None, "matches": []}
        median = statistics.median(durations)
        mad = statistics.median(abs(duration - median) for duration in durations)
        threshold = median + max(1, 6 * mad)
        matches = [{"run_id": row["run_id"], "revision": row["revision"], "span_id": row["span_id"],
                    "skill_name": row["skill_name"], "skill_action": row["skill_action"],
                    "status": row["status"], "duration_ms": row["duration_ms"],
                    "source_refs": json.loads(row["source_refs"])}
                   for row in rows if row["duration_ms"] > threshold]
        matches.sort(key=lambda row: (-row["duration_ms"], row["run_id"], row["revision"], row["span_id"]))
        return {**base, "status": "evaluated", "median_ms": median, "mad_ms": mad,
                "threshold_ms": threshold, "matches": matches}
