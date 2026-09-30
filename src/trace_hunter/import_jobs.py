"""Transient progress for real adapter imports; trace data stays in core storage."""

import copy
import base64
import binascii
import hashlib
import io
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock

from .database import Conflict
from .interop.common import IngestError
from .interop.service import convert, encoded


STAGES = (
    ("source_read", "读取原始文件"),
    ("adapter_convert", "Adapter 转换为 v2"),
    ("schema_validate", "Schema 与来源校验"),
    ("immutable_store", "保存不可变原文和 v2 Trace"),
    ("object_projection", "切分 Trace 对象"),
    ("trigram_ready", "建立可搜索文本投影"),
)
MAX_JOBS = 200
MAX_BATCHES = 20


def _now():
    return datetime.now(timezone.utc).isoformat()


class ImportJobs:
    """Keep only operational progress in memory; imported bytes are content-addressed."""

    def __init__(self, store, index, tasks=None, on_project_change=None):
        self.store = store
        self.index = index
        self.tasks = tasks
        self.on_project_change = on_project_change
        self._lock = RLock()
        self._jobs = {}
        self._batches = {}
        self._batch_root = Path(tasks.root) / "import-batches" if tasks is not None else None
        if self._batch_root is not None:
            self._batch_root.mkdir(parents=True, exist_ok=True, mode=0o700)
            self._load_batches()

    def _batch_path(self, project_id, batch_id):
        digest = hashlib.sha256((project_id + "\0" + batch_id).encode()).hexdigest()
        return self._batch_root / digest

    @staticmethod
    def _write_json(path, value):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, temporary = tempfile.mkstemp(prefix=".snapshot-", dir=path.parent)
        try:
            with os.fdopen(descriptor, "w") as output:
                json.dump(value, output, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False)
                output.flush(); os.fsync(output.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _persist_batch_locked(self, batch, item=None):
        if self._batch_root is None:
            return
        directory = self._batch_path(batch["project_id"], batch["batch_id"])
        metadata = {key: batch[key] for key in (
            "batch_id", "project_id", "total", "created_at", "started_at", "completed_at")}
        self._write_json(directory / "batch.json", {"version": 1, **metadata})
        if item is not None:
            self._write_json(directory / "items" / (item["job_id"] + ".json"), item)

    def _load_batches(self):
        restored = []
        for metadata_path in self._batch_root.glob("*/batch.json"):
            try:
                metadata = json.loads(metadata_path.read_text())
                items = {}
                for item_path in (metadata_path.parent / "items").glob("*.json"):
                    item = json.loads(item_path.read_text())
                    if isinstance(item, dict) and isinstance(item.get("job_id"), str):
                        items[item["job_id"]] = item
                if not all(isinstance(metadata.get(key), str) for key in (
                        "batch_id", "project_id", "created_at")):
                    continue
                if type(metadata.get("total")) is not int or metadata["total"] < 1:
                    continue
                restored.append({key: metadata.get(key) for key in (
                    "batch_id", "project_id", "total", "created_at", "started_at", "completed_at")} |
                    {"items": items})
            except (OSError, json.JSONDecodeError):
                continue
        restored.sort(key=lambda value: value["created_at"], reverse=True)
        for batch in restored[:MAX_BATCHES]:
            self._batches[(batch["project_id"], batch["batch_id"])] = batch

    @staticmethod
    def _identity(project_id, request_key):
        return "import-" + hashlib.sha256(
            (project_id + "\0" + request_key).encode()
        ).hexdigest()[:24]

    def create(self, project_id, raw, *, request_key, source_format, binding,
               expected_previous=0, batch_id=None, batch_total=None,
               source_name=None):
        if (batch_id is None) != (batch_total is None):
            raise ValueError("batch_id and batch_total must be provided together")
        if batch_total is not None and (type(batch_total) is not int or not 1 <= batch_total <= 10000):
            raise ValueError("batch_total must be between 1 and 10000")
        fingerprint = hashlib.sha256(json.dumps({
            "project_id": project_id,
            "request_key": request_key,
            "source_format": source_format,
            "binding": binding,
            "expected_previous": expected_previous,
            "source_sha256": hashlib.sha256(raw).hexdigest(),
            "batch_id": batch_id,
            "batch_total": batch_total,
            "source_name": source_name,
        }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        job_id = self._identity(project_id, request_key)
        with self._lock:
            previous = self._jobs.get((project_id, job_id))
            if previous:
                if previous["fingerprint"] != fingerprint:
                    raise Conflict("Import idempotency key was already used for different input")
                return self._public(previous), False
            if len(self._jobs) >= MAX_JOBS:
                completed = sorted(
                    (key for key, value in self._jobs.items()
                     if value["state"] in ("succeeded", "failed")),
                    key=lambda key: self._jobs[key]["completed_at"] or "",
                )
                if not completed:
                    raise Conflict("Too many adapter imports are currently running")
                self._jobs.pop(completed[0])
            created_at = _now()
            job = {
                "job_id": job_id,
                "project_id": project_id,
                "state": "queued",
                "source_format": source_format,
                "batch_id": batch_id,
                "source_name": source_name,
                "fingerprint": fingerprint,
                "created_at": created_at,
                "started_at": None,
                "completed_at": None,
                "updated_at": created_at,
                "steps": [
                    {"id": stage_id, "label": label,
                     "state": "completed" if stage_id == "source_read" else "pending",
                     "started_at": created_at if stage_id == "source_read" else None,
                     "completed_at": created_at if stage_id == "source_read" else None,
                     "details": {"bytes": len(raw)} if stage_id == "source_read" else {}}
                    for stage_id, label in STAGES
                ],
                "result": None,
                "error": None,
            }
            if batch_id is not None:
                self._register_batch_locked(project_id, batch_id, batch_total, job)
            self._jobs[(project_id, job_id)] = job
            self._publish_locked(job)
            return self._public(job), True

    def _register_batch_locked(self, project_id, batch_id, total, job):
        key = (project_id, batch_id)
        batch = self._batches.get(key)
        if batch is None:
            if len(self._batches) >= MAX_BATCHES:
                completed = sorted(
                    (item for item in self._batches if self._batch_state_locked(self._batches[item]) == "completed"),
                    key=lambda item: self._batches[item]["created_at"],
                )
                if not completed:
                    raise Conflict("Too many adapter import batches are currently tracked")
                self._batches.pop(completed[0])
            batch = {"batch_id": batch_id, "project_id": project_id, "total": total,
                     "created_at": _now(), "started_at": None, "completed_at": None,
                     "items": {}}
            self._batches[key] = batch
        elif batch["total"] != total:
            raise Conflict("Import batch total changed")
        if job["job_id"] not in batch["items"] and len(batch["items"]) >= total:
            raise Conflict("Import batch already contains its declared number of items")
        item = self._batch_item(job)
        batch["items"][job["job_id"]] = item
        self._persist_batch_locked(batch, item)

    @staticmethod
    def _batch_item(job):
        active = next((step["id"] for step in job["steps"] if step["state"] == "running"), None)
        return {"job_id": job["job_id"], "source_name": job.get("source_name"),
                "state": job["state"], "active_stage": active,
                "created_at": job["created_at"], "started_at": job["started_at"],
                "completed_at": job["completed_at"], "steps": copy.deepcopy(job["steps"]),
                "error": copy.deepcopy(job["error"])}

    @staticmethod
    def _batch_state_locked(batch):
        states = [item["state"] for item in batch["items"].values()]
        if len(states) == batch["total"] and all(state in ("succeeded", "failed") for state in states):
            return "completed"
        return "running" if states else "queued"

    def _sync_batch_locked(self, job):
        batch_id = job.get("batch_id")
        if batch_id is None:
            return
        batch = self._batches[(job["project_id"], batch_id)]
        batch["items"][job["job_id"]] = self._batch_item(job)
        if job["started_at"] and batch["started_at"] is None:
            batch["started_at"] = job["started_at"]
        if self._batch_state_locked(batch) == "completed" and batch["completed_at"] is None:
            batch["completed_at"] = _now()
        self._persist_batch_locked(batch, batch["items"][job["job_id"]])

    def get_batch(self, project_id, batch_id, limit=100):
        return self.get_batch_page(project_id, batch_id, limit=limit)

    @staticmethod
    def _batch_cursor(item):
        return base64.urlsafe_b64encode(json.dumps(
            [item["created_at"], item["job_id"]], separators=(",", ":")).encode()).decode()

    @staticmethod
    def _decode_batch_cursor(value):
        try:
            parsed = json.loads(base64.b64decode(value, altchars=b"-_", validate=True))
        except (ValueError, TypeError, UnicodeError, binascii.Error, json.JSONDecodeError):
            raise ValueError("Invalid import batch cursor") from None
        if not isinstance(parsed, list) or len(parsed) != 2 or not all(isinstance(item, str) for item in parsed):
            raise ValueError("Invalid import batch cursor")
        return tuple(parsed)

    def get_batch_page(self, project_id, batch_id, limit=100, after=None):
        with self._lock:
            try:
                batch = self._batches[(project_id, batch_id)]
            except KeyError:
                raise KeyError("Import batch not found") from None
            items = list(batch["items"].values())
            counts = {state: sum(item["state"] == state for item in items)
                      for state in ("queued", "running", "succeeded", "failed")}
            counts["queued"] += batch["total"] - len(items)
            finished = counts["succeeded"] + counts["failed"]
            end = batch["completed_at"]
            started = batch["started_at"]
            elapsed = 0.0
            if started:
                elapsed = (datetime.fromisoformat(end or _now()) - datetime.fromisoformat(started)).total_seconds()
            throughput = finished / elapsed if elapsed > 0 else 0.0
            remaining = batch["total"] - finished
            ordered = sorted(items, key=lambda item: (item["created_at"], item["job_id"]))
            if after:
                boundary = self._decode_batch_cursor(after)
                ordered = [item for item in ordered if (item["created_at"], item["job_id"]) > boundary]
            selected = ordered[:limit]
            more = len(ordered) > len(selected)
            result = {key: copy.deepcopy(batch[key]) for key in (
                "batch_id", "project_id", "total", "created_at", "started_at", "completed_at")}
            result.update({"state": self._batch_state_locked(batch), "registered": len(items),
                           "completed": finished, "counts": counts,
                           "progress": finished / batch["total"], "throughput": throughput,
                           "eta_seconds": remaining / throughput if throughput > 0 else None,
                           "items": copy.deepcopy(selected), "items_truncated": more,
                           "next_cursor": self._batch_cursor(selected[-1]) if more and selected else None})
            return result

    def get(self, project_id, job_id):
        with self._lock:
            try:
                return self._public(self._jobs[(project_id, job_id)])
            except KeyError:
                if self.tasks is None:
                    raise KeyError("Import job not found") from None
                try:
                    task = self.tasks.get(project_id, job_id)
                except KeyError:
                    raise KeyError("Import job not found") from None
                if task.get("source", {}).get("type") != "adapter_import":
                    raise KeyError("Import job not found") from None
                return {"job_id": task["task_id"], "task_id": task["task_id"],
                        "project_id": task["project_id"], "state": task["state"],
                        "source_format": task["source"].get("source_format", "unknown"),
                        "batch_id": task["source"].get("batch_id"), "source_name": task.get("title"),
                        "created_at": task["created_at"], "started_at": task["started_at"],
                        "completed_at": task["completed_at"], "updated_at": task["updated_at"],
                        "progress": task["progress"], "active_stage": task["current_stage"],
                        "steps": task["steps"], "result": task.get("result"), "error": task.get("error")}

    @staticmethod
    def _public(job):
        value = copy.deepcopy({key: value for key, value in job.items() if key != "fingerprint"})
        value["task_id"] = value["job_id"]
        completed = sum(step["state"] in ("completed", "failed") for step in value["steps"])
        value["progress"] = completed / len(value["steps"])
        value["active_stage"] = next((step["id"] for step in value["steps"] if step["state"] == "running"), None)
        return value

    def _publish_locked(self, job):
        if self.tasks is None:
            return
        public = self._public(job)
        state = "cancelled" if job["state"] == "cancelled" else job["state"]
        result = public.get("result")
        task_result = None
        artifacts = []
        if result:
            revision = result.get("revision", {})
            artifacts.append({"kind": "trace", "name": revision.get("run_id", "Trace"),
                              "ref": {"run_id": revision.get("run_id"), "revision": revision.get("revision")}})
            task_result = {"job_id": job["job_id"], "run_id": revision.get("run_id"),
                           "revision": revision.get("revision"),
                           "content_digest": (revision.get("content") or {}).get("digest"),
                           "index_state": (result.get("index") or {}).get("state"),
                           "import_result": copy.deepcopy(result)}
        self.tasks.publish({
            "task_id": job["job_id"], "project_id": job["project_id"], "kind": "adapter_import",
            "title": job.get("source_name") or f"导入 {job['source_format']}", "state": state,
            "current_stage": public["active_stage"], "progress": public["progress"],
            "processed": sum(step["state"] == "completed" for step in job["steps"]),
            "total": len(job["steps"]), "created_at": job["created_at"], "started_at": job["started_at"],
            "completed_at": job["completed_at"], "steps": public["steps"], "events": [],
            "artifacts": artifacts, "result": task_result, "error": public.get("error"),
            "source": {"type": "adapter_import", "job_id": job["job_id"],
                       "batch_id": job.get("batch_id"), "source_format": job["source_format"]},
        })

    def _update(self, project_id, job_id, **changes):
        with self._lock:
            job = self._jobs[(project_id, job_id)]
            job.update(changes); job["updated_at"] = _now()
            self._sync_batch_locked(job)
            self._publish_locked(job)

    def _stage(self, project_id, job_id, stage_id, state, details=None):
        with self._lock:
            job = self._jobs[(project_id, job_id)]
            stage = next(item for item in job["steps"] if item["id"] == stage_id)
            timestamp = _now()
            stage["state"] = state
            if state == "running":
                stage["started_at"] = timestamp
            if state in ("completed", "failed"):
                stage["started_at"] = stage["started_at"] or timestamp
                stage["completed_at"] = timestamp
            if details is not None:
                stage["details"] = details
            job["updated_at"] = timestamp
            self._sync_batch_locked(job)
            self._publish_locked(job)

    def cancel(self, project_id, job_id):
        with self._lock:
            job = self._jobs.get((project_id, job_id))
            if job is None:
                raise KeyError("Import job not found")
            if job["state"] in ("succeeded", "failed", "cancelled"):
                return self._public(job)
            if job.get("batch_id") is not None:
                raise Conflict("Batch imports cannot be cancelled individually")
            if job["state"] != "queued":
                raise Conflict("Running imports cannot be cancelled safely")
            timestamp = _now(); job.update(state="cancelled", completed_at=timestamp, updated_at=timestamp)
            self._sync_batch_locked(job); self._publish_locked(job)
            return self._public(job)

    def run(self, project_id, job_id, raw, *, request_key, source_format,
            binding, expected_previous=0):
        self._update(project_id, job_id, state="running", started_at=_now())
        active = None
        try:
            active = "adapter_convert"
            self._stage(project_id, job_id, active, "running")
            conversion, report = convert(raw, binding, source_format)
            document = conversion.document
            self._stage(project_id, job_id, active, "completed", {
                "schema_version": document["schema_version"],
                "spans": len(document["spans"]),
                "messages": len(document.get("messages", [])),
                "contexts": len(document.get("contexts", [])),
                "issues": len(report["issues"]),
            })

            active = "schema_validate"
            self._stage(project_id, job_id, active, "running")
            self._stage(project_id, job_id, active, "completed", {
                "byte_hash_verified": report["evidence"]["byte_hash_verified"],
                "resolved_source_pointers": report["evidence"]["resolved_source_pointers"],
                "capabilities": report["capabilities"],
            })

            active = "immutable_store"
            self._stage(project_id, job_id, active, "running")
            source_ref = self.store.content.put(io.BytesIO(raw), media_type="application/json")
            trace_raw = encoded(document)
            appended = self.store.revisions.append(
                project_id, trace_raw, request_key="adapter:" + request_key,
                expected_previous=expected_previous, derivation="legacy_import")
            descriptor = appended["revision"]
            self._stage(project_id, job_id, active, "completed", {
                "source": source_ref.as_dict(),
                "trace": descriptor["content"],
                "created": appended["created"],
                "run_id": descriptor["run_id"],
                "revision": descriptor["revision"],
            })

            active = "object_projection"
            self._stage(project_id, job_id, active, "running")
            if appended["created"]:
                status = self.index.project(project_id, descriptor["run_id"], descriptor["revision"])
            else:
                status = self.index.status(project_id, descriptor["run_id"], descriptor["revision"])
                if status["state"] != "complete":
                    status = self.index.project(project_id, descriptor["run_id"], descriptor["revision"])
            if status["state"] != "complete":
                raise RuntimeError("Trace projection failed: " + str(status.get("error_code")))
            self._stage(project_id, job_id, active, "completed", {
                "records": status["counts"]["records"],
                "tools": status["counts"]["tools"],
                "models": status["counts"]["models"],
                "coverage": status["coverage"]["capture"],
            })

            active = "trigram_ready"
            self._stage(project_id, job_id, active, "running")
            document_count = self.store.repository.rows("""
                SELECT count(*) AS count FROM trace_search_documents
                WHERE project_id=:project_id AND run_id=:run_id AND revision=:revision
                  AND projector_version=:projector_version
            """, {"project_id": project_id, "run_id": descriptor["run_id"],
                   "revision": descriptor["revision"],
                   "projector_version": self.index.projector_version})[0]["count"]
            self._stage(project_id, job_id, active, "completed", {
                "search_documents": document_count,
                "backend": "postgresql_pg_trgm" if self.store.repository.postgres else "sqlite_scan_fallback",
            })
            result = {"revision": descriptor, "index": status, "report": report,
                      "source_content": source_ref.as_dict()}
            self._update(project_id, job_id, state="succeeded", result=result,
                         completed_at=_now())
            if self.on_project_change is not None:
                self.on_project_change(project_id)
        except Exception as error:
            if active:
                self._stage(project_id, job_id, active, "failed")
            issues = error.issues if isinstance(error, IngestError) else []
            code = issues[0]["code"] if issues else (
                "IMPORT_CONFLICT" if isinstance(error, Conflict) else "IMPORT_FAILED")
            self._update(project_id, job_id, state="failed", completed_at=_now(),
                         error={"code": code,
                                "message": "输入不能由所选 Adapter 转换" if issues else (
                                    "导入请求与已有幂等键冲突" if isinstance(error, Conflict)
                                    else "导入处理失败，请查看失败阶段并重试"),
                                "issues": issues})
