#!/usr/bin/env python3
"""Fail-open Claude hook journal, scoped to one worker turn.

The hook never emits instructions back to Claude. The worker archives the
journal with the stream after the CLI exits, following the Stop-hook pattern
used by open-source Claude observability integrations.
"""

import fcntl
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


def main():
    target = os.environ.get("TRACE_HUNTER_AGENT_HOOK_PATH")
    if not target:
        return
    raw = sys.stdin.buffer.read(4 * 1024 * 1024 + 1)
    if len(raw) > 4 * 1024 * 1024:
        return
    value = json.loads(raw)
    if not isinstance(value, dict):
        return
    record = {"at": datetime.now(timezone.utc).isoformat(), "value": value}
    path = Path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "ab") as output:
        fcntl.flock(output, fcntl.LOCK_EX)
        output.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode() + b"\n")
        output.flush()
        fcntl.flock(output, fcntl.LOCK_UN)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Observability must not change the Agent's execution decision.
        pass
