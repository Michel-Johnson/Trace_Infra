-- A selection fixes revision membership; importing/querying alone never creates it.
CREATE TABLE selection_snapshots (
    project_id TEXT NOT NULL,
    selection_id TEXT NOT NULL,
    query_digest TEXT NOT NULL,
    query_spec TEXT NOT NULL,
    member_count INTEGER NOT NULL CHECK(member_count >= 0),
    manifest_digest TEXT NOT NULL CHECK(length(manifest_digest) = 71),
    manifest_size BIGINT NOT NULL CHECK(manifest_size >= 0),
    created_at TEXT NOT NULL,
    actor_kind TEXT NOT NULL CHECK(actor_kind IN ('service', 'operator', 'unknown')),
    actor_principal_id TEXT,
    PRIMARY KEY(project_id, selection_id)
);
CREATE INDEX selection_snapshots_created ON selection_snapshots(project_id, created_at, selection_id);

CREATE TABLE selection_members (
    project_id TEXT NOT NULL,
    selection_id TEXT NOT NULL,
    position INTEGER NOT NULL CHECK(position >= 0),
    run_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK(revision > 0),
    content_digest TEXT NOT NULL CHECK(length(content_digest) = 71),
    PRIMARY KEY(project_id, selection_id, position),
    UNIQUE(project_id, selection_id, run_id, revision),
    FOREIGN KEY(project_id, selection_id) REFERENCES selection_snapshots(project_id, selection_id),
    FOREIGN KEY(project_id, run_id, revision) REFERENCES trace_revisions(project_id, run_id, revision)
);

CREATE TABLE selection_requests (
    project_id TEXT NOT NULL,
    request_key TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    selection_id TEXT NOT NULL,
    PRIMARY KEY(project_id, request_key)
);
