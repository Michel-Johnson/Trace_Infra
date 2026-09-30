"""Explicitly rebuild missing/failed current Span projections without reading on query."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from trace_hunter.storage import Store
from trace_hunter.traces.index import PROJECTOR_VERSION, TraceIndex


def rebuild(store, *, after_project="", after_run="", after_revision=0,
            batch_size=100, limit=None, workers=8):
    if type(batch_size) is not int or not 1 <= batch_size <= 1000:
        raise ValueError("batch_size must be between 1 and 1000")
    if limit is not None and (type(limit) is not int or limit < 1):
        raise ValueError("limit must be positive")
    if type(workers) is not int or not 1 <= workers <= 16:
        raise ValueError("workers must be between 1 and 16")
    if not store.repository.postgres:
        workers = 1
    index = TraceIndex(store.revisions)
    rebuilt = 0
    cursor = {"project_id": after_project, "run_id": after_run, "revision": after_revision}
    while limit is None or rebuilt < limit:
        amount = batch_size if limit is None else min(batch_size, limit - rebuilt)
        rows = store.repository.rows("""
            SELECT r.project_id,r.run_id,r.revision
            FROM trace_revisions r
            WHERE (r.projector_version IS NULL OR r.projector_version!=:projector
                   OR r.projection_state!='complete') AND (
                r.project_id>:after_project OR
                (r.project_id=:after_project AND r.run_id>:after_run) OR
                (r.project_id=:after_project AND r.run_id=:after_run AND r.revision>:after_revision))
            ORDER BY r.project_id,r.run_id,r.revision LIMIT :limit
        """, {"projector": PROJECTOR_VERSION, "after_project": cursor["project_id"],
              "after_run": cursor["run_id"], "after_revision": cursor["revision"], "limit": amount})
        if not rows:
            break
        def project(row):
            return index.project(row["project_id"], row["run_id"], row["revision"])
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="trace-reproject") as pool:
            statuses = list(pool.map(project, rows))
        for row, status in zip(rows, statuses):
            cursor = {key: row[key] for key in ("project_id", "run_id", "revision")}
            if status["state"] != "complete":
                return {"state": "failed", "projector_version": PROJECTOR_VERSION,
                        "rebuilt": rebuilt, "next_after": cursor, "error_code": status["error_code"]}
            rebuilt += 1
    return {"state": "complete", "projector_version": PROJECTOR_VERSION,
            "rebuilt": rebuilt, "next_after": cursor}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--after-project", default="")
    parser.add_argument("--after-run", default="")
    parser.add_argument("--after-revision", type=int, default=0)
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()
    location = os.environ.get("DATABASE_URL")
    if not location:
        parser.error("Set DATABASE_URL; do not pass credentials on the command line")
    store = Store(location)
    try:
        result = rebuild(store, after_project=args.after_project, after_run=args.after_run,
                         after_revision=args.after_revision, batch_size=args.batch_size,
                         limit=args.limit, workers=args.workers)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result["state"] == "complete" else 1
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
