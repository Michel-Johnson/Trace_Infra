"""Phase 1: episode-backend RestoreState / ReplaySpan / Branch / CommitState.

Local directories follow the S3 key layout in docs/spec/phase-0.md. No network.
"""
from __future__ import annotations

import base64
import copy
import json
import os
import pickle
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .engine import Budget, Episode, PROTOCOL
from .tasks import load_task

BACKEND = "episode"
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


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


def new_id() -> str:
    """ULID, standard library only."""
    ms = int(time.time() * 1000)
    entropy = int.from_bytes(os.urandom(10), "big")
    n = (ms << 80) | entropy
    chars = ["0"] * 26
    for i in range(25, -1, -1):
        chars[i] = _CROCKFORD[n & 31]
        n >>= 5
    return "".join(chars)


def _write_json(path: str, obj: Any) -> None:
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2)


def _read_json(path: str) -> Any:
    with open(path) as fh:
        return json.load(fh)


@dataclass
class _Box:
    sandbox_id: str
    run_id: str
    episode: Episode
    span_id: str
    last_state_id: str | None = None
    dirty: bool = False


class LocalStore:
    """Filesystem layout equal to the S3 prefixes in the phase-0 spec."""

    def __init__(self, root: str):
        self.root = os.path.abspath(root)
        os.makedirs(self.root, exist_ok=True)

    def snapshot_uri(self, state_id: str) -> str:
        return os.path.join(self.root, "snapshots", state_id) + os.sep

    def prompt_uri(self, run_id: str, state_id: str) -> str:
        return os.path.join(self.root, "runs", run_id, "prompts", f"{state_id}.json")

    def event_uri(self, run_id: str, span_id: str, t: int) -> str:
        return os.path.join(
            self.root, "runs", run_id, "spans", span_id, "events", f"{t:08d}.json")

    def _idx(self, kind: str, key: str) -> str:
        return os.path.join(self.root, "index", kind, f"{key}.json")

    def put_run(self, rec: dict) -> None:
        _write_json(self._idx("runs", rec["run_id"]), rec)

    def get_run(self, run_id: str) -> dict:
        path = self._idx("runs", run_id)
        if not os.path.isfile(path):
            raise TraceError("StateNotFound", f"no run {run_id}")
        return _read_json(path)

    def put_state(self, rec: dict) -> None:
        _write_json(self._idx("states", rec["state_id"]), rec)

    def get_state(self, state_id: str) -> dict:
        path = self._idx("states", state_id)
        if not os.path.isfile(path):
            raise TraceError("StateNotFound", f"no state {state_id}")
        return _read_json(path)

    def put_span(self, rec: dict) -> None:
        _write_json(self._idx("spans", rec["span_id"]), rec)

    def get_span(self, span_id: str) -> dict:
        path = self._idx("spans", span_id)
        if not os.path.isfile(path):
            raise TraceError("SpanNotFound", f"no span {span_id}")
        return _read_json(path)

    def put_span_index(self, span_id: str, t: int, rec: dict) -> None:
        _write_json(os.path.join(self.root, "index", "span_index", span_id, f"{t:08d}.json"), rec)

    def list_span_index(self, span_id: str) -> list[dict]:
        d = os.path.join(self.root, "index", "span_index", span_id)
        if not os.path.isdir(d):
            return []
        rows = [_read_json(os.path.join(d, name)) for name in sorted(os.listdir(d))]
        rows.sort(key=lambda r: r["t"])
        return rows

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


def _dump_episode(ep: Episode, task_id: str, seed: int) -> dict:
    return {
        "task_id": task_id,
        "seed": seed,
        "t": ep.t,
        "done": ep.done,
        "result": ep.result,
        "budget": ep.budget.snapshot(),
        "task_blob": base64.b64encode(pickle.dumps(ep.task, protocol=4)).decode("ascii"),
        "rows": ep.trajectory,
    }


def _load_episode(blob: dict, *, clock: Callable[[], float]) -> Episode:
    task = pickle.loads(base64.b64decode(blob["task_blob"]))
    ep = Episode(task, clock=clock)
    ep.actions = task.actions()
    ep.t = blob["t"]
    ep.done = blob["done"]
    ep.result = blob["result"]
    ep.budget = Budget(dict(blob["budget"]))
    # Restore exact counters, including non-integers.
    ep.budget.counters = {k: float(v) for k, v in blob["budget"].items()}
    ep._rows = list(blob.get("rows") or [])
    return ep


def _json_canon(obj: Any) -> Any:
    """JSON round-trip so replay compares what was actually stored."""
    if obj is None:
        return None
    return json.loads(json.dumps(obj))


def _clone_episode(src: Episode) -> Episode:
    task = copy.deepcopy(src.task)
    ep = Episode(task, clock=src._clock)
    ep.actions = task.actions()
    ep.t = src.t
    ep.done = src.done
    ep.result = copy.deepcopy(src.result)
    ep.budget = Budget(copy.deepcopy(src.budget.counters))
    ep._rows = copy.deepcopy(src._rows)
    return ep


class Runtime:
    """In-process episode backend for the phase-0 APIs."""

    def __init__(self, root: str, *, clock: Callable[[], float] = time.time):
        self.store = LocalStore(root)
        self._clock = clock
        self._boxes: dict[str, _Box] = {}

    def _box(self, sandbox_id: str) -> _Box:
        box = self._boxes.get(sandbox_id)
        if box is None:
            raise TraceError("SandboxGone", f"no live sandbox {sandbox_id}")
        return box

    def _open_span(self, box: _Box, from_state_id: str) -> None:
        st = self.store.get_state(from_state_id)
        span_id = new_id()
        rec = {
            "span_id": span_id,
            "run_id": box.run_id,
            "from_state_id": from_state_id,
            "to_state_id": None,
            "sandbox_id": box.sandbox_id,
            "t_start": st["t"] + 1,
            "t_end": None,
            "created_at": self._clock(),
        }
        self.store.put_span(rec)
        box.span_id = span_id
        box.episode.span_id = span_id

    def _attach(self, ep: Episode, sandbox_id: str, run_id: str, span_id: str) -> None:
        ep.sandbox_id = sandbox_id
        ep.run_id = run_id
        ep.span_id = span_id
        ep._clock = self._clock

    def start_run(self, task_id: str, seed: int = 0) -> dict:
        """Create a sandbox at t=0, commit the root state, open a span."""
        task = load_task(task_id, seed)
        ep = Episode(task, clock=self._clock)
        run_id = new_id()
        sandbox_id = new_id()
        self.store.put_run({
            "run_id": run_id,
            "task_id": task_id,
            "seed": seed,
            "backend": BACKEND,
            "created_at": self._clock(),
        })
        box = _Box(sandbox_id=sandbox_id, run_id=run_id, episode=ep, span_id="")
        self._boxes[sandbox_id] = box
        self._attach(ep, sandbox_id, run_id, "")
        committed = self.commit_state(sandbox_id)
        return {
            "sandbox_id": sandbox_id,
            "run_id": run_id,
            "state_id": committed["state_id"],
            "t": 0,
            "backend": BACKEND,
            "prompt": None,
        }

    def act(self, sandbox_id: str, name: str, params: dict | None = None) -> dict:
        box = self._box(sandbox_id)
        env = box.episode.act(name, params)
        row = box.episode.trajectory[-1]
        self.store.write_event(box.run_id, box.span_id, row)
        span = self.store.get_span(box.span_id)
        span["t_end"] = row["t"]
        self.store.put_span(span)
        box.dirty = True
        return env

    def commit_state(self, sandbox_id: str, prompt: dict | None = None) -> dict:
        box = self._box(sandbox_id)
        run = self.store.get_run(box.run_id)
        state_id = new_id()
        parent = box.last_state_id
        from_span_id = box.span_id if box.dirty and box.span_id else None
        if from_span_id:
            span = self.store.get_span(from_span_id)
            span["to_state_id"] = state_id
            span["t_end"] = box.episode.t if box.episode.t else None
            self.store.put_span(span)
        prompt_uri = None
        if prompt is not None:
            prompt_uri = self.store.write_prompt(box.run_id, state_id, prompt)
        metadata = {
            "state_id": state_id,
            "run_id": box.run_id,
            "backend": BACKEND,
            "task_id": run["task_id"],
            "seed": run["seed"],
            "t": box.episode.t,
            "protocol": PROTOCOL,
        }
        snapshot_uri = self.store.write_snapshot(
            state_id, metadata, _dump_episode(box.episode, run["task_id"], run["seed"]))
        rec = {
            "state_id": state_id,
            "run_id": box.run_id,
            "parent_state_id": parent,
            "from_span_id": from_span_id,
            "t": box.episode.t,
            "backend": BACKEND,
            "snapshot_uri": snapshot_uri,
            "prompt_uri": prompt_uri,
            "budget": box.episode.budget.snapshot(),
            "created_at": self._clock(),
        }
        self.store.put_state(rec)
        box.last_state_id = state_id
        box.dirty = False
        self._attach(box.episode, box.sandbox_id, box.run_id, "")
        self._open_span(box, state_id)
        self._attach(box.episode, box.sandbox_id, box.run_id, box.span_id)
        return {
            "state_id": state_id,
            "run_id": box.run_id,
            "t": box.episode.t,
            "parent_state_id": parent,
            "snapshot_uri": snapshot_uri,
            "prompt_uri": prompt_uri,
        }

    def restore_state(self, state_id: str) -> dict:
        st = self.store.get_state(state_id)
        run = self.store.get_run(st["run_id"])
        blob = self.store.read_episode(state_id)
        ep = _load_episode(blob, clock=self._clock)
        sandbox_id = new_id()
        box = _Box(
            sandbox_id=sandbox_id,
            run_id=st["run_id"],
            episode=ep,
            span_id="",
            last_state_id=state_id,
            dirty=False,
        )
        self._boxes[sandbox_id] = box
        self._attach(ep, sandbox_id, box.run_id, "")
        self._open_span(box, state_id)
        self._attach(ep, sandbox_id, box.run_id, box.span_id)
        return {
            "sandbox_id": sandbox_id,
            "state_id": state_id,
            "run_id": st["run_id"],
            "t": st["t"],
            "backend": st["backend"],
            "prompt": self.store.read_prompt(st.get("prompt_uri")),
        }

    def replay_span(self, sandbox_id: str, span_id: str, stop_before_t: int) -> dict:
        box = self._box(sandbox_id)
        span = self.store.get_span(span_id)
        index = self.store.list_span_index(span_id)
        if not index:
            t_lo, t_hi = span["t_start"], span["t_start"]
        else:
            t_lo, t_hi = index[0]["t"], index[-1]["t"] + 1
        if stop_before_t < t_lo or stop_before_t > t_hi:
            raise TraceError(
                "StopBeforeTOutOfRange",
                f"stop_before_t={stop_before_t} not in [{t_lo}, {t_hi}]",
                stop_before_t=stop_before_t, t_lo=t_lo, t_hi=t_hi)
        applied = 0
        last_t = box.episode.t
        from_t = span["t_start"]
        for rec in index:
            if rec["t"] >= stop_before_t:
                break
            event = self.store.read_event(rec["event_uri"])
            env = self.act(sandbox_id, event["action"], event.get("params") or {})
            got_obs = _json_canon(env.get("observation"))
            exp_obs = event.get("observation")
            if env["status"] != event["status"] or got_obs != exp_obs:
                raise TraceError(
                    "ReplayDivergence",
                    f"observation mismatch at t={event['t']}",
                    t=event["t"], expected=exp_obs, actual=got_obs,
                    expected_status=event["status"], actual_status=env["status"])
            applied += 1
            last_t = event["t"]
        return {
            "sandbox_id": sandbox_id,
            "span_id": span_id,
            "from_t": from_t,
            "stopped_before_t": stop_before_t,
            "last_t": last_t,
            "events_applied": applied,
        }

    def branch(self, sandbox_id: str, n: int) -> dict:
        box = self._box(sandbox_id)
        if n < 1 or n > 100:
            raise TraceError("BranchFailed", f"n must be 1..100, got {n}")
        parent_state_id = None if box.dirty else box.last_state_id
        children: list[dict] = []
        for i in range(n):
            try:
                child_ep = _clone_episode(box.episode)
                child_id = new_id()
                child = _Box(
                    sandbox_id=child_id,
                    run_id=box.run_id,
                    episode=child_ep,
                    span_id="",
                    last_state_id=box.last_state_id,
                    dirty=box.dirty,
                )
                self._boxes[child_id] = child
                if child.last_state_id and not child.dirty:
                    self._attach(child_ep, child_id, child.run_id, "")
                    self._open_span(child, child.last_state_id)
                else:
                    # Keep recording on a fresh span attached to the last known state,
                    # or to a synthetic open span if there is none.
                    if child.last_state_id:
                        self._attach(child_ep, child_id, child.run_id, "")
                        self._open_span(child, child.last_state_id)
                    else:
                        child.span_id = new_id()
                    self._attach(child_ep, child_id, child.run_id, child.span_id)
                children.append({"sandbox_id": child_id, "index": i})
            except Exception as e:
                children.append({"index": i, "error": "BranchFailed", "message": str(e)})
        return {
            "parent_sandbox_id": sandbox_id,
            "parent_state_id": parent_state_id,
            "children": children,
        }

    def get_span(self, span_id: str) -> dict:
        span = self.store.get_span(span_id)
        events = [self.store.read_event(r["event_uri"])
                  for r in self.store.list_span_index(span_id)]
        return {"span": span, "events": events}

    def episode(self, sandbox_id: str) -> Episode:
        return self._box(sandbox_id).episode
