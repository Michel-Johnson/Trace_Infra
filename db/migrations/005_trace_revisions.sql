-- Original run documents remain untouched. These resources retain exact input bytes.
CREATE TABLE trace_heads (
    project_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    latest_revision INTEGER NOT NULL CHECK (latest_revision >= 0),
    PRIMARY KEY (project_id, run_id)
);

CREATE TABLE trace_revisions (
    project_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision > 0),
    content_digest TEXT NOT NULL CHECK (length(content_digest) = 71),
    size_bytes BIGINT NOT NULL CHECK (size_bytes >= 0),
    media_type TEXT NOT NULL,
    format_version TEXT NOT NULL,
    previous_revision INTEGER,
    derivation TEXT NOT NULL CHECK (derivation IN ('capture', 'supplement', 'correction', 'legacy_import')),
    metadata TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (project_id, run_id, revision),
    FOREIGN KEY (project_id, run_id) REFERENCES trace_heads(project_id, run_id),
    FOREIGN KEY (project_id, run_id, previous_revision) REFERENCES trace_revisions(project_id, run_id, revision),
    CHECK ((revision = 1 AND previous_revision IS NULL) OR
           (revision > 1 AND previous_revision IS NOT NULL AND previous_revision = revision - 1))
);
CREATE INDEX trace_revisions_content ON trace_revisions(project_id, content_digest);

CREATE TABLE trace_revision_requests (
    project_id TEXT NOT NULL,
    request_key TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    run_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision > 0),
    PRIMARY KEY (project_id, request_key)
);
