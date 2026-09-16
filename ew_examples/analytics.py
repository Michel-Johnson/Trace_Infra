"""ClickHouse-shaped span analytics. Standard library only.

Production would be ClickHouse (docs/spec/clickhouse.sql). This process uses
sqlite with the same column names. Raw events stay in object storage (S3 keys);
this table is the derived index for filters, matching Langfuse: S3 is the WAL.
"""
from __future__ import annotations

import json
import os
import sqlite3
from typing import Any

from .store import TraceError

CH_SQLITE = """
CREATE TABLE IF NOT EXISTS span_events (
  run_id      TEXT NOT NULL,
  span_id     TEXT NOT NULL,
  sandbox_id  TEXT,
  t           INTEGER NOT NULL,
  ts          REAL NOT NULL,
  action      TEXT NOT NULL,
  status      TEXT NOT NULL,
  cost        INTEGER NOT NULL,
  event_uri   TEXT NOT NULL,
  observation TEXT,
  protocol    INTEGER NOT NULL,
  PRIMARY KEY (span_id, t)
);
CREATE INDEX IF NOT EXISTS span_events_run ON span_events (run_id, t);
CREATE INDEX IF NOT EXISTS span_events_action ON span_events (action);
"""


class ClickHouse:
    """Derived event table. Does not store snapshots."""

    def __init__(self, path: str):
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        self.path = os.path.abspath(path)
        self.conn = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(CH_SQLITE)
        self.conn.commit()

    def ingest_row(self, row: dict, event_uri: str = "") -> None:
        obs = row.get("observation")
        self.conn.execute(
            "INSERT INTO span_events (run_id, span_id, sandbox_id, t, ts, action, "
            "status, cost, event_uri, observation, protocol) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(span_id, t) DO UPDATE SET "
            "status=excluded.status, observation=excluded.observation, "
            "event_uri=excluded.event_uri",
            (row.get("run_id") or "", row.get("span_id") or "",
             row.get("sandbox_id"), int(row["t"]), float(row.get("ts") or 0),
             row.get("action") or "", row.get("status") or "",
             int(row.get("cost") or 0), event_uri or "",
             json.dumps(obs, ensure_ascii=False) if obs is not None else None,
             int(row.get("protocol") or 1)))
        self.conn.commit()

    def ingest_from_store(self, store: Any) -> int:
        """Replay object-store events into the analytics table (S3 as WAL)."""
        rows = store.conn.execute(
            "SELECT span_id, t, event_uri FROM span_index ORDER BY span_id, t"
        ).fetchall()
        n = 0
        for rec in rows:
            event = store.read_event(rec["event_uri"])
            self.ingest_row(event, rec["event_uri"])
            n += 1
        return n

    def query_events(self, *, run_id: str | None = None,
                     span_id: str | None = None,
                     action: str | None = None) -> list[dict]:
        sql = ("SELECT run_id, span_id, sandbox_id, t, ts, action, status, cost, "
               "event_uri, observation, protocol FROM span_events WHERE 1=1")
        args: list[Any] = []
        if run_id:
            sql += " AND run_id = ?"
            args.append(run_id)
        if span_id:
            sql += " AND span_id = ?"
            args.append(span_id)
        if action:
            sql += " AND action = ?"
            args.append(action)
        sql += " ORDER BY run_id, span_id, t"
        out = []
        for rec in self.conn.execute(sql, args).fetchall():
            row = {k: rec[k] for k in rec.keys()}
            if row["observation"] is not None:
                row["observation"] = json.loads(row["observation"])
            out.append(row)
        return out

    def get_event(self, span_id: str, t: int) -> dict:
        rec = self.conn.execute(
            "SELECT run_id, span_id, sandbox_id, t, ts, action, status, cost, "
            "event_uri, observation, protocol FROM span_events "
            "WHERE span_id = ? AND t = ?",
            (span_id, int(t))).fetchone()
        if rec is None:
            raise TraceError("SpanNotFound", f"no analytics row {span_id} t={t}")
        row = {k: rec[k] for k in rec.keys()}
        if row["observation"] is not None:
            row["observation"] = json.loads(row["observation"])
        return row
