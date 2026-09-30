CREATE TABLE external_actions (
    project_id TEXT NOT NULL,
    action_id TEXT NOT NULL,
    invocation_id TEXT NOT NULL,
    action_key_hash TEXT NOT NULL,
    action_key TEXT NOT NULL,
    spec_digest TEXT NOT NULL,
    spec TEXT NOT NULL,
    created_attempt INTEGER NOT NULL,
    created_grant TEXT NOT NULL,
    created_ms BIGINT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('prepared','running','outcome_unknown','applied','not_applied','abandoned')),
    version INTEGER NOT NULL CHECK(version > 0),
    current_execution INTEGER NOT NULL CHECK(current_execution >= 0),
    PRIMARY KEY(project_id,action_id),
    UNIQUE(project_id,invocation_id,action_key_hash),
    FOREIGN KEY(project_id,invocation_id) REFERENCES invocations(project_id,invocation_id),
    FOREIGN KEY(created_grant) REFERENCES invocation_task_execution_grants(grant_id)
);
CREATE INDEX external_actions_invocation ON external_actions(project_id,invocation_id,state,created_ms,action_id);
CREATE TABLE external_action_executions (
    project_id TEXT NOT NULL,
    action_id TEXT NOT NULL,
    execution INTEGER NOT NULL,
    invocation_attempt INTEGER NOT NULL,
    grant_id TEXT NOT NULL,
    fence_id TEXT NOT NULL,
    start_version INTEGER NOT NULL,
    started_ms BIGINT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('running','outcome_unknown','applied','not_applied')),
    PRIMARY KEY(project_id,action_id,execution),
    FOREIGN KEY(project_id,action_id) REFERENCES external_actions(project_id,action_id),
    FOREIGN KEY(grant_id) REFERENCES invocation_task_execution_grants(grant_id)
);
CREATE TABLE external_action_events (
    project_id TEXT NOT NULL,
    action_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    event_kind TEXT NOT NULL,
    event_digest TEXT NOT NULL,
    event TEXT NOT NULL,
    PRIMARY KEY(project_id,action_id,version),
    FOREIGN KEY(project_id,action_id) REFERENCES external_actions(project_id,action_id)
);
CREATE TABLE external_action_receipts (
    project_id TEXT NOT NULL,
    action_id TEXT NOT NULL,
    execution INTEGER NOT NULL,
    report_key_hash TEXT NOT NULL,
    report_key TEXT NOT NULL,
    submission_digest TEXT NOT NULL,
    receipt_digest TEXT NOT NULL,
    receipt TEXT NOT NULL,
    PRIMARY KEY(project_id,action_id,report_key_hash),
    FOREIGN KEY(project_id,action_id,execution) REFERENCES external_action_executions(project_id,action_id,execution)
);
