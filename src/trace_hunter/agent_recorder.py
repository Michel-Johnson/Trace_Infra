"""Turn-scoped Claude source manifest helpers."""

import hashlib
import json
from pathlib import Path


class StreamCapture:
    """Bound stream-json archival while keeping a durable partial marker."""

    def __init__(self, *, limit_bytes=32 * 1024 * 1024):
        self.limit_bytes = limit_bytes
        self.used_bytes = 0
        self.records = []
        self.dropped = 0

    def append(self, output, observed):
        encoded = json.dumps(observed, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"
        # Reserve enough room to note truncation even if the worker restarts.
        if self.used_bytes + len(encoded) > self.limit_bytes - 4096:
            self.dropped += 1
            if self.dropped == 1:
                marker = b'{"state":"partial","reason":"stream_budget"}\n'
                output.write(marker)
                output.flush()
                self.used_bytes += len(marker)
            return False
        output.write(encoded)
        output.flush()
        self.used_bytes += len(encoded)
        self.records.append(observed)
        return True

    def coverage(self):
        return {"state": "partial" if self.dropped else "complete",
                "saved": len(self.records), "dropped": self.dropped,
                "byte_limit": self.limit_bytes}


def read_jsonl(path, *, max_bytes=32 * 1024 * 1024):
    path = Path(path)
    if not path.exists():
        return [], {"state": "missing", "reason": "no_events"}
    if path.stat().st_size > max_bytes:
        return [], {"state": "partial", "reason": "journal_limit"}
    values, bad = [], 0
    for line in path.read_bytes().splitlines():
        try:
            value = json.loads(line)
        except (ValueError, UnicodeError):
            bad += 1
            continue
        if isinstance(value, dict):
            values.append(value)
        else:
            bad += 1
    partial = bad or any(item.get("state") == "partial" for item in values)
    return values, {"state": "partial" if partial else "complete",
                    "count": len(values), "invalid_lines": bad}


def body_files(directory):
    """Return only regular JSON files directly under this turn's private directory."""
    root = Path(directory).resolve()
    if not root.is_dir():
        return []
    return [path for path in sorted(root.iterdir())
            if path.is_file() and not path.is_symlink() and
            path.name.endswith((".request.json", ".response.json")) and
            path.resolve().parent == root]


def body_source_id(path):
    return "body-" + hashlib.sha256(path.name.encode()).hexdigest()[:32]


def body_index(directory):
    values, coverage = read_jsonl(Path(directory) / "index.jsonl", max_bytes=16 * 1024 * 1024)
    root = Path(directory).resolve()
    indexed = []
    for item in values:
        if not isinstance(item, dict):
            continue
        parsed = {key: item.get(key) for key in
                  ("timestamp", "session_id", "query_source", "model", "request_id",
                   "message_id", "message_uuid")}
        for key in ("request_file", "response_file"):
            name = item.get(key)
            if not isinstance(name, str):
                continue
            supplied = Path(name)
            path = (supplied if supplied.is_absolute() else root / supplied).resolve()
            if path.parent == root and path.is_file() and not path.is_symlink():
                parsed[key.replace("_file", "_source_id")] = body_source_id(path)
        indexed.append(parsed)
    return indexed, coverage
