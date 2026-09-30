"""Filesystem-backed resumable uploads feeding the existing adapter pipeline."""

import hashlib
import io
import json
import os
import shutil
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock

from .database import Conflict
from .content import ContentRef
from .interop.service import ADAPTERS

MAX_UPLOAD_BYTES = 256 * 1024 * 1024
DEFAULT_PART_BYTES = 8 * 1024 * 1024
MAX_PART_BYTES = 16 * 1024 * 1024


class DuplicateUpload(Conflict):
    """The same auto-import request belongs to another browser identity."""

    def __init__(self, imported):
        self.imported = imported
        super().__init__("相同 Trace 已导入，本次未重复导入" if imported else
                         "已有相同文件的上传记录，请核对是否已导入")


def _now():
    return datetime.now(timezone.utc).isoformat()


class Uploads:
    def __init__(self, root, jobs, content=None, auto_imports=None):
        self.root = Path(root) / "uploads"
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.jobs = jobs
        self.content = content or jobs.store.content
        self.auto_imports = auto_imports
        self.lock = RLock()
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="trace-upload")

    def close(self):
        self.pool.shutdown(wait=True, cancel_futures=False)

    @staticmethod
    def identity(project_id, request_key):
        return "upload-" + hashlib.sha256((project_id + "\0" + request_key).encode()).hexdigest()[:24]

    def _directory(self, upload_id):
        if not upload_id.startswith("upload-") or len(upload_id) != 31:
            raise ValueError("Invalid upload_id")
        return self.root / upload_id

    def _manifest(self, upload_id):
        path = self._directory(upload_id) / "manifest.json"
        try:
            return json.loads(path.read_text())
        except FileNotFoundError:
            raise KeyError("Upload not found") from None

    def _write(self, upload_id, value):
        directory = self._directory(upload_id); directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, temporary = tempfile.mkstemp(prefix=".manifest-", dir=directory)
        try:
            with os.fdopen(descriptor, "w") as output:
                json.dump(value, output, ensure_ascii=False, sort_keys=True, separators=(",", ":")); output.flush(); os.fsync(output.fileno())
            os.replace(temporary, directory / "manifest.json")
        finally:
            if os.path.exists(temporary): os.unlink(temporary)

    def create(self, project_id, value, *, owner_id=None):
        if not isinstance(value, dict) or set(value) - {"request_key", "source_format", "size_bytes", "sha256",
                                                        "part_size", "binding", "expected_previous", "source_name",
                                                        "batch_id", "batch_total"}:
            raise ValueError("Unsupported upload options")
        request_key, source_format = value.get("request_key"), value.get("source_format")
        if not isinstance(request_key, str) or not request_key or len(request_key) > 512:
            raise ValueError("request_key is required")
        if source_format != "auto" and source_format not in ADAPTERS:
            raise ValueError("Unsupported adapter format")
        if source_format == "auto" and (self.auto_imports is None or not owner_id):
            raise ValueError("Automatic Agent import is not available")
        if (value.get("batch_id") is None) != (value.get("batch_total") is None):
            raise ValueError("batch_id and batch_total must be provided together")
        size = value.get("size_bytes")
        if type(size) is not int or not 1 <= size <= MAX_UPLOAD_BYTES:
            raise ValueError("size_bytes must be between 1 and 268435456")
        digest = value.get("sha256")
        if not isinstance(digest, str) or len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
            raise ValueError("sha256 must be 64 lowercase hexadecimal characters")
        part_size = value.get("part_size", DEFAULT_PART_BYTES)
        if type(part_size) is not int or not 1024 * 1024 <= part_size <= MAX_PART_BYTES:
            raise ValueError("part_size must be between 1 MiB and 16 MiB")
        upload_id = self.identity(project_id, request_key)
        def fingerprint_for(identity):
            return hashlib.sha256(json.dumps({**value, "owner_id": identity},
                                             sort_keys=True, separators=(",", ":")).encode()).hexdigest()

        fingerprint = fingerprint_for(owner_id if source_format == "auto" else None)
        with self.lock:
            try:
                previous = self._manifest(upload_id)
            except KeyError:
                previous = None
            if previous:
                if previous["fingerprint"] != fingerprint or previous["project_id"] != project_id:
                    prior_owner = previous.get("owner_id")
                    if (source_format == "auto" and prior_owner and prior_owner != owner_id and
                            previous["project_id"] == project_id and
                            previous["fingerprint"] == fingerprint_for(prior_owner)):
                        imported = False
                        if previous["state"] == "completed" and previous.get("job_id"):
                            try:
                                task = self.auto_imports.tasks.get(project_id, previous["job_id"])
                                imported = task["state"] == "succeeded"
                            except KeyError:
                                pass
                        raise DuplicateUpload(imported)
                    raise Conflict("Upload request_key was already used for different input")
                return {**self.describe(project_id, upload_id), "reused": True}
            total_parts = (size + part_size - 1) // part_size
            manifest = {"version": 1, "upload_id": upload_id, "project_id": project_id,
                        "request_key": request_key, "fingerprint": fingerprint, "source_format": source_format,
                        "size_bytes": size, "sha256": digest, "part_size": part_size, "total_parts": total_parts,
                        "binding": value.get("binding") or {}, "expected_previous": value.get("expected_previous", 0),
                        "source_name": value.get("source_name"), "batch_id": value.get("batch_id"),
                        "batch_total": value.get("batch_total"), "owner_id": owner_id,
                        "state": "uploading", "parts": {}, "attempt": 1,
                        "agent_session_id": None, "agent_turn_id": None,
                        "job_id": None, "created_at": _now(), "updated_at": _now()}
            self._write(upload_id, manifest)
            return {**self.describe(project_id, upload_id), "reused": False}

    def describe(self, project_id, upload_id):
        with self.lock:
            value = self._manifest(upload_id)
            if value["project_id"] != project_id: raise KeyError("Upload not found")
            native_terminal_id = None
            if value.get("agent_session_id") and value.get("owner_id") and self.auto_imports:
                try:
                    session = self.auto_imports.sessions.get(value["agent_session_id"], value["owner_id"])
                    if (session.get("claude_session_id") == value["agent_session_id"] and
                            self.auto_imports.sessions.is_native_terminal(
                                value["agent_session_id"], value["owner_id"])):
                        native_terminal_id = session["claude_session_id"]
                except KeyError:
                    pass
            received = sorted(int(item) for item in value["parts"])
            missing = [item for item in range(value["total_parts"]) if item not in received]
            return {key: value[key] for key in ("upload_id", "project_id", "source_format", "size_bytes",
                                                "sha256", "part_size", "total_parts", "state", "job_id",
                                                "created_at", "updated_at")} | {
                "agent_session_id": value.get("agent_session_id"),
                "agent_turn_id": value.get("agent_turn_id"),
                "native_terminal_id": native_terminal_id,
                "received_parts": received, "missing_parts": missing,
                "received_bytes": sum(value["parts"][str(item)]["size_bytes"] for item in received),
                "progress": len(received) / value["total_parts"]}

    def put_part(self, project_id, upload_id, position, raw, digest):
        with self.lock:
            value = self._manifest(upload_id)
            if value["project_id"] != project_id: raise KeyError("Upload not found")
            if value["state"] != "uploading": raise Conflict("Upload is no longer accepting parts")
            if type(position) is not int or not 0 <= position < value["total_parts"]:
                raise ValueError("Invalid upload part position")
            expected_size = value["part_size"] if position < value["total_parts"] - 1 else value["size_bytes"] - position * value["part_size"]
            if len(raw) != expected_size: raise ValueError("Upload part size does not match manifest")
            actual = hashlib.sha256(raw).hexdigest()
            if digest != actual: raise ValueError("Upload part SHA-256 does not match")
            previous = value["parts"].get(str(position))
            if previous:
                if previous["sha256"] != actual or previous["size_bytes"] != len(raw):
                    raise Conflict("Upload part was already stored with different bytes")
                return self.describe(project_id, upload_id)
            ref = self.content.put(io.BytesIO(raw), media_type="application/octet-stream")
            value["parts"][str(position)] = {"sha256": actual, "size_bytes": len(raw),
                                               "content": ref.as_dict()}
            value["updated_at"] = _now(); self._write(upload_id, value)
            return self.describe(project_id, upload_id)

    def complete(self, project_id, upload_id):
        with self.lock:
            value = self._manifest(upload_id)
            if value["project_id"] != project_id: raise KeyError("Upload not found")
            if value["state"] == "completed": return self.describe(project_id, upload_id)
            if value["state"] == "ready" and value["source_format"] == "auto":
                ref = ContentRef(**value["content_ref"])
                task, session_id, turn_id = self.auto_imports.start(value, ref)
                value.update(state="completed", job_id=task["task_id"],
                             agent_session_id=session_id, agent_turn_id=turn_id, updated_at=_now())
                self._write(upload_id, value)
                return self.describe(project_id, upload_id)
            if len(value["parts"]) != value["total_parts"]: raise Conflict("Upload has missing parts")
            digest, raw = hashlib.sha256(), bytearray()
            for position in range(value["total_parts"]):
                part = value["parts"][str(position)]
                chunk = self.content.read_bytes(ContentRef(**part["content"]),
                                                max_bytes=value["part_size"])
                digest.update(chunk); raw.extend(chunk)
            if len(raw) != value["size_bytes"] or digest.hexdigest() != value["sha256"]:
                raise ValueError("Completed upload does not match declared size or SHA-256")
            if value["source_format"] == "auto":
                ref = self.content.put(io.BytesIO(raw), media_type="application/octet-stream")
                value.update(state="ready", content_ref=ref.as_dict(), updated_at=_now())
                self._write(upload_id, value)
                return self.complete(project_id, upload_id)
            job, created = self.jobs.create(project_id, bytes(raw), request_key=value["request_key"],
                source_format=value["source_format"], binding=value["binding"],
                expected_previous=value["expected_previous"], source_name=value["source_name"],
                batch_id=value.get("batch_id"), batch_total=value.get("batch_total"))
            value.update(state="completed", job_id=job["job_id"], updated_at=_now()); self._write(upload_id, value)
            if created:
                self.pool.submit(self.jobs.run, project_id, job["job_id"], bytes(raw),
                                 request_key=value["request_key"], source_format=value["source_format"],
                                 binding=value["binding"], expected_previous=value["expected_previous"])
            return self.describe(project_id, upload_id)

    def retry(self, project_id, upload_id):
        with self.lock:
            value = self._manifest(upload_id)
            if value["project_id"] != project_id: raise KeyError("Upload not found")
            if value["source_format"] != "auto" or value["state"] != "completed":
                raise Conflict("Only completed automatic imports can be retried")
            if value.get("agent_session_id") and value.get("agent_turn_id"):
                session = self.auto_imports.sessions.get(value["agent_session_id"], value["owner_id"])
                previous = next((turn for turn in session["turns"]
                                 if turn["turn_id"] == value["agent_turn_id"]), None)
                if previous and previous["state"] in ("queued", "running"):
                    raise Conflict("Previous Agent import turn is still active")
            self.auto_imports.tasks.retry(project_id, value["job_id"])
            value.update(state="ready", attempt=value.get("attempt", 1) + 1,
                         agent_session_id=None, agent_turn_id=None, updated_at=_now())
            self._write(upload_id, value)
            return self.complete(project_id, upload_id)

    def cancel(self, project_id, upload_id):
        with self.lock:
            value = self._manifest(upload_id)
            if value["project_id"] != project_id: raise KeyError("Upload not found")
            if value["source_format"] != "auto" or not value.get("job_id"):
                raise Conflict("No automatic Agent import to cancel")
            if value.get("agent_session_id"):
                self.auto_imports.sessions.cancel(value["agent_session_id"], value["owner_id"])
            return self.auto_imports.tasks.cancel(project_id, value["job_id"])

    def delete(self, project_id, upload_id):
        with self.lock:
            value = self._manifest(upload_id)
            if value["project_id"] != project_id: raise KeyError("Upload not found")
            if value["state"] == "completed": raise Conflict("Completed uploads cannot be deleted")
            shutil.rmtree(self._directory(upload_id))
        return {"upload_id": upload_id, "deleted": True}
