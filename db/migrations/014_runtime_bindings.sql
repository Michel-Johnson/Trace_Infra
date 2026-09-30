CREATE TABLE runtime_heads (
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    runtime_id TEXT NOT NULL,
    current_revision INTEGER NOT NULL CHECK(current_revision>=0),
    PRIMARY KEY(project_id,runtime_id)
);
CREATE TABLE runtime_bindings (
    project_id TEXT NOT NULL,
    runtime_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK(revision>0),
    spec_digest TEXT NOT NULL,
    spec TEXT NOT NULL,
    enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
    actor_kind TEXT NOT NULL,
    actor_principal_id TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY(project_id,runtime_id,revision),
    FOREIGN KEY(project_id,runtime_id) REFERENCES runtime_heads(project_id,runtime_id)
);
CREATE TABLE runtime_binding_requests (
    project_id TEXT NOT NULL,
    request_key_hash TEXT NOT NULL,
    request_key TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    runtime_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    PRIMARY KEY(project_id,request_key_hash),
    FOREIGN KEY(project_id,runtime_id,revision) REFERENCES runtime_bindings(project_id,runtime_id,revision)
        DEFERRABLE INITIALLY DEFERRED
);
CREATE TABLE runtime_probes (
    project_id TEXT NOT NULL,
    runtime_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    sequence INTEGER NOT NULL CHECK(sequence>0),
    state TEXT NOT NULL CHECK(state IN ('incomplete','succeeded','failed')),
    started_ms BIGINT NOT NULL,
    completed_ms BIGINT,
    elapsed_ms INTEGER,
    document TEXT,
    document_digest TEXT,
    protocol_supported INTEGER CHECK(protocol_supported IN (0,1)),
    operation_count INTEGER,
    failure_code TEXT,
    http_status INTEGER,
    actor_kind TEXT NOT NULL,
    actor_principal_id TEXT,
    PRIMARY KEY(project_id,runtime_id,revision,sequence),
    FOREIGN KEY(project_id,runtime_id,revision) REFERENCES runtime_bindings(project_id,runtime_id,revision)
);
CREATE TABLE runtime_advertised_operations (
    project_id TEXT NOT NULL,
    runtime_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    sequence INTEGER NOT NULL,
    operation_id TEXT NOT NULL,
    operation_version TEXT NOT NULL,
    operation_digest TEXT NOT NULL,
    PRIMARY KEY(project_id,runtime_id,revision,sequence,operation_id,operation_version),
    FOREIGN KEY(project_id,runtime_id,revision,sequence) REFERENCES runtime_probes(project_id,runtime_id,revision,sequence)
);
CREATE INDEX runtime_advertised_lookup ON runtime_advertised_operations(project_id,operation_id,operation_version,operation_digest);
