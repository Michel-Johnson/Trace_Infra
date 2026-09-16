-- ClickHouse production schema for span analytics.
-- Column names match ew_examples/analytics.py. Local sqlite uses the same
-- names (String -> TEXT, JSON as TEXT). Raw events stay in object storage;
-- this table is filled by ingesting those objects (S3 as WAL).

CREATE TABLE span_events
(
    run_id      String,
    span_id     String,
    sandbox_id  String,
    t           UInt32,
    ts          Float64,
    action      LowCardinality(String),
    status      LowCardinality(String),
    cost        Int32,
    event_uri   String,
    observation String,
    protocol    UInt8
)
ENGINE = MergeTree
ORDER BY (run_id, span_id, t);
