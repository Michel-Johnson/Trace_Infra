CREATE TABLE plugin_versions (
    plugin_id TEXT NOT NULL, version TEXT NOT NULL, manifest_digest TEXT NOT NULL,
    manifest TEXT NOT NULL, execution_mode TEXT NOT NULL CHECK (execution_mode IN ('builtin','external')),
    created_at TEXT NOT NULL, PRIMARY KEY(plugin_id,version)
);
CREATE TABLE evaluation_batches (
    id TEXT PRIMARY KEY, created_at TEXT NOT NULL, plugin_id TEXT NOT NULL, plugin_version TEXT NOT NULL,
    collection_id TEXT, scope TEXT NOT NULL, config TEXT NOT NULL, config_digest TEXT NOT NULL,
    request_key TEXT NOT NULL UNIQUE, request_digest TEXT NOT NULL,
    FOREIGN KEY(plugin_id,plugin_version) REFERENCES plugin_versions(plugin_id,version)
);
CREATE TABLE evaluation_jobs (
    id TEXT PRIMARY KEY, batch_id TEXT NOT NULL REFERENCES evaluation_batches(id), run_id TEXT NOT NULL REFERENCES runs(id),
    query_id TEXT NOT NULL, input_digest TEXT NOT NULL, snapshot_digest TEXT NOT NULL, snapshot TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('queued','running','completed','failed','cancelled')),
    attempt INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    grant_hash TEXT, grant_expires DOUBLE PRECISION, position INTEGER NOT NULL DEFAULT 0,
    UNIQUE(batch_id,run_id)
);
CREATE INDEX evaluation_jobs_state ON evaluation_jobs(state,created_at,id);
CREATE INDEX evaluation_jobs_query ON evaluation_jobs(query_id,created_at);
CREATE TABLE evaluation_attempts (
    job_id TEXT NOT NULL REFERENCES evaluation_jobs(id), attempt INTEGER NOT NULL,
    worker TEXT NOT NULL, token_hash TEXT NOT NULL, lease_until DOUBLE PRECISION NOT NULL,
    started_at DOUBLE PRECISION NOT NULL, finished_at DOUBLE PRECISION,
    state TEXT NOT NULL, error TEXT,
    PRIMARY KEY(job_id,attempt)
);
CREATE TABLE evaluation_results (
    job_id TEXT NOT NULL, attempt INTEGER NOT NULL, payload TEXT NOT NULL,
    digest TEXT NOT NULL, created_at TEXT NOT NULL,
    PRIMARY KEY(job_id,attempt), FOREIGN KEY(job_id,attempt) REFERENCES evaluation_attempts(job_id,attempt)
);
