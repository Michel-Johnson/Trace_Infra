"""Small persistent task registry shared by CLI, agents and the web workbench."""

import copy
import base64
import binascii
import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock

from .database import Conflict


TERMINAL_STATES = {"succeeded", "failed", "cancelled"}
ACTIVE_STATES = {"queued", "running"}
TASK_STATES = ACTIVE_STATES | TERMINAL_STATES
TASK_KINDS = {"adapter_import", "evaluation", "analysis", "custom"}
MAX_TASKS = 1024
MAX_EVENTS = 100


def now():
    return datetime.now(timezone.utc).isoformat()


def _cursor(value):
    return base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode()).decode()


def _decode_cursor(value):
    try:
        parsed = json.loads(base64.b64decode(value, altchars=b"-_", validate=True))
    except (ValueError, TypeError, UnicodeError, binascii.Error, json.JSONDecodeError):
        raise ValueError("Invalid task cursor") from None
    if not isinstance(parsed, list) or len(parsed) != 2 or not all(isinstance(item, str) for item in parsed):
        raise ValueError("Invalid task cursor")
    return tuple(parsed)


class TaskRegistry:
    """Persist operational snapshots beside the immutable content store, not in core tables."""

    def __init__(self, directory):
        self.root = Path(directory)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.root / "tasks.json"
        self._lock = RLock()
        self._tasks = {}
        self._load()

    @staticmethod
    def identity(project_id, request_key):
        return "task-" + hashlib.sha256((project_id + "\0" + request_key).encode()).hexdigest()[:24]

    def _load(self):
        if not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError):
            return
        changed = False
        for task in payload.get("tasks", []):
            if not isinstance(task, dict) or not isinstance(task.get("task_id"), str) or not isinstance(task.get("project_id"), str):
                continue
            if task.get("state") in ACTIVE_STATES:
                timestamp = now()
                task.update(state="failed", completed_at=timestamp, updated_at=timestamp,
                            error={"code": "SERVICE_RESTARTED", "message": "服务重启，原执行进程已结束"},
                            revision=int(task.get("revision", 0)) + 1)
                task.setdefault("events", []).append({"at": timestamp, "type": "failed", "message": "服务重启，任务未继续执行"})
                changed = True
            self._tasks[(task["project_id"], task["task_id"])] = task
        if changed:
            self._persist_locked()

    def _persist_locked(self):
        payload = json.dumps({"version": 1, "tasks": list(self._tasks.values())},
                             ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        descriptor, temporary = tempfile.mkstemp(prefix=".tasks-", dir=self.root)
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(payload); output.flush(); os.fsync(output.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _trim_locked(self):
        if len(self._tasks) < MAX_TASKS:
            return
        completed = sorted((key for key, value in self._tasks.items() if value["state"] in TERMINAL_STATES),
                           key=lambda key: self._tasks[key]["updated_at"])
        if not completed:
            raise Conflict("Too many tasks are currently active")
        self._tasks.pop(completed[0])

    @staticmethod
    def _public(task):
        value = copy.deepcopy({key: value for key, value in task.items() if key != "request_fingerprint"})
        value.get("source", {}).pop("checkpoint", None)
        return value

    @classmethod
    def _summary(cls, task):
        value = cls._public(task)
        value["result"] = None
        value["events"] = value["events"][-1:]
        for step in value["steps"]:
            step["details"] = {}
        return value

    def create(self, project_id, *, request_key, kind, title, steps=None, source=None, total="steps"):
        if kind not in TASK_KINDS:
            raise ValueError("Unsupported task kind")
        task_id = self.identity(project_id, request_key)
        fingerprint = hashlib.sha256(json.dumps({"kind": kind, "title": title, "steps": steps or [], "source": source or {}},
                                                ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        with self._lock:
            previous = self._tasks.get((project_id, task_id))
            if previous:
                if previous.get("request_fingerprint") != fingerprint:
                    raise Conflict("Task idempotency key was already used for different input")
                return self._public(previous), False
            self._trim_locked()
            timestamp = now()
            task = {"task_id": task_id, "project_id": project_id, "kind": kind, "title": title,
                    "state": "queued", "current_stage": None, "progress": 0.0,
                    "processed": 0, "total": (len(steps or []) or None) if total == "steps" else total,
                    "created_at": timestamp, "started_at": None, "updated_at": timestamp,
                    "completed_at": None, "revision": 1,
                    "steps": copy.deepcopy(steps or []), "events": [], "artifacts": [],
                    "result": None, "error": None, "source": copy.deepcopy(source or {}),
                    "request_fingerprint": fingerprint}
            self._tasks[(project_id, task_id)] = task
            self._persist_locked()
            return self._public(task), True

    def publish(self, task):
        """Replace an adapter-import projection with its authoritative current snapshot."""
        with self._lock:
            key = (task["project_id"], task["task_id"])
            previous = self._tasks.get(key)
            if previous is None:
                self._trim_locked()
            value = copy.deepcopy(task)
            value["revision"] = int(previous.get("revision", 0)) + 1 if previous else 1
            value["updated_at"] = now()
            value.setdefault("events", [])
            value.setdefault("artifacts", [])
            value.setdefault("request_fingerprint", value["task_id"])
            self._tasks[key] = value
            self._persist_locked()
            return self._public(value)

    def get(self, project_id, task_id):
        with self._lock:
            try:
                return self._public(self._tasks[(project_id, task_id)])
            except KeyError:
                raise KeyError("Task not found") from None

    def get_checkpoint(self, project_id, task_id):
        with self._lock:
            try:
                return copy.deepcopy(self._tasks[(project_id, task_id)].get("source", {}).get("checkpoint", {}))
            except KeyError:
                raise KeyError("Task not found") from None

    def list(self, project_id, *, states=None, kinds=None, limit=100, after=None):
        with self._lock:
            values = [self._summary(value) for (project, _), value in self._tasks.items()
                      if project == project_id and (not states or value["state"] in states)
                      and (not kinds or value["kind"] in kinds)]
            total = len(values)
        values.sort(key=lambda value: (value["updated_at"], value["task_id"]), reverse=True)
        if after:
            boundary = _decode_cursor(after)
            values = [value for value in values
                      if (value["updated_at"], value["task_id"]) < boundary]
        items = values[:limit]
        more = len(values) > len(items)
        next_cursor = _cursor([items[-1]["updated_at"], items[-1]["task_id"]]) if more and items else None
        return {"items": items, "total": total, "truncated": more, "next_cursor": next_cursor,
                "retention_limit": MAX_TASKS}

    def update(self, project_id, task_id, *, state=None, current_stage=None, progress=None,
               processed=None, total=None, message=None, result=None, error=None, artifacts=None):
        with self._lock:
            try:
                task = self._tasks[(project_id, task_id)]
            except KeyError:
                raise KeyError("Task not found") from None
            if task["source"].get("type") == "adapter_import":
                raise Conflict("Adapter import progress is managed by the import service")
            if task["state"] in TERMINAL_STATES:
                if state in (None, task["state"]):
                    return self._public(task)
                raise Conflict("Terminal task state cannot change")
            if state is not None and state not in TASK_STATES - {"queued"}:
                raise ValueError("Invalid task state transition")
            if progress is not None and (not 0 <= progress <= 1 or progress < task["progress"]):
                raise ValueError("Task progress must be monotonic between 0 and 1")
            next_processed = task["processed"] if processed is None else processed
            next_total = task["total"] if total is None else total
            if next_total is not None and next_processed > next_total:
                raise ValueError("Task processed count cannot exceed total")
            timestamp = now()
            if state == "running" and task["started_at"] is None:
                task["started_at"] = timestamp
            for key, value in (("state", state), ("current_stage", current_stage), ("progress", progress),
                               ("processed", processed), ("total", total), ("result", result),
                               ("error", error), ("artifacts", artifacts)):
                if value is not None:
                    task[key] = copy.deepcopy(value)
            if task["steps"]:
                active = next((index for index, step in enumerate(task["steps"])
                               if step["id"] == task.get("current_stage")), None)
                for index, step in enumerate(task["steps"]):
                    if state == "succeeded" or (active is not None and index < active):
                        step["state"] = "completed"
                    elif active == index:
                        step["state"] = state if state in ("failed", "cancelled") else "completed" if state == "succeeded" else "running"
                    elif step["state"] not in ("completed", "failed"):
                        step["state"] = "pending"
                    if step["state"] == "running":
                        step["started_at"] = step.get("started_at") or timestamp
                    if step["state"] in ("completed", "failed"):
                        step["started_at"] = step.get("started_at") or timestamp
                        step["completed_at"] = step.get("completed_at") or timestamp
            if state in TERMINAL_STATES:
                task["completed_at"] = timestamp
                if state == "succeeded": task["progress"] = 1.0
            if message:
                task["events"].append({"at": timestamp, "type": state or "progress", "message": message[:1000]})
                task["events"] = task["events"][-MAX_EVENTS:]
            task["updated_at"] = timestamp; task["revision"] += 1
            self._persist_locked()
            return self._public(task)

    def cancel(self, project_id, task_id):
        return self.update(project_id, task_id, state="cancelled", message="任务已取消")

    def retry(self, project_id, task_id):
        with self._lock:
            try:
                task = self._tasks[(project_id, task_id)]
            except KeyError:
                raise KeyError("Task not found") from None
            if task["state"] not in ("failed", "cancelled"):
                raise Conflict("Only failed or cancelled tasks can be retried")
            timestamp = now()
            task.update(state="queued", current_stage=None, progress=0.0, processed=0,
                        started_at=None, completed_at=None, updated_at=timestamp,
                        error=None, result=None, artifacts=[])
            for step in task["steps"]:
                step.update(state="pending", started_at=None, completed_at=None, details={})
            task["events"].append({"at": timestamp, "type": "retry", "message": "任务重新排队"})
            task["revision"] += 1
            self._persist_locked()
            return self._public(task)

    def checkpoint(self, project_id, task_id, value):
        with self._lock:
            task = self._tasks[(project_id, task_id)]
            task["source"]["checkpoint"] = copy.deepcopy(value)
            task["updated_at"] = now(); task["revision"] += 1
            self._persist_locked()
            return self._public(task)
