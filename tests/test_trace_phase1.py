"""Phase 1: episode-backend RestoreState / ReplaySpan / Branch / CommitState.

    python3 -m pytest tests/test_trace_phase1.py -q
"""
from __future__ import annotations

import json
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import example_agent                                    # noqa: E402
from ew_examples import Episode, Runtime, TraceError, load_task  # noqa: E402
from ew_examples.engine import PROTOCOL                 # noqa: E402
from ew_examples.tasks import TASKS                     # noqa: E402
from ew_examples.trace import new_id                    # noqa: E402

ALL = sorted(TASKS)
ULID = re.compile(r"^[0123456789ABCDEFGHJKMNPQRSTVWXYZ]{26}$")
SEED = 3
PROMPT = {"system": "trace-phase1", "transcript": [{"role": "user", "content": "go"}]}


def _script(tid, seed=SEED):
    """Record the example agent's (action, params) without going through Runtime."""
    task = load_task(tid, seed)
    ep = Episode(task)
    steps = []
    orig = ep.act

    def wrapped(name, params=None):
        params = dict(params or {})
        steps.append((name, params))
        return orig(name, params)

    ep.act = wrapped
    example_agent.solve(task, ep)
    return steps


def _drive(rt, sandbox_id, steps):
    envs = []
    for name, params in steps:
        envs.append(rt.act(sandbox_id, name, params))
    return envs


def _continuous(tid, seed, steps, stop_after_t):
    ep = Episode(load_task(tid, seed))
    last = None
    for name, params in steps:
        last = ep.act(name, params)
        if ep.t >= stop_after_t:
            break
    return ep, last


def _recorded_run(rt, tid, prefix_len=2):
    """Commit after `prefix_len` acts, then play the rest of the example-agent script."""
    steps = _script(tid)
    assert len(steps) >= prefix_len + 3, (tid, len(steps))
    started = rt.start_run(tid, SEED)
    sid = started["sandbox_id"]
    _drive(rt, sid, steps[:prefix_len])
    mid = rt.commit_state(sid, prompt=PROMPT)
    span_id = rt.episode(sid).span_id
    _drive(rt, sid, steps[prefix_len:])
    return started, sid, steps, mid, span_id


# ---- restore + replay equals a continuous run -----------------------------

@pytest.mark.parametrize("tid", ALL)
def test_restore_then_replay_matches_a_continuous_run(tid, tmp_path):
    """Same task_id+seed: Commit, Restore, Replay to stop_before_t, then one more
    act. Budget, t, and that next observation must match a straight Episode."""
    rt = Runtime(str(tmp_path / tid))
    started, sid, steps, mid, span_id = _recorded_run(rt, tid)
    prefix_len = 2
    stop_before_t = prefix_len + 3          # apply t=3 and t=4; land at t=4
    restored = rt.restore_state(mid["state_id"])
    assert restored["sandbox_id"] != sid
    assert restored["t"] == prefix_len
    assert restored["prompt"] == PROMPT
    assert restored["backend"] == "episode"
    replay = rt.replay_span(restored["sandbox_id"], span_id, stop_before_t)
    assert replay["events_applied"] == 2
    assert replay["last_t"] == stop_before_t - 1
    assert replay["from_t"] == prefix_len + 1
    live = rt.episode(restored["sandbox_id"])
    straight, _ = _continuous(tid, SEED, steps, stop_after_t=stop_before_t - 1)
    assert live.t == straight.t == stop_before_t - 1
    assert live.budget.snapshot() == straight.budget.snapshot()
    nxt_name, nxt_params = steps[stop_before_t - 1]
    got = rt.act(restored["sandbox_id"], nxt_name, nxt_params)
    exp = straight.act(nxt_name, nxt_params)
    assert got["status"] == exp["status"]
    assert got.get("observation") == exp.get("observation")
    assert live.t == straight.t
    assert live.budget.snapshot() == straight.budget.snapshot()


@pytest.mark.parametrize("tid", ALL)
def test_commit_restore_at_t0_then_full_span_replay(tid, tmp_path):
    """Root state is t=0. Replaying the first span up to (not including) the last
    recorded t must land on the same budget as a continuous run of those acts."""
    steps = _script(tid)
    rt = Runtime(str(tmp_path / tid))
    started = rt.start_run(tid, SEED)
    span_id = rt.episode(started["sandbox_id"]).span_id
    _drive(rt, started["sandbox_id"], steps)
    stop_before_t = steps.__len__()          # last recorded t; skip the final act
    assert stop_before_t >= 2
    restored = rt.restore_state(started["state_id"])
    replay = rt.replay_span(restored["sandbox_id"], span_id, stop_before_t)
    assert replay["events_applied"] == stop_before_t - 1
    live = rt.episode(restored["sandbox_id"])
    straight, _ = _continuous(tid, SEED, steps, stop_after_t=stop_before_t - 1)
    assert live.t == straight.t
    assert live.budget.snapshot() == straight.budget.snapshot()


# ---- branch isolation -----------------------------------------------------

def test_branch_n2_children_do_not_share_mutable_task_state(tmp_path):
    """Honeypot sampling advances a cursor. Two children from the same commit
    must each see the first page, not each other's already-consumed ids."""
    rt = Runtime(str(tmp_path))
    started = rt.start_run("corpus_procurement", SEED)
    sid = started["sandbox_id"]
    committed = rt.commit_state(sid)
    out = rt.branch(sid, 2)
    assert out["parent_state_id"] == committed["state_id"]
    assert len(out["children"]) == 2
    c0, c1 = out["children"][0]["sandbox_id"], out["children"][1]["sandbox_id"]
    assert ULID.match(c0) and ULID.match(c1) and c0 != c1 != sid
    params = {"source": "src_03", "n": 3}
    a0 = rt.act(c0, "sample_source", params)
    a1 = rt.act(c1, "sample_source", params)
    assert a0["status"] == a1["status"] == "ok"
    assert a0["observation"]["items"] == a1["observation"]["items"]
    b0 = rt.act(c0, "sample_source", params)
    assert b0["observation"]["items"] != a0["observation"]["items"]
    b1 = rt.act(c1, "sample_source", params)
    assert b1["observation"]["items"] == b0["observation"]["items"]
    parent = rt.act(sid, "sample_source", params)
    assert parent["observation"]["items"] == a0["observation"]["items"]


def test_branch_n2_budgets_are_independent(tmp_path):
    rt = Runtime(str(tmp_path))
    started = rt.start_run("verify_solutions", SEED)
    rt.commit_state(started["sandbox_id"])
    kids = rt.branch(started["sandbox_id"], 2)["children"]
    c0, c1 = kids[0]["sandbox_id"], kids[1]["sandbox_id"]
    before = rt.episode(c1).budget.snapshot()
    for _ in range(20):
        rt.act(c0, "run_all", {"input": "()"})
    assert rt.episode(c0).budget.snapshot()["test_runs"] == 0
    assert rt.episode(c1).budget.snapshot() == before
    still = rt.act(c1, "run_all", {"input": "()"})
    assert still["status"] == "ok"


# ---- full observation in events ------------------------------------------

def test_span_events_keep_the_full_observation_not_the_summary(tmp_path):
    """Lists longer than five are truncated in obs_summary. Replay needs all of
    them, so the stored event must keep the untruncated observation."""
    rt = Runtime(str(tmp_path))
    started = rt.start_run("corpus_procurement", SEED)
    env = rt.act(started["sandbox_id"], "sample_source",
                 {"source": "src_00", "n": 8})
    items = env["observation"]["items"]
    assert len(items) == 8
    span_id = rt.episode(started["sandbox_id"]).span_id
    got = rt.get_span(span_id)
    row = got["events"][0]
    assert row["protocol"] == PROTOCOL
    assert row["observation"]["items"] == items
    assert len(row["obs_summary"]["items"]) == 6
    assert row["obs_summary"]["items"][-1] == "...3 more"
    assert ULID.match(row["run_id"])
    assert row["span_id"] == span_id
    assert row["sandbox_id"] == started["sandbox_id"]


def test_vanilla_episode_also_writes_full_observation():
    ep = Episode(load_task("corpus_procurement", SEED))
    env = ep.act("sample_source", {"source": "src_00", "n": 8})
    row = ep.trajectory[0]
    assert row["observation"]["items"] == env["observation"]["items"]
    assert "protocol" in row
    assert "run_id" not in row


# ---- store layout and identifiers ----------------------------------------

def test_local_store_uses_the_phase0_key_layout(tmp_path):
    rt = Runtime(str(tmp_path))
    started = rt.start_run("clinical_signal", SEED)
    sid = started["sandbox_id"]
    rt.act(sid, "inspect_study", {})
    committed = rt.commit_state(sid, prompt=PROMPT)
    root = str(tmp_path)
    state_id, run_id = committed["state_id"], started["run_id"]
    assert os.path.isfile(os.path.join(root, "snapshots", state_id, "episode.json"))
    assert os.path.isfile(os.path.join(root, "snapshots", state_id, "metadata.json"))
    assert os.path.isfile(os.path.join(root, "runs", run_id, "prompts",
                                       f"{state_id}.json"))
    span_id = rt.store.get_state(state_id)["from_span_id"]
    assert os.path.isfile(os.path.join(
        root, "runs", run_id, "spans", span_id, "events", "00000001.json"))
    meta = json.load(open(os.path.join(root, "snapshots", state_id, "metadata.json")))
    assert meta["backend"] == "episode" and meta["protocol"] == PROTOCOL
    assert meta["task_id"] == "clinical_signal" and meta["t"] == 1
    assert ULID.match(state_id) and ULID.match(run_id) and ULID.match(sid)
    assert committed["parent_state_id"] == started["state_id"]
    assert json.load(open(os.path.join(root, "runs", run_id, "prompts",
                                       f"{state_id}.json"))) == PROMPT


def test_new_id_is_a_crockford_ulid():
    got = {new_id() for _ in range(20)}
    assert len(got) == 20
    assert all(ULID.match(x) for x in got)


# ---- errors ---------------------------------------------------------------

def test_missing_state_and_span_and_sandbox_raise_trace_error(tmp_path):
    rt = Runtime(str(tmp_path))
    with pytest.raises(TraceError) as e:
        rt.restore_state("0" * 26)
    assert e.value.code == "StateNotFound"
    with pytest.raises(TraceError) as e:
        rt.get_span("0" * 26)
    assert e.value.code == "SpanNotFound"
    with pytest.raises(TraceError) as e:
        rt.act("0" * 26, "inspect_study", {})
    assert e.value.code == "SandboxGone"


def test_stop_before_t_out_of_range(tmp_path):
    rt = Runtime(str(tmp_path))
    started = rt.start_run("verify_solutions", SEED)
    sid = started["sandbox_id"]
    span_id = rt.episode(sid).span_id
    rt.act(sid, "list_candidates", {})
    restored = rt.restore_state(started["state_id"])
    with pytest.raises(TraceError) as e:
        rt.replay_span(restored["sandbox_id"], span_id, stop_before_t=99)
    assert e.value.code == "StopBeforeTOutOfRange"
    assert e.value.extra["t_lo"] == 1
    assert e.value.extra["t_hi"] == 2


def test_replay_divergence_when_a_stored_observation_is_tampered(tmp_path):
    rt = Runtime(str(tmp_path))
    started = rt.start_run("verify_solutions", SEED)
    sid = started["sandbox_id"]
    span_id = rt.episode(sid).span_id
    rt.act(sid, "list_candidates", {})
    recs = rt.store.list_span_index(span_id)
    event = rt.store.read_event(recs[0]["event_uri"])
    event["observation"] = {"candidates": ["tampered"]}
    rt.store.write_event(started["run_id"], span_id, event)
    restored = rt.restore_state(started["state_id"])
    with pytest.raises(TraceError) as e:
        rt.replay_span(restored["sandbox_id"], span_id, stop_before_t=2)
    assert e.value.code == "ReplayDivergence"
    assert e.value.extra["t"] == 1


def test_restore_does_not_reuse_the_original_sandbox_id(tmp_path):
    rt = Runtime(str(tmp_path))
    started = rt.start_run("corpus_dedup", SEED)
    again = rt.restore_state(started["state_id"])
    assert again["sandbox_id"] != started["sandbox_id"]
    rt.act(started["sandbox_id"], "inspect_corpus", {})
    rt.act(again["sandbox_id"], "inspect_corpus", {})
    assert rt.episode(started["sandbox_id"]).t == 1
    assert rt.episode(again["sandbox_id"]).t == 1


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
