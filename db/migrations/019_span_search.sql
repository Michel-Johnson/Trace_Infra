-- Rebuildable execution facts for structured span search. Source trace bytes
-- remain authoritative; these columns never store input/output bodies.
ALTER TABLE trace_index_records ADD COLUMN parent_id TEXT;
ALTER TABLE trace_index_records ADD COLUMN proposal_id TEXT;
ALTER TABLE trace_index_records ADD COLUMN call_id TEXT;
ALTER TABLE trace_index_records ADD COLUMN invocation_id TEXT;
ALTER TABLE trace_index_records ADD COLUMN attempt INTEGER CHECK(attempt IS NULL OR attempt > 0);
ALTER TABLE trace_index_records ADD COLUMN skill_name TEXT;
ALTER TABLE trace_index_records ADD COLUMN skill_action TEXT CHECK(skill_action IS NULL OR skill_action IN ('load','invoke'));
ALTER TABLE trace_index_records ADD COLUMN source_refs TEXT;

CREATE INDEX trace_index_records_skill
    ON trace_index_records(project_id, projector_version, skill_name, skill_action, status);
CREATE INDEX trace_index_records_duration
    ON trace_index_records(project_id, projector_version, duration_ms);
CREATE INDEX trace_index_records_order
    ON trace_index_records(project_id, run_id, revision, projector_version, source_sequence);
