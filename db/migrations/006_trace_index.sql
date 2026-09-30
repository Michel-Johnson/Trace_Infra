-- Rebuildable projections only: immutable revision content remains the authority.
CREATE TABLE trace_indexes (
    project_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    projector_version TEXT NOT NULL,
    content_digest TEXT NOT NULL,
    format_version TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('unindexed', 'complete', 'failed')),
    error_code TEXT,
    query_id TEXT,
    env_id TEXT,
    harness TEXT,
    model TEXT,
    run_status TEXT,
    title TEXT,
    record_count INTEGER,
    model_count INTEGER,
    model_batch_count INTEGER,
    tool_count INTEGER,
    agent_count INTEGER,
    wait_count INTEGER,
    other_count INTEGER,
    unknown_count INTEGER,
    capture_coverage TEXT,
    identity_basis TEXT NOT NULL,
    projection_digest TEXT,
    indexed_at TEXT,
    PRIMARY KEY (project_id, run_id, revision, projector_version),
    FOREIGN KEY (project_id, run_id, revision)
        REFERENCES trace_revisions(project_id, run_id, revision),
    CHECK ((state = 'complete' AND error_code IS NULL AND record_count IS NOT NULL
            AND projection_digest IS NOT NULL AND indexed_at IS NOT NULL)
        OR (state = 'failed' AND error_code IS NOT NULL AND record_count IS NULL)
        OR state = 'unindexed')
);
CREATE INDEX trace_indexes_query ON trace_indexes(project_id, projector_version, query_id, state);

CREATE TABLE trace_index_records (
    project_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    projector_version TEXT NOT NULL,
    source_ordinal INTEGER NOT NULL CHECK (source_ordinal >= 0),
    span_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    operation TEXT,
    status TEXT NOT NULL,
    agent_id TEXT,
    segment_id TEXT,
    turn_id TEXT,
    stream_id TEXT,
    source_sequence BIGINT,
    clock_id TEXT,
    clock_kind TEXT,
    clock_origin_at TEXT,
    clock_uncertainty_ms DOUBLE PRECISION,
    start_ms DOUBLE PRECISION,
    end_ms DOUBLE PRECISION,
    duration_ms DOUBLE PRECISION,
    duration_basis TEXT,
    duration_scope TEXT,
    first_response_ms DOUBLE PRECISION,
    last_response_ms DOUBLE PRECISION,
    response_boundary TEXT,
    PRIMARY KEY (project_id, run_id, revision, projector_version, source_ordinal),
    UNIQUE (project_id, run_id, revision, projector_version, span_id),
    FOREIGN KEY (project_id, run_id, revision, projector_version)
        REFERENCES trace_indexes(project_id, run_id, revision, projector_version)
);
CREATE INDEX trace_index_records_kind ON trace_index_records(project_id, projector_version, kind, status);
