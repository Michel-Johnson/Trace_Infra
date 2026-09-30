"""Shared visibility scope for every composite object query."""

import json

from ..catalog import canonical
from ..traces.index import PROJECTOR_VERSION
from ..traces.service import MAX_REVISION, identifier


def options(value):
    if not isinstance(value, dict) or set(value) - {"mode", "visible_to"}:
        raise ValueError("Unsupported query scope")
    mode = value.get("mode")
    if mode not in ("analysis", "model_context"):
        raise ValueError("scope.mode must be analysis or model_context")
    visible = value.get("visible_to")
    if mode == "model_context":
        if not isinstance(visible, dict) or set(visible) != {"run_id", "revision", "model_span_id"}:
            raise ValueError("model_context requires exact visible_to")
        identifier(visible["run_id"], "visible_to.run_id")
        identifier(visible["model_span_id"], "visible_to.model_span_id")
        if type(visible["revision"]) is not int or not 1 <= visible["revision"] <= MAX_REVISION:
            raise ValueError("Invalid visible_to.revision")
    elif visible is not None:
        raise ValueError("visible_to is only valid for model_context")
    return {"mode": mode, "visible_to": visible}


class QueryScope:
    """Resolve a scope without reading or reconstructing source history.

    A model-context scope is deliberately strict: only the captured Context and
    its linked Messages are queryable. Span timing or nearby history is never
    used to infer what the model saw.
    """

    def __init__(self, repository):
        self.repository = repository

    def resolve(self, project_id, value):
        identifier(project_id, "project_id")
        request = options(value)
        digest = canonical({"version": "query-scope/1", "project_id": project_id,
                            "projector_version": PROJECTOR_VERSION, **request})[1]
        if request["mode"] == "analysis":
            return {"mode": "analysis", "status": "pass", "visible_to": None,
                    "context_id": None, "issues": [], "scope_digest": digest,
                    "allowed_objects": None}
        visible = request["visible_to"]
        rows = self.repository.rows("""
            SELECT t.context_id,t.visibility_status,t.visibility_issues
            FROM trace_objects t JOIN trace_revisions i
              ON i.project_id=t.project_id AND i.run_id=t.run_id AND i.revision=t.revision
             AND i.projector_version=t.projector_version
            WHERE t.project_id=:project AND t.run_id=:run AND t.revision=:revision
              AND t.projector_version=:projector AND t.span_id=:span
              AND t.object_kind='span' AND t.kind IN ('model','model_batch')
              AND i.projection_state='complete'
        """, {"project": project_id, "run": visible["run_id"],
              "revision": visible["revision"], "projector": PROJECTOR_VERSION,
              "span": visible["model_span_id"]})
        if not rows:
            raise KeyError("Visible model span projection not found")
        row = rows[0]
        status = row["visibility_status"] or "unknown"
        issues = json.loads(row["visibility_issues"] or "[]")
        allowed = set()
        if status == "pass" and row["context_id"]:
            allowed.add((visible["run_id"], visible["revision"], "context", row["context_id"]))
            messages = self.repository.rows("""
                SELECT e.target_id FROM trace_edges e
                WHERE e.project_id=:project AND e.run_id=:run AND e.revision=:revision
                  AND e.projector_version=:projector AND e.relation='context_message'
                  AND e.source_kind='context' AND e.source_id=:context
                ORDER BY e.position,e.edge_ordinal
            """, {"project": project_id, "run": visible["run_id"],
                  "revision": visible["revision"], "projector": PROJECTOR_VERSION,
                  "context": row["context_id"]})
            allowed.update((visible["run_id"], visible["revision"], "message", item["target_id"])
                           for item in messages)
        return {"mode": "model_context", "status": status, "visible_to": visible,
                "context_id": row["context_id"], "issues": issues,
                "scope_digest": digest, "allowed_objects": allowed}

    @staticmethod
    def permits(resolved, row):
        allowed = resolved["allowed_objects"]
        return allowed is None or (row["run_id"], row["revision"],
                                   row["object_kind"], row["object_id"]) in allowed

    @staticmethod
    def report(resolved):
        if resolved["mode"] == "analysis":
            return {"mode": "analysis", "status": "pass", "issues": [],
                    "scope_digest": resolved["scope_digest"]}
        return {"mode": "model_context", "status": resolved["status"],
                "context_id": resolved["context_id"], "issues": resolved["issues"],
                "scope_digest": resolved["scope_digest"], **resolved["visible_to"]}
