-- Lossless UTF-8 hex metadata avoids PostgreSQL JSONB's rejection of JSON NUL.
-- Revision JSON and its metadata TEXT remain the immutable source of truth.
CREATE TABLE trace_revision_lookup (
    project_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    query_id_hex TEXT,
    env_id_hex TEXT,
    harness_hex TEXT,
    model_hex TEXT,
    status_hex TEXT,
    PRIMARY KEY(project_id, run_id, revision),
    FOREIGN KEY(project_id, run_id, revision)
        REFERENCES trace_revisions(project_id, run_id, revision)
);

-- Older native imports could predate explicit project registration. Preserve
-- their original IDs and retain any name already assigned by an operator.
INSERT INTO projects(project_id, name, created_at)
SELECT DISTINCT project_id, project_id, CURRENT_TIMESTAMP FROM trace_heads WHERE 1=1
ON CONFLICT(project_id) DO NOTHING;

-- Prefixes bound B-tree key size for valid 4096-character metadata. Queries
-- retain the complete hex equality predicate; these indexes never truncate data.
CREATE INDEX trace_lookup_query ON trace_revision_lookup(project_id, substr(query_id_hex,1,128));
CREATE INDEX trace_lookup_env ON trace_revision_lookup(project_id, substr(env_id_hex,1,128));
CREATE INDEX trace_lookup_harness ON trace_revision_lookup(project_id, substr(harness_hex,1,128));
