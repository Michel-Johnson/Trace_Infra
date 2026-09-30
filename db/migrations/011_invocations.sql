CREATE TABLE operation_versions (
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    operation_id TEXT NOT NULL,
    version TEXT NOT NULL,
    definition_digest TEXT NOT NULL,
    definition TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(project_id,operation_id,version)
);

CREATE TABLE invocations (
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    invocation_id TEXT NOT NULL,
    operation_id TEXT NOT NULL,
    operation_version TEXT NOT NULL,
    operation_digest TEXT NOT NULL,
    spec_digest TEXT NOT NULL,
    spec TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending','running','succeeded','failed','cancelled','blocked')),
    input_count INTEGER NOT NULL CHECK(input_count>=0 AND input_count<=100),
    actor_kind TEXT NOT NULL CHECK(actor_kind IN ('unknown','operator','service')),
    actor_principal_id TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY(project_id,invocation_id),
    FOREIGN KEY(project_id,operation_id,operation_version)
        REFERENCES operation_versions(project_id,operation_id,version)
);
CREATE INDEX invocations_created ON invocations(project_id,created_at,invocation_id);

CREATE TABLE invocation_inputs (
    project_id TEXT NOT NULL,
    invocation_id TEXT NOT NULL,
    input_position INTEGER NOT NULL CHECK(input_position>=0 AND input_position<100),
    role TEXT NOT NULL,
    upstream_kind TEXT NOT NULL CHECK(upstream_kind IN ('trace_revision','selection_snapshot','artifact')),
    upstream_digest TEXT NOT NULL,
    trace_run_id TEXT,
    trace_revision INTEGER,
    selection_id TEXT,
    upstream_artifact_id TEXT,
    PRIMARY KEY(project_id,invocation_id,input_position),
    FOREIGN KEY(project_id,invocation_id) REFERENCES invocations(project_id,invocation_id),
    FOREIGN KEY(project_id,trace_run_id,trace_revision) REFERENCES trace_revisions(project_id,run_id,revision),
    FOREIGN KEY(project_id,selection_id) REFERENCES selection_snapshots(project_id,selection_id),
    FOREIGN KEY(project_id,upstream_artifact_id) REFERENCES artifacts(project_id,artifact_id),
    CHECK (
        (upstream_kind='trace_revision' AND trace_run_id IS NOT NULL AND trace_revision IS NOT NULL
            AND trace_revision>0 AND selection_id IS NULL AND upstream_artifact_id IS NULL)
        OR (upstream_kind='selection_snapshot' AND selection_id IS NOT NULL
            AND trace_run_id IS NULL AND trace_revision IS NULL AND upstream_artifact_id IS NULL)
        OR (upstream_kind='artifact' AND upstream_artifact_id IS NOT NULL
            AND trace_run_id IS NULL AND trace_revision IS NULL AND selection_id IS NULL)
    )
);

CREATE TABLE invocation_requests (
    project_id TEXT NOT NULL,
    request_key_hash TEXT NOT NULL,
    request_key TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    invocation_id TEXT NOT NULL,
    PRIMARY KEY(project_id,request_key_hash),
    FOREIGN KEY(project_id,invocation_id) REFERENCES invocations(project_id,invocation_id)
        DEFERRABLE INITIALLY DEFERRED
);
