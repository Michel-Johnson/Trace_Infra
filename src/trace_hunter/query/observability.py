"""Read-only project storage and search-index health summary."""

import json

from ..traces.index import COVERAGE_FIELDS, PROJECTOR_VERSION
from ..traces.service import identifier


class ProjectObservability:
    def __init__(self, repository):
        self.repository = repository

    def summary(self, project_id):
        identifier(project_id, "project_id")
        params = {"project_id": project_id, "projector_version": PROJECTOR_VERSION}
        revisions = self.repository.rows("""
            SELECT r.projection_state,r.capture_coverage,r.record_count
            FROM traces t JOIN trace_revisions r
              ON r.project_id=t.project_id AND r.run_id=t.run_id
             AND r.revision=t.latest_revision
            WHERE t.project_id=:project_id
        """, params)
        object_counts = self.repository.rows("""
            SELECT count(*) AS objects,
              sum(CASE WHEN o.object_kind='span' THEN 1 ELSE 0 END) AS spans,
              sum(CASE WHEN o.object_kind='message' THEN 1 ELSE 0 END) AS messages,
              sum(CASE WHEN o.object_kind='context' THEN 1 ELSE 0 END) AS contexts,
              sum(CASE WHEN o.object_kind='tool_call' THEN 1 ELSE 0 END) AS tool_calls,
              sum(CASE WHEN o.object_kind='span' AND o.duration_ms IS NOT NULL THEN 1 ELSE 0 END) AS timed_spans,
              sum(CASE WHEN o.object_kind='span' AND o.status IS NOT NULL AND o.status!='unknown' THEN 1 ELSE 0 END) AS known_status_spans,
              sum(CASE WHEN o.object_kind='span' AND o.kind IN ('model','model_batch') THEN 1 ELSE 0 END) AS model_spans,
              sum(CASE WHEN o.object_kind='span' AND o.kind IN ('model','model_batch') AND o.context_id IS NOT NULL THEN 1 ELSE 0 END) AS contextual_model_spans,
              sum(CASE WHEN o.object_kind='span' AND o.kind IN ('model','model_batch')
                        AND (o.input_tokens IS NOT NULL OR o.output_tokens IS NOT NULL) THEN 1 ELSE 0 END) AS tokenized_model_spans
            FROM trace_objects o JOIN traces t
              ON t.project_id=o.project_id AND t.run_id=o.run_id AND t.latest_revision=o.revision
            WHERE o.project_id=:project_id AND o.projector_version=:projector_version
        """, params)[0]
        related = self.repository.rows("""
            SELECT
              (SELECT count(*) FROM trace_search_documents d JOIN traces t
                 ON t.project_id=d.project_id AND t.run_id=d.run_id AND t.latest_revision=d.revision
                WHERE d.project_id=:project_id AND d.projector_version=:projector_version) AS search_documents,
              (SELECT count(*) FROM trace_edges e JOIN traces t
                 ON t.project_id=e.project_id AND t.run_id=e.run_id AND t.latest_revision=e.revision
                WHERE e.project_id=:project_id AND e.projector_version=:projector_version) AS edges
        """, params)[0]
        projection = {state: 0 for state in ("complete", "unindexed", "failed")}
        coverage = {field: {state: 0 for state in ("complete", "partial", "missing", "unknown")}
                    for field in COVERAGE_FIELDS}
        for revision in revisions:
            projection[revision["projection_state"]] = projection.get(revision["projection_state"], 0) + 1
            values = json.loads(revision["capture_coverage"]) if revision["capture_coverage"] else {}
            for field in COVERAGE_FIELDS:
                state = values.get(field, "unknown")
                coverage[field][state] = coverage[field].get(state, 0) + 1
        counts = {key: int(value or 0) for key, value in object_counts.items()}
        counts.update({key: int(value or 0) for key, value in related.items()})
        counts["traces"] = len(revisions)
        counts["projected_span_records"] = sum(int(row["record_count"] or 0) for row in revisions)
        trigram_active = False
        if self.repository.postgres:
            health = self.repository.rows("""
                SELECT
                  EXISTS(SELECT 1 FROM pg_extension WHERE extname='pg_trgm') AS extension_ready,
                  EXISTS(SELECT 1 FROM pg_indexes
                         WHERE schemaname=current_schema()
                           AND indexname='trace_search_documents_trigram') AS index_ready
            """)[0]
            trigram_active = bool(health["extension_ready"] and health["index_ready"])
        return {
            "project_id": project_id,
            "projector_version": PROJECTOR_VERSION,
            "database": "postgresql" if self.repository.postgres else "sqlite",
            "search_backend": "postgresql_pg_trgm" if self.repository.postgres else "sqlite_scan_fallback",
            "trigram_index": "active" if trigram_active else "unavailable",
            "counts": counts,
            "projection": projection,
            "capture_coverage": coverage,
        }
