-- Queryable model usage stays on the existing object projection; no new table.
ALTER TABLE trace_objects ADD COLUMN IF NOT EXISTS model_provider TEXT;
ALTER TABLE trace_objects ADD COLUMN IF NOT EXISTS requested_model TEXT;
ALTER TABLE trace_objects ADD COLUMN IF NOT EXISTS response_model TEXT;
ALTER TABLE trace_objects ADD COLUMN IF NOT EXISTS usage_completeness TEXT;
ALTER TABLE trace_objects ADD COLUMN IF NOT EXISTS input_tokens BIGINT;
ALTER TABLE trace_objects ADD COLUMN IF NOT EXISTS output_tokens BIGINT;
ALTER TABLE trace_objects ADD COLUMN IF NOT EXISTS cache_read_tokens BIGINT;
ALTER TABLE trace_objects ADD COLUMN IF NOT EXISTS cache_write_tokens BIGINT;
ALTER TABLE trace_objects ADD COLUMN IF NOT EXISTS reasoning_tokens BIGINT;

CREATE INDEX IF NOT EXISTS trace_objects_model_usage
    ON trace_objects(project_id,projector_version,requested_model,response_model,usage_completeness);
