-- Bounded source-order neighborhoods reuse the existing trace object projection.
-- The partial index keeps span windows proportional to the requested range.
CREATE INDEX IF NOT EXISTS trace_objects_source_window
    ON trace_objects(project_id,run_id,revision,projector_version,source_ordinal)
    WHERE object_kind='span';
