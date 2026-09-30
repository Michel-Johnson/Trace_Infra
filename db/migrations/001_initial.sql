CREATE TABLE runs (
    id TEXT PRIMARY KEY,
    digest TEXT NOT NULL CHECK (length(digest) = 64),
    imported_at TEXT NOT NULL,
    payload TEXT NOT NULL,
    query_id TEXT NOT NULL,
    env_id TEXT NOT NULL,
    metadata JSONB NOT NULL
);
CREATE INDEX runs_query_import ON runs(query_id, imported_at, id);
CREATE INDEX runs_env ON runs(env_id);
CREATE TABLE analyses (
    digest TEXT NOT NULL,
    version TEXT NOT NULL,
    scope TEXT NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY(digest, version, scope)
);
CREATE TABLE collections (
    id TEXT PRIMARY KEY,
    digest TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_seq BIGINT GENERATED ALWAYS AS IDENTITY
);
CREATE TABLE case_definitions (
    query_id TEXT PRIMARY KEY,
    digest TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE collection_cases (
    collection_id TEXT NOT NULL REFERENCES collections(id),
    query_id TEXT NOT NULL REFERENCES case_definitions(query_id),
    position INTEGER NOT NULL CHECK (position >= 0),
    created_seq BIGINT GENERATED ALWAYS AS IDENTITY,
    PRIMARY KEY(collection_id, query_id),
    UNIQUE(collection_id, position)
);
CREATE INDEX collection_cases_query ON collection_cases(query_id);
