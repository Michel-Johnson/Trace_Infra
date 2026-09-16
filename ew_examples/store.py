"""Phase 2: SQL index + object store with the S3 key layout.

Postgres DDL is docs/spec/schema.sql. This process uses sqlite3 with the same
table and column names so the pack stays standard-library-only. Span events and
snapshots are files, not rows.
"""
from __future__ import annotations

import json
import os
import sqlite3
from typing import Any


class TraceError(Exception):
    """A trace API refusal. `code` matches docs/spec/phase-0.md."""

    def __init__(self, code: str, message: str, **extra: Any):
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra

    def as_dict(self) -> dict:
        out = {"error": self.code, "message": self.message}
        out.update(self.extra)
        return out


def _write_json(path: str, obj: Any) -> None:
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2)


def _read_json(path: str) -> Any:
    with open(path) as fh:
        return json.load(fh)


# Same tables as docs/spec/schema.sql. JSONB -> TEXT, TIMESTAMPTZ -> REAL.
SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  run_id        TEXT PRIMARY KEY,
  task_id       TEXT NOT NULL,
  seed          INTEGER NOT NULL,
  backend       TEXT NOT NULL CHECK (backend IN ('episode', 'firecracker')),
  created_at    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS states (
  state_id         TEXT PRIMARY KEY,
  run_id           TEXT NOT NULL REFERENCES runs (run_id),
  parent_state_id  TEXT REFERENCES states (state_id),
  from_span_id     TEXT,
  t                INTEGER NOT NULL,
  backend          TEXT NOT NULL CHECK (backend IN ('episode', 'firecracker')),
  snapshot_uri     TEXT NOT NULL,
  prompt_uri       TEXT,
  budget           TEXT NOT NULL,
  created_at       REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS spans (
  span_id         TEXT PRIMARY KEY,
  run_id          TEXT NOT NULL REFERENCES runs (run_id),
  from_state_id   TEXT NOT NULL REFERENCES states (state_id),
  to_state_id     TEXT REFERENCES states (state_id),
  sandbox_id      TEXT,
  t_start         INTEGER NOT NULL,
  t_end           INTEGER,
  created_at      REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS span_index (
  span_id     TEXT NOT NULL REFERENCES spans (span_id),
  t           INTEGER NOT NULL,
  event_uri   TEXT NOT NULL,
  action      TEXT NOT NULL,
  status      TEXT NOT NULL,
  PRIMARY KEY (span_id, t)
);

CREATE INDEX IF NOT EXISTS states_run_id ON states (run_id);
CREATE INDEX IF NOT EXISTS spans_run_id ON spans (run_id);
CREATE INDEX IF NOT EXISTS spans_from_state ON spans (from_state_id);
"""


def _as_dict(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    return {k: row[k] for k in row.keys()}


def _budget_out(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    return json.loads(raw)


class LocalStore:
    """Blobs on disk (S3 keys). Index in sqlite (Postgres tables)."""

    def __init__(self, root: str):
        self.root = os.path.abspath(root)
        os.makedirs(self.root, exist_ok=True)
        self.db_path = os.path.join(self.root, "index.sqlite")
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SQLITE_SCHEMA)
        self.conn.commit()

    def snapshot_uri(self, state_id: str) -> str:
        return os.path.join(self.root, "snapshots", state_id) + os.sep

    def prompt_uri(self, run_id: str, state_id: str) -> str:
        return os.path.join(self.root, "runs", run_id, "prompts", f"{state_id}.json")

    def event_uri(self, run_id: str, span_id: str, t: int) -> str:
        return os.path.join(
            self.root, "runs", run_id, "spans", span_id, "events", f"{t:08d}.json")

    def _execute(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        cur = self.conn.execute(sql, args)
        self.conn.commit()
        return cur

    def put_run(self, rec: dict) -> None:
        self._execute(
            "INSERT INTO runs (run_id, task_id, seed, backend, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (rec["run_id"], rec["task_id"], int(rec["seed"]), rec["backend"],
             float(rec["created_at"])))

    def get_run(self, run_id: str) -> dict:
        row = _as_dict(self.conn.execute(
            "SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone())
        if row is None:
            raise TraceError("StateNotFound", f"no run {run_id}")
        return row

    def put_state(self, rec: dict) -> None:
        self._execute(
            "INSERT INTO states (state_id, run_id, parent_state_id, from_span_id, "
            "t, backend, snapshot_uri, prompt_uri, budget, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (rec["state_id"], rec["run_id"], rec.get("parent_state_id"),
             rec.get("from_span_id"), int(rec["t"]), rec["backend"],
             rec["snapshot_uri"], rec.get("prompt_uri"),
             json.dumps(rec["budget"], ensure_ascii=False),
             float(rec["created_at"])))

    def get_state(self, state_id: str) -> dict:
        row = _as_dict(self.conn.execute(
            "SELECT * FROM states WHERE state_id = ?", (state_id,)).fetchone())
        if row is None:
            raise TraceError("StateNotFound", f"no state {state_id}")
        row["budget"] = _budget_out(row["budget"])
        return row

    def put_span(self, rec: dict) -> None:
        self._execute(
            "INSERT INTO spans (span_id, run_id, from_state_id, to_state_id, "
            "sandbox_id, t_start, t_end, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(span_id) DO UPDATE SET "
            "to_state_id=excluded.to_state_id, "
            "t_end=excluded.t_end, "
            "sandbox_id=excluded.sandbox_id",
            (rec["span_id"], rec["run_id"], rec["from_state_id"],
             rec.get("to_state_id"), rec.get("sandbox_id"),
             int(rec["t_start"]), rec.get("t_end"), float(rec["created_at"])))

    def get_span(self, span_id: str) -> dict:
        row = _as_dict(self.conn.execute(
            "SELECT * FROM spans WHERE span_id = ?", (span_id,)).fetchone())
        if row is None:
            raise TraceError("SpanNotFound", f"no span {span_id}")
        return row

    def put_span_index(self, span_id: str, t: int, rec: dict) -> None:
        self._execute(
            "INSERT INTO span_index (span_id, t, event_uri, action, status) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(span_id, t) DO UPDATE SET "
            "event_uri=excluded.event_uri, "
            "action=excluded.action, "
            "status=excluded.status",
            (span_id, int(t), rec["event_uri"], rec["action"], rec["status"]))

    def list_span_index(self, span_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT span_id, t, event_uri, action, status "
            "FROM span_index WHERE span_id = ? ORDER BY t ASC",
            (span_id,)).fetchall()
        return [_as_dict(r) for r in rows]

    def write_event(self, run_id: str, span_id: str, row: dict) -> str:
        uri = self.event_uri(run_id, span_id, row["t"])
        _write_json(uri, row)
        self.put_span_index(span_id, row["t"], {
            "span_id": span_id,
            "t": row["t"],
            "event_uri": uri,
            "action": row["action"],
            "status": row["status"],
        })
        return uri

    def read_event(self, uri: str) -> dict:
        return _read_json(uri)

    def write_snapshot(self, state_id: str, metadata: dict, episode_obj: dict) -> str:
        base = os.path.join(self.root, "snapshots", state_id)
        _write_json(os.path.join(base, "metadata.json"), metadata)
        _write_json(os.path.join(base, "episode.json"), episode_obj)
        return self.snapshot_uri(state_id)

    def read_episode(self, state_id: str) -> dict:
        path = os.path.join(self.root, "snapshots", state_id, "episode.json")
        if not os.path.isfile(path):
            raise TraceError("StateNotFound", f"no episode snapshot for {state_id}")
        return _read_json(path)

    def read_metadata(self, state_id: str) -> dict:
        path = os.path.join(self.root, "snapshots", state_id, "metadata.json")
        if not os.path.isfile(path):
            raise TraceError("StateNotFound", f"no snapshot metadata for {state_id}")
        return _read_json(path)

    def write_prompt(self, run_id: str, state_id: str, prompt: dict) -> str:
        uri = self.prompt_uri(run_id, state_id)
        _write_json(uri, prompt)
        return uri

    def read_prompt(self, uri: str | None) -> dict | None:
        if not uri:
            return None
        if not os.path.isfile(uri):
            return None
        return _read_json(uri)
