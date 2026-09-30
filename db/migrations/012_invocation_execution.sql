CREATE TABLE invocation_attempts (
    project_id TEXT NOT NULL,
    invocation_id TEXT NOT NULL,
    attempt INTEGER NOT NULL CHECK(attempt>0),
    lease_id TEXT NOT NULL,
    worker_id TEXT NOT NULL,
    owner_kind TEXT NOT NULL CHECK(owner_kind IN ('operator','service')),
    owner_principal_id TEXT,
    status TEXT NOT NULL CHECK(status IN ('running','succeeded','failed','expired','cancelled')),
    lease_seconds INTEGER NOT NULL CHECK(lease_seconds>=1 AND lease_seconds<=300),
    started_ms BIGINT NOT NULL,
    heartbeat_ms BIGINT NOT NULL,
    lease_expires_ms BIGINT NOT NULL,
    finished_ms BIGINT,
    PRIMARY KEY(project_id,invocation_id,attempt),
    FOREIGN KEY(project_id,invocation_id) REFERENCES invocations(project_id,invocation_id)
);
CREATE TABLE invocation_claim_requests (
    project_id TEXT NOT NULL,
    invocation_id TEXT NOT NULL,
    request_key_hash TEXT NOT NULL,
    request_key TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    PRIMARY KEY(project_id,invocation_id,request_key_hash),
    FOREIGN KEY(project_id,invocation_id,attempt) REFERENCES invocation_attempts(project_id,invocation_id,attempt)
);
CREATE TABLE invocation_results (
    project_id TEXT NOT NULL,
    invocation_id TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    submission_digest TEXT NOT NULL,
    receipt_digest TEXT NOT NULL,
    receipt TEXT NOT NULL,
    PRIMARY KEY(project_id,invocation_id,attempt),
    FOREIGN KEY(project_id,invocation_id,attempt) REFERENCES invocation_attempts(project_id,invocation_id,attempt)
);
CREATE TABLE invocation_outputs (
    project_id TEXT NOT NULL,
    invocation_id TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    output_position INTEGER NOT NULL CHECK(output_position>=0 AND output_position<100),
    role TEXT NOT NULL,
    artifact_id TEXT NOT NULL,
    descriptor_digest TEXT NOT NULL,
    PRIMARY KEY(project_id,invocation_id,attempt,output_position),
    FOREIGN KEY(project_id,invocation_id,attempt) REFERENCES invocation_attempts(project_id,invocation_id,attempt),
    FOREIGN KEY(project_id,artifact_id) REFERENCES artifacts(project_id,artifact_id)
);
CREATE TABLE invocation_events (
    project_id TEXT NOT NULL,
    invocation_id TEXT NOT NULL,
    sequence INTEGER NOT NULL CHECK(sequence>0),
    attempt INTEGER NOT NULL CHECK(attempt>=0),
    kind TEXT NOT NULL,
    actor_kind TEXT NOT NULL,
    actor_principal_id TEXT,
    recorded_ms BIGINT NOT NULL,
    PRIMARY KEY(project_id,invocation_id,sequence),
    FOREIGN KEY(project_id,invocation_id) REFERENCES invocations(project_id,invocation_id)
);
