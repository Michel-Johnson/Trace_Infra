"""Phase 2: SQL index + object-store events.

    python3 -m pytest tests/test_trace_phase2.py -q
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ew_examples import Runtime, TraceError                         # noqa: E402
from ew_examples.engine import PROTOCOL                             # noqa: E402

SEED = 3
PROMPT = {"system": "trace-phase2", "transcript": [{"role": "user", "content": "go"}]}
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _tables(conn):
    return {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}


def test_get_state_returns_snapshot_uri_and_prompt(tmp_path):
    """Acceptance: lookup by state_id finds the snapshot and the prompt."""
    rt = Runtime(str(tmp_path))
    started = rt.start_run("counter", SEED)
    sid = started["sandbox_id"]
    rt.act(sid, "peek", {})
    committed = rt.commit_state(sid, prompt=PROMPT)
    got = rt.get_state(committed["state_id"])
    assert got["state_id"] == committed["state_id"]
    assert got["parent_state_id"] == started["state_id"]
    assert got["t"] == 1
    assert got["prompt"] == PROMPT
    assert got["prompt_uri"] == committed["prompt_uri"]
    assert got["snapshot_uri"] == committed["snapshot_uri"]
    episode = os.path.join(got["snapshot_uri"], "episode.json")
    meta = os.path.join(got["snapshot_uri"], "metadata.json")
    assert os.path.isfile(episode) and os.path.isfile(meta)
    assert json.load(open(meta))["protocol"] == PROTOCOL
    assert json.load(open(got["prompt_uri"])) == PROMPT
    root_state = rt.get_state(started["state_id"])
    assert root_state["prompt"] is None
    assert root_state["parent_state_id"] is None


def test_get_span_returns_full_actions_in_t_order(tmp_path):
    """Acceptance: a span reads every action in order, not the truncated summary."""
    rt = Runtime(str(tmp_path))
    started = rt.start_run("counter", SEED)
    sid = started["sandbox_id"]
    span_id = rt.episode(sid).span_id
    first = rt.act(sid, "list_items", {})
    rt.act(sid, "inc", {})
    rt.act(sid, "peek", {})
    got = rt.get_span(span_id)
    events = got["events"]
    assert [e["t"] for e in events] == [1, 2, 3]
    assert [e["action"] for e in events] == ["list_items", "inc", "peek"]
    items = first["observation"]["items"]
    assert len(items) == 8
    assert events[0]["observation"]["items"] == items
    assert events[0]["obs_summary"]["items"][-1] == "...3 more"
    assert len(events[0]["obs_summary"]["items"]) == 6


def test_index_is_sql_not_json_files(tmp_path):
    rt = Runtime(str(tmp_path))
    started = rt.start_run("counter", SEED)
    rt.act(started["sandbox_id"], "inc", {})
    db = os.path.join(str(tmp_path), "index.sqlite")
    assert os.path.isfile(db)
    assert not os.path.isdir(os.path.join(str(tmp_path), "index"))
    conn = sqlite3.connect(db)
    assert {"runs", "states", "spans", "span_index"} <= _tables(conn)
    n_states = conn.execute("SELECT COUNT(*) FROM states").fetchone()[0]
    n_spans = conn.execute("SELECT COUNT(*) FROM spans").fetchone()[0]
    n_idx = conn.execute("SELECT COUNT(*) FROM span_index").fetchone()[0]
    assert n_states == 1 and n_spans == 1 and n_idx == 1
    row = conn.execute(
        "SELECT snapshot_uri, prompt_uri, parent_state_id, t FROM states "
        "WHERE state_id = ?", (started["state_id"],)).fetchone()
    assert row[0].endswith(os.sep) and row[1] is None and row[2] is None
    assert row[3] == 0


def test_new_runtime_on_the_same_root_still_finds_state_and_span(tmp_path):
    """Index and blobs survive a new process. sandbox_id does not."""
    root = str(tmp_path)
    rt = Runtime(root)
    started = rt.start_run("counter", SEED)
    sid = started["sandbox_id"]
    span_id = rt.episode(sid).span_id
    rt.act(sid, "inc", {})
    committed = rt.commit_state(sid, prompt=PROMPT)
    gone = started["sandbox_id"]
    del rt
    again = Runtime(root)
    with pytest.raises(TraceError) as e:
        again.act(gone, "inc", {})
    assert e.value.code == "SandboxGone"
    bundle = again.get_state(committed["state_id"])
    assert bundle["prompt"] == PROMPT
    assert bundle["parent_state_id"] == started["state_id"]
    events = again.get_span(span_id)["events"]
    assert [ev["t"] for ev in events] == [1]
    assert events[0]["action"] == "inc"
    restored = again.restore_state(committed["state_id"])
    assert restored["t"] == 1
    assert restored["prompt"] == PROMPT
    env = again.act(restored["sandbox_id"], "peek", {})
    assert env["status"] == "ok"


def test_postgres_schema_file_declares_the_same_tables():
    sql = open(os.path.join(ROOT, "docs", "spec", "schema.sql")).read()
    for table in ("runs", "states", "spans", "span_index"):
        assert f"CREATE TABLE {table}" in sql
    assert "snapshot_uri" in sql and "prompt_uri" in sql
    assert "parent_state_id" in sql
    assert "JSONB" in sql


def test_span_to_state_id_points_at_the_commit_that_closed_it(tmp_path):
    rt = Runtime(str(tmp_path))
    started = rt.start_run("counter", SEED)
    sid = started["sandbox_id"]
    span_id = rt.episode(sid).span_id
    rt.act(sid, "peek", {})
    committed = rt.commit_state(sid, prompt=PROMPT)
    span = rt.get_span(span_id)["span"]
    assert span["from_state_id"] == started["state_id"]
    assert span["to_state_id"] == committed["state_id"]
    assert span["t_start"] == 1 and span["t_end"] == 1


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
