-- Immutable publications with a separate stable computation description.
CREATE TABLE artifacts (
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    artifact_id TEXT NOT NULL,
    artifact_type TEXT NOT NULL,
    descriptor_digest TEXT NOT NULL CHECK(length(descriptor_digest)=71),
    spec TEXT NOT NULL,
    content_digest TEXT NOT NULL CHECK(length(content_digest)=71),
    size_bytes BIGINT NOT NULL CHECK(size_bytes>=0 AND size_bytes<=8388608),
    media_type TEXT NOT NULL,
    producer_name_hex TEXT,
    producer_version_hex TEXT,
    config_digest TEXT NOT NULL CHECK(length(config_digest)=71),
    input_count INTEGER NOT NULL CHECK(input_count>=0 AND input_count<=100),
    actor_kind TEXT NOT NULL CHECK(actor_kind IN ('unknown','operator','service')),
    actor_principal_id TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY(project_id,artifact_id)
);
CREATE INDEX artifacts_created ON artifacts(project_id,created_at,artifact_id);
CREATE INDEX artifacts_type ON artifacts(project_id,artifact_type,created_at,artifact_id);

CREATE TABLE artifact_inputs (
    project_id TEXT NOT NULL,
    artifact_id TEXT NOT NULL,
    input_position INTEGER NOT NULL CHECK(input_position>=0 AND input_position<100),
    role TEXT NOT NULL,
    upstream_kind TEXT NOT NULL CHECK(upstream_kind IN ('trace_revision','selection_snapshot','artifact')),
    upstream_digest TEXT NOT NULL CHECK(length(upstream_digest)=71),
    trace_run_id TEXT,
    trace_revision INTEGER,
    selection_id TEXT,
    upstream_artifact_id TEXT,
    PRIMARY KEY(project_id,artifact_id,input_position),
    FOREIGN KEY(project_id,artifact_id) REFERENCES artifacts(project_id,artifact_id),
    FOREIGN KEY(project_id,trace_run_id,trace_revision) REFERENCES trace_revisions(project_id,run_id,revision),
    FOREIGN KEY(project_id,selection_id) REFERENCES selection_snapshots(project_id,selection_id),
    FOREIGN KEY(project_id,upstream_artifact_id) REFERENCES artifacts(project_id,artifact_id),
    CHECK (
        (upstream_kind='trace_revision' AND trace_run_id IS NOT NULL AND trace_revision IS NOT NULL AND trace_revision>0
            AND selection_id IS NULL AND upstream_artifact_id IS NULL)
        OR (upstream_kind='selection_snapshot' AND selection_id IS NOT NULL
            AND trace_run_id IS NULL AND trace_revision IS NULL AND upstream_artifact_id IS NULL)
        OR (upstream_kind='artifact' AND upstream_artifact_id IS NOT NULL
            AND trace_run_id IS NULL AND trace_revision IS NULL AND selection_id IS NULL)
    )
);
CREATE INDEX artifact_inputs_trace ON artifact_inputs(project_id,trace_run_id,trace_revision);
CREATE INDEX artifact_inputs_selection ON artifact_inputs(project_id,selection_id);
CREATE INDEX artifact_inputs_artifact ON artifact_inputs(project_id,upstream_artifact_id);

-- Hash the opaque key for a bounded B-tree key, retaining the original for exact
-- equality checking. In particular, long Unicode keys must not exceed PG limits.
CREATE TABLE artifact_requests (
    project_id TEXT NOT NULL,
    request_key_hash TEXT NOT NULL CHECK(length(request_key_hash)=64),
    request_key TEXT NOT NULL,
    fingerprint TEXT NOT NULL CHECK(length(fingerprint)=71),
    artifact_id TEXT NOT NULL,
    PRIMARY KEY(project_id,request_key_hash),
    FOREIGN KEY(project_id,artifact_id) REFERENCES artifacts(project_id,artifact_id)
        DEFERRABLE INITIALLY DEFERRED
);
