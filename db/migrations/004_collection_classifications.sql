-- Immutable dispatch identity across resumable chunks of up to 50 plugin jobs.
CREATE TABLE collection_classifications (
    request_key TEXT PRIMARY KEY,
    request_digest TEXT NOT NULL,
    payload TEXT NOT NULL
);
