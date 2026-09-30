CREATE TABLE notification_heads (
    project_id TEXT PRIMARY KEY,
    sequence BIGINT NOT NULL CHECK(sequence >= 0),
    FOREIGN KEY(project_id) REFERENCES projects(project_id)
);
CREATE TABLE notifications (
    project_id TEXT NOT NULL,
    sequence BIGINT NOT NULL CHECK(sequence > 0),
    event_type TEXT NOT NULL,
    invocation_id TEXT NOT NULL,
    attempt INTEGER,
    payload TEXT NOT NULL,
    payload_digest TEXT NOT NULL,
    recorded_ms BIGINT NOT NULL,
    PRIMARY KEY(project_id,sequence),
    FOREIGN KEY(project_id,invocation_id) REFERENCES invocations(project_id,invocation_id)
);
CREATE TABLE notification_consumers (
    project_id TEXT NOT NULL,
    consumer_id TEXT NOT NULL,
    owner_kind TEXT NOT NULL,
    owner_principal_id TEXT,
    acknowledged_sequence BIGINT NOT NULL CHECK(acknowledged_sequence >= 0),
    created_ms BIGINT NOT NULL,
    updated_ms BIGINT NOT NULL,
    PRIMARY KEY(project_id,consumer_id),
    FOREIGN KEY(project_id) REFERENCES projects(project_id)
);
