-- Query foundation for trace-index/4.  These are rebuildable projection facts;
-- immutable source documents and Trace revisions are not rewritten.
ALTER TABLE trace_revisions
    ADD COLUMN IF NOT EXISTS attributes JSONB NOT NULL DEFAULT '{}'::jsonb;

ALTER TABLE trace_objects
    ADD COLUMN IF NOT EXISTS attributes JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS start_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS end_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS session_namespace TEXT,
    ADD COLUMN IF NOT EXISTS session_id TEXT;

CREATE INDEX IF NOT EXISTS trace_revisions_attributes
    ON trace_revisions USING GIN(attributes jsonb_path_ops);
CREATE INDEX IF NOT EXISTS trace_objects_attributes
    ON trace_objects USING GIN(attributes jsonb_path_ops);
CREATE INDEX IF NOT EXISTS trace_objects_absolute_time
    ON trace_objects(project_id,projector_version,start_at,run_id,revision,object_ordinal)
    WHERE start_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS trace_objects_session
    ON trace_objects(project_id,projector_version,session_namespace,session_id,start_at,run_id,revision)
    WHERE session_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS trace_edges_reverse_lookup
    ON trace_edges(project_id,run_id,revision,projector_version,relation,target_id);
