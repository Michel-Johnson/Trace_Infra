"""Immutable, project-scoped persistence for composite analysis results."""

import json
from datetime import datetime, timezone

from sqlalchemy import text

from ..catalog import canonical
from ..traces.service import identifier


class AnalysisResults:
    def __init__(self, repository):
        self.repository = repository

    def save(self, project_id, analyzer, request, result):
        identifier(project_id, "project_id")
        revisions = result["coverage"]["resolved_revisions"]
        if not revisions and request["scope"]["mode"] == "model_context":
            target = request["scope"]["visible_to"]
            revisions = [{"run_id": target["run_id"], "revision": target["revision"]}]
        if not revisions:
            raise ValueError("Cannot persist an analysis without resolved revisions")
        query = {key: value for key, value in request.items() if key != "persist"}
        response = {key: value for key, value in result.items() if key != "persisted"}
        input_digest = canonical({"project_id": project_id, "revisions": revisions,
                                  "projector_version": result["projector_version"]})[1]
        result_id = canonical({"project_id": project_id, **analyzer,
                               "query_digest": result["query_digest"],
                               "scope_digest": result["scope"]["scope_digest"],
                               "input_digest": input_digest})[1]
        payload = {"result_id": result_id, **analyzer, "query": query, "result": response,
                   "input_digest": input_digest,
                   "created_at": datetime.now(timezone.utc).isoformat()}
        anchor = revisions[0]
        with self.repository.engine.begin() as db:
            db.execute(text("""INSERT INTO analysis_results(project_id,run_id,revision,analyzer,
                analyzer_version,scope_digest,input_digest,payload,created_at)
                VALUES(:project,:run,:revision,:analyzer,:version,:result_id,:input_digest,:payload,:created_at)
                ON CONFLICT(project_id,run_id,revision,analyzer,analyzer_version,scope_digest) DO NOTHING"""),
                {"project": project_id, "run": anchor["run_id"], "revision": anchor["revision"],
                 "analyzer": analyzer["analyzer"], "version": analyzer["analyzer_version"],
                 "result_id": result_id, "input_digest": input_digest,
                 "payload": canonical(payload)[0], "created_at": payload["created_at"]})
        stored = self.get(project_id, analyzer["analyzer"], analyzer["analyzer_version"], result_id)
        if stored["input_digest"] != input_digest or stored["query"] != query:
            raise RuntimeError("Persisted analysis identity collision")
        return {"result_id": result_id, "created_at": stored["created_at"],
                "input_digest": input_digest}

    def get(self, project_id, analyzer, analyzer_version, result_id):
        identifier(project_id, "project_id"); identifier(analyzer, "analyzer")
        identifier(analyzer_version, "analyzer_version"); identifier(result_id, "result_id")
        rows = self.repository.rows("""SELECT payload FROM analysis_results
            WHERE project_id=:project AND analyzer=:analyzer AND analyzer_version=:version
              AND scope_digest=:result_id ORDER BY created_at LIMIT 1""",
            {"project": project_id, "analyzer": analyzer,
             "version": analyzer_version, "result_id": result_id})
        if not rows:
            raise KeyError("Analysis result not found")
        return json.loads(rows[0]["payload"])

    def query(self, project_id, value):
        identifier(project_id, "project_id")
        if not isinstance(value, dict) or set(value) - {"analyzer", "analyzer_version", "run_ids", "limit"}:
            raise ValueError("Unsupported analysis result query")
        analyzer = value.get("analyzer")
        version = value.get("analyzer_version")
        if analyzer is not None: identifier(analyzer, "analyzer")
        if version is not None: identifier(version, "analyzer_version")
        run_ids = value.get("run_ids", [])
        if not isinstance(run_ids, list) or len(run_ids) > 5000 or any(
                not isinstance(item, str) for item in run_ids):
            raise ValueError("run_ids supports at most 5000 identifiers")
        for run_id in run_ids: identifier(run_id, "run_id")
        limit = value.get("limit", 100)
        if type(limit) is not int or not 1 <= limit <= 5000:
            raise ValueError("limit must be between 1 and 5000")
        params = {"project": project_id, "limit": limit + 1}
        conditions = ["project_id=:project"]
        for key, item in (("analyzer", analyzer), ("analyzer_version", version)):
            if item is not None:
                params[key] = item; conditions.append(key + "=:" + key)
        if run_ids:
            names = []
            for index, run_id in enumerate(sorted(set(run_ids))):
                name = "run_" + str(index); params[name] = run_id; names.append(":" + name)
            conditions.append("run_id IN (" + ",".join(names) + ")")
        rows = self.repository.rows("SELECT payload FROM analysis_results WHERE " +
            " AND ".join(conditions) + " ORDER BY run_id,revision,analyzer,analyzer_version,created_at LIMIT :limit",
            params)
        items = [json.loads(row["payload"]) for row in rows[:limit]]
        return {"items": items, "truncated": len(rows) > limit}
