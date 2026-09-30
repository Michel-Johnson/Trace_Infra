CREATE TABLE invocation_task_grants (
    grant_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    invocation_id TEXT NOT NULL,
    attempt INTEGER NOT NULL CHECK(attempt > 0),
    runtime_id TEXT NOT NULL,
    runtime_revision INTEGER NOT NULL,
    runtime_digest TEXT NOT NULL,
    probe_sequence INTEGER NOT NULL,
    token_digest TEXT NOT NULL UNIQUE,
    issued_ms BIGINT NOT NULL,
    expires_ms BIGINT NOT NULL,
    revoked_ms BIGINT,
    FOREIGN KEY(project_id,invocation_id,attempt) REFERENCES invocation_attempts(project_id,invocation_id,attempt),
    FOREIGN KEY(project_id,runtime_id,runtime_revision,probe_sequence) REFERENCES runtime_probes(project_id,runtime_id,revision,sequence)
);
CREATE INDEX task_grants_attempt ON invocation_task_grants(project_id,invocation_id,attempt,issued_ms,grant_id);
