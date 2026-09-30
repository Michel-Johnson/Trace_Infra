-- Stable recent-trace paging. Source revisions and payloads remain immutable.
CREATE INDEX trace_revisions_recent
    ON trace_revisions(project_id, created_at DESC, run_id COLLATE "C", revision);
