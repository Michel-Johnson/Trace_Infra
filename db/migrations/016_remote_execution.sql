-- Presence grants execution; existing task credentials remain input-only.
CREATE TABLE invocation_task_execution_grants (
    grant_id TEXT PRIMARY KEY,
    FOREIGN KEY(grant_id) REFERENCES invocation_task_grants(grant_id)
);
CREATE TABLE invocation_remote_results (
    project_id TEXT NOT NULL,
    invocation_id TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    grant_id TEXT NOT NULL,
    evidence_digest TEXT NOT NULL,
    evidence TEXT NOT NULL,
    PRIMARY KEY(project_id,invocation_id,attempt),
    FOREIGN KEY(project_id,invocation_id,attempt) REFERENCES invocation_results(project_id,invocation_id,attempt),
    FOREIGN KEY(grant_id) REFERENCES invocation_task_execution_grants(grant_id)
);
