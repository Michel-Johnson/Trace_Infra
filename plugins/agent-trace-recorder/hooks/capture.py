#!/usr/bin/env python3
"""Append one Claude hook input to the worker-owned local journal.

Never print hook output or contact a network service: recording cannot alter the
Agent's tool decision. The worker archives this journal after Claude exits.
"""

import fcntl
import json
import os
import sys
import time
from datetime import datetime, timezone

MAX_HOOK_BYTES = 16 * 1024 * 1024


def main():
    target = os.environ.get("TRACE_HUNTER_AGENT_HOOK_PATH")
    if not target:
        return
    raw = sys.stdin.buffer.read(MAX_HOOK_BYTES + 1)
    record = {"at": datetime.now(timezone.utc).isoformat(),
              "monotonic_ns": time.monotonic_ns(), "recorder_version": "0.1.1"}
    if len(raw) > MAX_HOOK_BYTES:
        record.update(state="partial", reason="hook_input_limit", size_at_least=len(raw))
    else:
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeError):
            record.update(state="partial", reason="hook_invalid_json")
        else:
            if not isinstance(value, dict):
                record.update(state="partial", reason="hook_not_object")
            else:
                record.update(state="complete", value=value)
    fd = os.open(target, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "ab") as output:
        fcntl.flock(output, fcntl.LOCK_EX)
        output.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode() + b"\n")
        output.flush()
        fcntl.flock(output, fcntl.LOCK_UN)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass  # Hooks are strictly observational.
