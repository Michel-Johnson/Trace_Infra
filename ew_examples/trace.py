"""Episode-backend RestoreState / ReplaySpan / Branch / CommitState.

Index is SQL (sqlite locally, Postgres schema in docs/spec/schema.sql).
Blobs follow the S3 key layout in docs/spec/phase-0.md. No network.
"""
from __future__ import annotations

import base64
import copy
import json
import os
import pickle
import shutil
import time
from dataclasses import dataclass
from typing import Any, Callable

from .analytics import ClickHouse
from .engine import Budget, Episode, PROTOCOL
from .fc import BACKEND as FC_BACKEND, FirecrackerConfig, FirecrackerVM
from .forkd import ParentImage, freeze_disk, place_child
from .layers import LayeredDisk, ensure_base
from .store import LocalStore, TraceError
from .tasks import load_task
from .uffd import DirtyMemfile

BACKEND = "episode"
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


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


@dataclass
class _Box:
    sandbox_id: str
    run_id: str
    episode: Episode | None = None
    span_id: str = ""
    last_state_id: str | None = None
    dirty: bool = False
    backend: str = BACKEND
    t: int = 0
    vm: FirecrackerVM | None = None
    disk: LayeredDisk | None = None


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

    def __init__(self, root: str, *, clock: Callable[[], float] = time.time,
                 firecracker: FirecrackerConfig | None = None,
                 objects: str | None = None):
        self.store = LocalStore(root, objects=objects)
        self.analytics = ClickHouse(os.path.join(self.store.root, "clickhouse.sqlite"))
        self.store.on_event = self.analytics.ingest_row
        self._clock = clock
        self._boxes: dict[str, _Box] = {}
        self.firecracker = firecracker

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
        if box.episode is not None:
            box.episode.span_id = span_id

    def _attach(self, ep: Episode, sandbox_id: str, run_id: str, span_id: str) -> None:
        ep.sandbox_id = sandbox_id
        ep.run_id = run_id
        ep.span_id = span_id
        ep._clock = self._clock

    def start_run(self, task_id: str, seed: int = 0, *,
                  backend: str = BACKEND) -> dict:
        """Create a sandbox at t=0, commit the root state, open a span."""
        if backend == FC_BACKEND:
            return self._fc_start(task_id, seed)
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
        if box.backend == FC_BACKEND:
            return self._fc_act(box, name, params)
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
        if box.backend == FC_BACKEND:
            return self._fc_commit(box, prompt)
        run = self.store.get_run(box.run_id)
        state_id = new_id()
        parent = box.last_state_id
        from_span_id = box.span_id if box.dirty and box.span_id else None
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
        # State row first so spans.to_state_id can reference it.
        self.store.put_state(rec)
        if from_span_id:
            span = self.store.get_span(from_span_id)
            span["to_state_id"] = state_id
            span["t_end"] = box.episode.t if box.episode.t else None
            self.store.put_span(span)
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

    def restore_state(self, state_id: str, *, use_uffd: bool = True) -> dict:
        st = self.store.get_state(state_id)
        if st["backend"] == FC_BACKEND:
            return self._fc_restore(st, use_uffd=use_uffd)
        ep = _load_episode(self.store.read_episode(state_id), clock=self._clock)
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

    def restore_and_replay(self, state_id: str, *,
                           span_id: str | None = None,
                           stop_before_t: int | None = None,
                           use_uffd: bool = True) -> dict:
        """Thaw `state_id`. Optionally re-run a recorded span after that.

        RestoreState is the freeze/restore. ReplaySpan is not a substitute:
        it executes the old commands again. Omit `span_id` to only thaw.
        """
        restored = self.restore_state(state_id, use_uffd=use_uffd)
        replay = None
        if span_id is not None:
            if stop_before_t is None:
                span = self.store.get_span(span_id)
                index = self.store.list_span_index(span_id)
                if index:
                    stop_before_t = index[-1]["t"] + 1
                else:
                    stop_before_t = span["t_start"]
            replay = self.replay_span(
                restored["sandbox_id"], span_id, stop_before_t)
        return {
            "sandbox_id": restored["sandbox_id"],
            "state_id": restored["state_id"],
            "run_id": restored["run_id"],
            "t": restored["t"] if replay is None else replay["last_t"],
            "backend": restored["backend"],
            "restore": restored,
            "replay": replay,
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
        last_t = box.t if box.backend == FC_BACKEND else box.episode.t
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
        if box.backend == FC_BACKEND:
            return self._fc_branch(box, n)
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

    def get_state(self, state_id: str) -> dict:
        """Index row plus prompt bytes. Snapshot itself stays in object storage."""
        st = self.store.get_state(state_id)
        out = dict(st)
        out["prompt"] = self.store.read_prompt(st.get("prompt_uri"))
        return out

    def get_span(self, span_id: str) -> dict:
        span = self.store.get_span(span_id)
        events = [self.store.read_event(r["event_uri"])
                  for r in self.store.list_span_index(span_id)]
        return {"span": span, "events": events}

    def episode(self, sandbox_id: str) -> Episode:
        ep = self._box(sandbox_id).episode
        if ep is None:
            raise TraceError("SandboxGone", "this sandbox is firecracker, not episode")
        return ep

    def _record_env(self, box: _Box, name: str, params: dict, status: str,
                    observation: dict | None = None, error: str | None = None,
                    message: str | None = None, cost: int = 0) -> dict:
        box.t += 1
        env: dict[str, Any] = {
            "protocol": PROTOCOL, "status": status, "cost_charged": cost,
            "budget_remaining": {},
        }
        if observation is not None:
            env["observation"] = observation
        if error:
            env["error"] = error
        if message:
            env["message"] = message
        row = {
            "protocol": PROTOCOL, "t": box.t, "ts": round(self._clock(), 2),
            "action": name, "params": params, "status": status, "cost": cost,
            "budget_remaining": {}, "observation": observation,
            "run_id": box.run_id, "span_id": box.span_id,
            "sandbox_id": box.sandbox_id,
        }
        if error:
            row["error"] = error
        if message:
            row["message"] = message
        self.store.write_event(box.run_id, box.span_id, row)
        span = self.store.get_span(box.span_id)
        span["t_end"] = box.t
        self.store.put_span(span)
        box.dirty = True
        return env

    def _fc_start(self, task_id: str, seed: int) -> dict:
        if self.firecracker is None:
            raise TraceError("VmmFailed", "Runtime was not given a FirecrackerConfig")
        run_id = new_id()
        sandbox_id = new_id()
        jail = os.path.join(self.store.live_root, "live", sandbox_id)
        vm = FirecrackerVM(self.firecracker, jail)
        _base_id, base_path = self._shared_base()
        disk = LayeredDisk(base_path, os.path.join(jail, "upper"))
        disk.backing_for_vmm(vm.rootfs_abs)
        vm.boot_from_rootfs(vm.rootfs_abs)
        self.store.put_run({
            "run_id": run_id,
            "task_id": task_id,
            "seed": seed,
            "backend": FC_BACKEND,
            "created_at": self._clock(),
        })
        box = _Box(sandbox_id=sandbox_id, run_id=run_id, backend=FC_BACKEND,
                   vm=vm, t=0, span_id="", disk=disk)
        self._boxes[sandbox_id] = box
        committed = self.commit_state(sandbox_id)
        return {
            "sandbox_id": sandbox_id,
            "run_id": run_id,
            "state_id": committed["state_id"],
            "t": 0,
            "backend": FC_BACKEND,
            "prompt": None,
        }

    def _fc_act(self, box: _Box, name: str, params: dict | None) -> dict:
        params = dict(params or {})
        if box.vm is None:
            raise TraceError("SandboxGone", "vm process is gone")
        if name != "exec":
            return self._record_env(box, name, params, "error",
                                    error="UnknownAction",
                                    message="firecracker backend only has exec")
        cmd = str(params.get("cmd") or params.get("command") or "")
        if not cmd:
            return self._record_env(box, name, params, "error",
                                    error="ActionRefused", message="exec needs cmd")
        try:
            obs = box.vm.exec(cmd)
        except TraceError as e:
            return self._record_env(box, name, params, "error",
                                    error=e.code, message=e.message)
        status = "ok" if obs.get("exit") == 0 else "error"
        extra = {}
        if status == "error":
            extra["error"] = "ExecFailed"
            extra["message"] = f"exit {obs.get('exit')}"
        return self._record_env(box, name, params, status, observation=obs, **extra)

    def _fc_commit(self, box: _Box, prompt: dict | None) -> dict:
        if box.vm is None:
            raise TraceError("SandboxGone", "vm process is gone")
        run = self.store.get_run(box.run_id)
        state_id = new_id()
        parent = box.last_state_id
        from_span_id = box.span_id if box.dirty and box.span_id else None
        prompt_uri = None
        if prompt is not None:
            prompt_uri = self.store.write_prompt(box.run_id, state_id, prompt)
        snap_dir = self.store.fc_dir(state_id)
        snapfile = os.path.join(snap_dir, "snapfile")
        memfile = os.path.join(snap_dir, "memfile")
        box.vm.pause()
        try:
            box.vm.create_snapshot(snapfile, memfile)
        except Exception:
            try:
                box.vm.resume()
            except Exception:
                pass
            raise
        box.vm.resume()
        if box.disk is None:
            base_id, base_path = self._shared_base()
            box.disk = LayeredDisk(base_path, os.path.join(snap_dir, "upper"))
        else:
            box.disk.upper_path = os.path.join(snap_dir, "upper")
        box.disk.capture_from(box.vm.rootfs_abs)
        metadata = {
            "state_id": state_id,
            "run_id": box.run_id,
            "backend": FC_BACKEND,
            "task_id": run["task_id"],
            "seed": run["seed"],
            "t": box.t,
            "protocol": PROTOCOL,
        }
        self.store.write_metadata(state_id, metadata)
        self.store.write_header(state_id, self._fc_header(box, state_id, snap_dir))
        snapshot_uri = self.store.snapshot_uri(state_id)
        rec = {
            "state_id": state_id,
            "run_id": box.run_id,
            "parent_state_id": parent,
            "from_span_id": from_span_id,
            "t": box.t,
            "backend": FC_BACKEND,
            "snapshot_uri": snapshot_uri,
            "prompt_uri": prompt_uri,
            "budget": {},
            "created_at": self._clock(),
        }
        self.store.put_state(rec)
        if from_span_id:
            span = self.store.get_span(from_span_id)
            span["to_state_id"] = state_id
            span["t_end"] = box.t if box.t else None
            self.store.put_span(span)
        box.last_state_id = state_id
        box.dirty = False
        self._open_span(box, state_id)
        return {
            "state_id": state_id,
            "run_id": box.run_id,
            "t": box.t,
            "parent_state_id": parent,
            "snapshot_uri": snapshot_uri,
            "prompt_uri": prompt_uri,
        }

    def _shared_base(self) -> tuple[str, str]:
        assert self.firecracker is not None
        base_id = "rootfs"
        dest = self.store.layer_base_path(base_id)
        ensure_base(self.firecracker.rootfs, dest)
        return base_id, dest

    def _fc_header(self, box: _Box, state_id: str, snap_dir: str) -> dict:
        header = {
            "vcpu_count": box.vm.cfg.vcpu_count if box.vm else 1,
            "mem_size_mib": box.vm.cfg.mem_size_mib if box.vm else 128,
            "agent_port": box.vm.cfg.agent_port if box.vm else 5252,
            "guest_cid": box.vm.guest_cid if box.vm else 0,
        }
        if box.disk is not None:
            base_id, base_path = self._shared_base()
            header["disk"] = box.disk.header_disk(
                base_id=base_id, base_uri=base_path)
            header["disk"]["upper"] = "upper"
        mem_path = os.path.join(snap_dir, "memfile")
        if os.path.isfile(mem_path):
            header["memory"] = DirtyMemfile(mem_path).header_memory()
        return header

    def _fc_restore(self, st: dict, *, use_uffd: bool = True) -> dict:
        if self.firecracker is None:
            raise TraceError("VmmFailed", "Runtime was not given a FirecrackerConfig")
        sandbox_id = new_id()
        jail = os.path.join(self.store.live_root, "live", sandbox_id)
        snap_dir = self.store.fc_dir(st["state_id"])
        vm = FirecrackerVM(self.firecracker, jail)
        header_path = os.path.join(snap_dir, "header")
        header: dict = {}
        if os.path.isfile(header_path):
            with open(header_path) as fh:
                header = json.load(fh)
        disk = None
        rootfs = os.path.join(snap_dir, "rootfs")
        if (header.get("disk") or {}).get("kind") == "overlaybd":
            disk = LayeredDisk.from_snapshot(
                header, snap_dir, self.store.layers_dir())
            disk.backing_for_vmm(vm.rootfs_abs)
            rootfs = vm.rootfs_abs
        elif not os.path.isfile(rootfs):
            raise TraceError("StateNotFound", f"no disk layers for {st['state_id']}")
        vm.load_snapshot(
            os.path.join(snap_dir, "snapfile"),
            os.path.join(snap_dir, "memfile"),
            rootfs,
            use_uffd=use_uffd,
        )
        box = _Box(
            sandbox_id=sandbox_id,
            run_id=st["run_id"],
            backend=FC_BACKEND,
            vm=vm,
            t=st["t"],
            last_state_id=st["state_id"],
            dirty=False,
            span_id="",
            disk=disk,
        )
        self._boxes[sandbox_id] = box
        self._open_span(box, st["state_id"])
        return {
            "sandbox_id": sandbox_id,
            "state_id": st["state_id"],
            "run_id": st["run_id"],
            "t": st["t"],
            "backend": FC_BACKEND,
            "prompt": self.store.read_prompt(st.get("prompt_uri")),
        }

    def _fc_branch(self, box: _Box, n: int) -> dict:
        if box.vm is None:
            raise TraceError(
                "BranchFailed", "forkd needs a live parent vm to BRANCH")
        parent_state_id = None if box.dirty else box.last_state_id
        dest = os.path.join(self.store.live_root, "live", box.sandbox_id, "fork", new_id())
        os.makedirs(dest, exist_ok=True)
        snapfile = os.path.join(dest, "snapfile")
        memfile = os.path.join(dest, "memfile")
        box.vm.pause()
        try:
            box.vm.create_snapshot(snapfile, memfile, pack=False)
        except Exception:
            try:
                box.vm.resume()
            except Exception:
                pass
            raise
        box.vm.resume()
        disk = None
        if box.disk is not None:
            disk = freeze_disk(box.disk, os.path.join(dest, "upper"),
                               box.vm.rootfs_abs)
        image = ParentImage(dir=dest, memfile=memfile, snapfile=snapfile, disk=disk)
        children: list[dict] = []
        for i in range(n):
            try:
                child_id = new_id()
                jail = os.path.join(self.store.live_root, "live", child_id)
                placed = place_child(image, child_id, jail)
                vm = FirecrackerVM(box.vm.cfg, jail)
                rootfs = vm.rootfs_abs
                if placed.disk is not None:
                    placed.disk.backing_for_vmm(rootfs)
                elif os.path.isfile(box.vm.rootfs_abs):
                    shutil.copy2(box.vm.rootfs_abs, rootfs)
                vm.load_snapshot(
                    placed.snapfile or snapfile, placed.memfile, rootfs,
                    share_mem=True)
                child = _Box(
                    sandbox_id=child_id,
                    run_id=box.run_id,
                    backend=FC_BACKEND,
                    vm=vm,
                    disk=placed.disk,
                    t=box.t,
                    last_state_id=box.last_state_id,
                    dirty=box.dirty,
                    span_id="",
                )
                self._boxes[child_id] = child
                if child.last_state_id:
                    self._open_span(child, child.last_state_id)
                children.append({"sandbox_id": child_id, "index": i})
            except Exception as e:
                children.append({"index": i, "error": "BranchFailed", "message": str(e)})
        return {
            "parent_sandbox_id": box.sandbox_id,
            "parent_state_id": parent_state_id,
            "children": children,
        }
