"""Phase 6: ClickHouse analytics, HTTP API, cross-node object store.

    python3 -m pytest tests/test_trace_phase6.py -q
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ew_examples import Runtime, TraceError, serve                   # noqa: E402
from ew_examples.analytics import ClickHouse                         # noqa: E402

SEED = 3
PROMPT = {"system": "phase6", "transcript": [{"role": "user", "content": "go"}]}


def _http(base: str, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        base + path, data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def test_clickhouse_returns_full_observation_not_summary(tmp_path):
    """Acceptance: analytics lookup by run_id has the full observation."""
    rt = Runtime(str(tmp_path))
    started = rt.start_run("corpus_procurement", SEED)
    sid = started["sandbox_id"]
    first = rt.act(sid, "sample_source", {"source": "src_00", "n": 8})
    items = first["observation"]["items"]
    assert len(items) == 8
    rows = rt.analytics.query_events(run_id=started["run_id"])
    assert [r["t"] for r in rows] == [1]
    assert rows[0]["action"] == "sample_source"
    assert rows[0]["observation"]["items"] == items
    assert len(rows[0]["observation"]["items"]) == 8


def test_analytics_reingest_from_object_store(tmp_path):
    rt = Runtime(str(tmp_path))
    started = rt.start_run("verify_solutions", SEED)
    rt.act(started["sandbox_id"], "list_candidates", {})
    ch = ClickHouse(str(tmp_path / "empty-ch.sqlite"))
    n = ch.ingest_from_store(rt.store)
    assert n >= 1
    got = ch.query_events(run_id=started["run_id"])
    assert got[0]["action"] == "list_candidates"
    assert got[0]["observation"] is not None


def test_cross_node_restore_from_shared_objects(tmp_path):
    """Acceptance: a second Runtime sharing objects, not live jails, can Restore."""
    shared = str(tmp_path / "s3")
    node_a = str(tmp_path / "node_a")
    node_b = str(tmp_path / "node_b")
    a = Runtime(node_a, objects=shared)
    started = a.start_run("verify_solutions", SEED)
    sid = started["sandbox_id"]
    a.act(sid, "list_candidates", {})
    committed = a.commit_state(sid, prompt=PROMPT)
    b = Runtime(node_b, objects=shared)
    restored = b.restore_state(committed["state_id"])
    assert restored["sandbox_id"] != sid
    assert restored["t"] == 1
    assert restored["prompt"] == PROMPT
    assert os.path.isfile(os.path.join(
        shared, "snapshots", committed["state_id"], "episode.json"))
    assert os.path.isfile(os.path.join(shared, "index.sqlite"))
    assert not os.path.exists(os.path.join(node_b, "index.sqlite"))
    env = b.act(restored["sandbox_id"], "list_candidates", {})
    assert env["status"] == "ok"
    assert "candidates" in env["observation"]
    with pytest.raises(TraceError) as e:
        b.act(sid, "list_candidates", {})
    assert e.value.code == "SandboxGone"


def test_http_start_act_commit_restore_branch(tmp_path):
    """Acceptance: HTTP walks the experiment path."""
    rt = Runtime(str(tmp_path))
    httpd, base = serve(rt)
    try:
        code, started = _http(base, "POST", "/v1/runs", {
            "task_id": "verify_solutions", "seed": SEED})
        assert code == 201
        sid = started["sandbox_id"]
        code, env = _http(base, "POST", f"/v1/sandboxes/{sid}/act", {
            "name": "list_candidates", "params": {}})
        assert code == 200
        assert env["status"] == "ok"
        code, committed = _http(base, "POST", f"/v1/sandboxes/{sid}/commit", {
            "prompt": PROMPT})
        assert code == 201
        code, restored = _http(base, "POST",
                               f"/v1/states/{committed['state_id']}/restore", {})
        assert code == 201
        assert restored["sandbox_id"] != sid
        code, branched = _http(base, "POST",
                               f"/v1/sandboxes/{restored['sandbox_id']}/branch",
                               {"n": 2})
        assert code == 201
        assert len(branched["children"]) == 2
        code, events = _http(base, "GET",
                             f"/v1/analytics/events?run_id={started['run_id']}")
        assert code == 200
        assert events["events"][0]["action"] == "list_candidates"
        code, missing = _http(base, "GET", "/v1/states/does-not-exist")
        assert code == 404
        assert missing["error"] == "StateNotFound"
    finally:
        httpd.shutdown()


def test_e2b_aliases_create_snapshot_fork(tmp_path):
    rt = Runtime(str(tmp_path))
    httpd, base = serve(rt)
    try:
        code, started = _http(base, "POST", "/sandboxes", {
            "task_id": "verify_solutions", "seed": SEED,
            "templateID": "verify_solutions"})
        assert code == 201
        sid = started["sandbox_id"]
        _http(base, "POST", f"/v1/sandboxes/{sid}/act", {
            "name": "list_candidates", "params": {}})
        code, snap = _http(base, "POST", f"/sandboxes/{sid}/snapshots", {
            "prompt": PROMPT})
        assert code == 201
        assert snap["state_id"]
        code, again = _http(base, "POST", "/sandboxes", {"state_id": snap["state_id"]})
        assert code == 201
        assert again["sandbox_id"] != sid
        code, forked = _http(base, "POST", f"/sandboxes/{again['sandbox_id']}/fork",
                             {"count": 2})
        assert code == 201
        assert len(forked["children"]) == 2
    finally:
        httpd.shutdown()


def test_start_run_still_defaults_to_episode(tmp_path):
    rt = Runtime(str(tmp_path))
    started = rt.start_run("verify_solutions", SEED)
    assert started["backend"] == "episode"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
