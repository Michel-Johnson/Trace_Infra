"""Register existing immutable run bytes and index them without changing old rows."""

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from trace_hunter.database import Conflict
from trace_hunter.storage import Store
from trace_hunter.traces.index import TraceIndex


def backfill(store, *, project_id="default", after="", batch_size=100, limit=None):
    if type(batch_size) is not int or not 1 <= batch_size <= 1000:
        raise ValueError("batch_size must be between 1 and 1000")
    if limit is not None and (type(limit) is not int or limit < 1):
        raise ValueError("limit must be positive")
    index = TraceIndex(store.revisions)
    created = indexed = 0
    cursor = after
    while limit is None or indexed < limit:
        amount = batch_size if limit is None else min(batch_size, limit - indexed)
        rows = store.repository.rows(
            "SELECT id,payload,digest FROM runs WHERE id > :after ORDER BY id LIMIT :limit",
            {"after": cursor, "limit": amount})
        if not rows:
            break
        for row in rows:
            raw = row["payload"].encode("utf-8")
            if hashlib.sha256(raw).hexdigest() != row["digest"]:
                raise Conflict("Stored legacy payload does not match its digest; backfill stopped")
            result = store.revisions.append(project_id, raw, request_key="legacy:" + row["id"],
                                             derivation="legacy_import")
            status = index.project(project_id, row["id"], result["revision"]["revision"])
            if status["state"] != "complete":
                return {"state": "failed", "created": created + int(result["created"]),
                        "indexed": indexed, "next_after": cursor, "failed_run_id": row["id"],
                        "error_code": status["error_code"]}
            created += int(result["created"])
            indexed += 1
            cursor = row["id"]
    return {"state": "complete", "created": created, "indexed": indexed, "next_after": cursor}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-id", default="default")
    parser.add_argument("--after", default="")
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()
    location = os.environ.get("DATABASE_URL")
    if not location:
        parser.error("Set DATABASE_URL; do not pass credentials on the command line")
    store = Store(location)
    try:
        result = backfill(store, project_id=args.project_id, after=args.after,
                          batch_size=args.batch_size, limit=args.limit)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result["state"] == "complete" else 1
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
