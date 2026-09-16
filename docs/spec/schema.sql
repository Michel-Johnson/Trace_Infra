-- Trace infra index. Postgres production schema.
-- Column names match docs/spec/phase-0.md. Local sqlite uses the same names
-- (JSONB -> TEXT, TIMESTAMPTZ -> REAL) in ew_examples/store.py.

CREATE TABLE runs (
  run_id        TEXT PRIMARY KEY,
  task_id       TEXT NOT NULL,
  seed          INTEGER NOT NULL,
  backend       TEXT NOT NULL CHECK (backend IN ('episode', 'firecracker')),
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE states (
  state_id         TEXT PRIMARY KEY,
  run_id           TEXT NOT NULL REFERENCES runs (run_id),
  parent_state_id  TEXT REFERENCES states (state_id),
  from_span_id     TEXT,
  t                INTEGER NOT NULL,
  backend          TEXT NOT NULL CHECK (backend IN ('episode', 'firecracker')),
  snapshot_uri     TEXT NOT NULL,
  prompt_uri       TEXT,
  budget           JSONB NOT NULL,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE spans (
  span_id         TEXT PRIMARY KEY,
  run_id          TEXT NOT NULL REFERENCES runs (run_id),
  from_state_id   TEXT NOT NULL REFERENCES states (state_id),
  to_state_id     TEXT REFERENCES states (state_id),
  sandbox_id      TEXT,
  t_start         INTEGER NOT NULL,
  t_end           INTEGER,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE span_index (
  span_id     TEXT NOT NULL REFERENCES spans (span_id),
  t           INTEGER NOT NULL,
  event_uri   TEXT NOT NULL,
  action      TEXT NOT NULL,
  status      TEXT NOT NULL,
  PRIMARY KEY (span_id, t)
);

CREATE INDEX states_run_id ON states (run_id);
CREATE INDEX spans_run_id ON spans (run_id);
CREATE INDEX spans_from_state ON spans (from_state_id);
