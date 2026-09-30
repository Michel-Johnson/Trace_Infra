-- Register project identities without rewriting historical trace documents or IDs.
CREATE TABLE projects (
    project_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);

INSERT INTO projects(project_id, name, created_at)
VALUES('default', 'Default', CURRENT_TIMESTAMP)
ON CONFLICT(project_id) DO NOTHING;

INSERT INTO projects(project_id, name, created_at)
SELECT DISTINCT project_id, project_id, CURRENT_TIMESTAMP FROM trace_heads WHERE 1=1
ON CONFLICT(project_id) DO NOTHING;

CREATE TABLE service_principals (
    principal_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    name TEXT NOT NULL,
    scopes TEXT NOT NULL,
    created_at TEXT NOT NULL,
    revoked_at TEXT
);
CREATE INDEX service_principals_project ON service_principals(project_id, created_at, principal_id);

CREATE TABLE service_credentials (
    credential_id TEXT PRIMARY KEY,
    principal_id TEXT NOT NULL REFERENCES service_principals(principal_id),
    token_digest TEXT NOT NULL UNIQUE CHECK(length(token_digest) = 64),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked_at TEXT
);
CREATE INDEX service_credentials_principal ON service_credentials(principal_id, created_at, credential_id);
