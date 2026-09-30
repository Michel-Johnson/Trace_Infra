#!/usr/bin/env python3
"""Verify a fixed historical corpus through the actual API in disposable storage.

Use TEST_DATABASE_URL for a dedicated PostgreSQL test database, or explicitly
select --sqlite-test. This is an in-process service regression, not a network
load test. Reports contain aggregates/digests/error codes, never trace bodies.
"""

import argparse
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import platform
import sys
import tempfile
import time
from urllib.parse import quote
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src"), str(ROOT / "apps/api")]
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool

from scripts.build_backend_corpus_manifest import build_manifest, canonical, decode, digest, file_records, MAX_JSON_BYTES
from trace_hunter.content import LocalContentStore
from trace_hunter.identity import metadata
from trace_hunter.storage import Store
from trace_hunter_api.app import create_app

VERSION = "backend-corpus-evaluation/1"
FORBIDDEN_TABLES = ("analyses", "evaluation_jobs", "plugin_invocations")
KIND_COUNTS = {"model": "models", "model_batch": "model_batches", "tool": "tools",
               "agent": "agents", "wait": "waits", "other": "other"}


class EvaluationMismatch(Exception):
    """Only fixed codes, never validator messages or source values."""


def require(condition, code):
    if not condition:
        raise EvaluationMismatch(code)


def verified_manifest(source, manifest_path):
    frozen = decode(Path(manifest_path).read_bytes())
    actual = build_manifest(source)
    require(actual["state"] == "ready", "corpus_not_ready")
    require(frozen == actual, "fixed_manifest_mismatch")
    return actual


def selected_documents(source, manifest, selected):
    """Sequential archive reads without extraction or keeping a corpus in RAM."""
    wanted = {entry["path"]: entry for entry in selected}
    inventory = {entry["path"] for entry in manifest["files"]}
    seen = set()
    for name, size, stream in file_records(Path(source)):
        # build_manifest normalizes a common archive wrapper. The fixed full
        # inventory has already been checked before this pass begins.
        clean = name if Path(source).is_dir() or name in inventory else name.partition("/")[2]
        if clean not in wanted:
            continue
        entry = wanted[clean]
        require(clean not in seen, "duplicate_selected_path")
        require(size <= MAX_JSON_BYTES, "selected_document_too_large")
        raw = stream.read(MAX_JSON_BYTES + 1)
        require(len(raw) == entry["bytes"] and digest(raw) == entry["sha256"], "selected_document_changed")
        seen.add(clean)
        yield entry, raw
    require(seen == set(wanted), "selected_document_missing")


@contextmanager
def isolated_store(sqlite_test, resources):
    """Never falls back to DATABASE_URL; only its own random schema is dropped."""
    admin, store, schema = None, None, None
    temporary = tempfile.TemporaryDirectory(prefix="backend-corpus-")
    directory = temporary.name
    try:
        if sqlite_test:
            location = Path(directory) / "service.sqlite"
        else:
            configured = os.environ.get("TEST_DATABASE_URL")
            require(bool(configured), "test_database_url_required")
            base = make_url(configured)
            require(base.drivername in ("postgresql", "postgresql+psycopg"), "postgresql_test_database_required")
            base = base.set(drivername="postgresql+psycopg")
            admin = create_engine(base, poolclass=NullPool, hide_parameters=True,
                                  connect_args={"connect_timeout": 10})
            name = "backend_corpus_" + uuid.uuid4().hex
            with admin.begin() as db:
                db.execute(text("CREATE SCHEMA " + name))
            schema = name
            location = base.update_query_dict({"options": "-csearch_path=" + schema}).render_as_string(hide_password=False)
        store = Store(location, content_store=LocalContentStore(Path(directory) / "content"))
        store.repository.migrate()
        yield store
    finally:
        try:
            if store is not None:
                store.close()
        finally:
            try:
                if schema is not None:
                    with admin.begin() as db:
                        db.execute(text("DROP SCHEMA " + schema + " CASCADE"))
                resources["database_cleaned"] = True
            finally:
                try:
                    if admin is not None:
                        admin.dispose()
                finally:
                    temporary.cleanup()
                    resources["content_cleaned"] = True


def timing_summary(values):
    if not values:
        return {"count": 0, "total_ms": 0, "mean_ms": None, "p50_ms": None, "p95_ms": None, "max_ms": None}
    ordered = sorted(values)
    return {"count": len(values), "total_ms": round(sum(values), 3),
            "mean_ms": round(sum(values) / len(values), 3),
            "p50_ms": round(ordered[math.ceil(len(values) * .5) - 1], 3),
            "p95_ms": round(ordered[math.ceil(len(values) * .95) - 1], 3), "max_ms": round(max(values), 3)}


def implementation_digest():
    entries = []
    for directory, suffix in (("src/trace_hunter", ".py"), ("apps/api/trace_hunter_api", ".py"), ("db/migrations", ".sql")):
        for path in sorted((ROOT / directory).rglob("*" + suffix)):
            entries.append({"path": path.relative_to(ROOT).as_posix(), "sha256": digest(path.read_bytes())})
    for name in ("scripts/evaluate_backend_corpus.py", "scripts/build_backend_corpus_manifest.py"):
        entries.append({"path": name, "sha256": digest((ROOT / name).read_bytes())})
    return digest(canonical(entries))


def validate_revision(result, entry, document, raw, project_id):
    require(result["created"] is True, "initial_request_not_created")
    revision, index = result["revision"], result["index"]
    require(revision["project_id"] == project_id and revision["run_id"] == entry["run_id"], "revision_identity_mismatch")
    require(revision["revision"] == 1 and revision["previous_revision"] is None, "initial_revision_mismatch")
    require(revision["content"] == {"digest": "sha256:" + entry["sha256"], "size_bytes": len(raw), "media_type": "application/json"}, "content_reference_mismatch")
    require(revision["format_version"] == entry["schema_version"], "format_mismatch")
    require(revision["metadata"]["source_verification"] == "document_bytes_only", "source_verification_mismatch")
    run = metadata(document)["run"]
    require(index["run"] == {key: run[key] for key in ("query_id", "env_id", "harness", "model", "title", "status")}, "indexed_run_mismatch")
    spans = document["spans"]
    kinds = Counter(span["kind"] for span in spans)
    expected = {"records": len(spans), **{value: kinds[key] for key, value in KIND_COUNTS.items()},
                "unknown": sum(span["status"] == "unknown" for span in spans)}
    require(expected["records"] == entry["record_count"] and expected["tools"] == entry["tool_count"]
            and expected["models"] == entry["model_record_count"], "manifest_record_counts_mismatch")
    require(dict(Counter(span["status"] for span in spans if span["kind"] == "tool")) == entry["tool_status"], "manifest_tool_status_mismatch")
    require(index["state"] == "complete" and index["error_code"] is None, "index_not_complete")
    require(index["counts"] == expected, "index_counts_mismatch")
    require(index["content_digest"] == revision["content"]["digest"], "index_content_digest_mismatch")
    require(index["coverage"]["index"] == "complete", "index_coverage_mismatch")
    require(index["coverage"]["capture"] == {key: document["coverage"].get(key, "unknown")
            for key in ("tools", "model_requests", "messages", "contexts", "timing")}, "capture_coverage_mismatch")


def evaluate(input_path, manifest_path, *, sqlite_test=False, limit=None, progress=None):
    """Return a body-free report; errors use fixed stages/codes, not response text."""
    start = time.perf_counter()
    report = {"schema_version": VERSION, "state": "failed", "started_at": datetime.now(timezone.utc).isoformat(),
              "transport": "in_process_fastapi_testclient", "backend": "sqlite_test" if sqlite_test else "postgresql_test_schema",
              "python": platform.python_version(), "implementation_digest": implementation_digest(),
              "cleanup": {"database_cleaned": False, "content_cleaned": False},
              "historical": {"available_runs": 0, "selected_runs": 0, "attempted_runs": 0, "verified_runs": 0},
              "derived": {"generated_runs": 0, "executed_runs": 0}, "errors": []}
    timings, http_statuses = defaultdict(list), Counter()
    aggregate_kinds, aggregate_statuses, aggregate_tools = Counter(), Counter(), Counter()
    stage, run_id = "verify_manifest", None
    try:
        require(limit is None or type(limit) is int and limit > 0, "limit_must_be_positive")
        source = Path(input_path).resolve()
        verify_start = time.perf_counter()
        manifest = verified_manifest(source, manifest_path)
        report["manifest_verification_ms"] = round((time.perf_counter() - verify_start) * 1000, 3)
        selected = sorted(manifest["runs"], key=lambda item: item["run_id"])
        if limit is not None:
            selected = selected[:limit]
        report["input"] = {"corpus_digest": manifest["corpus_digest"], "inventory_digest": manifest["inventory_digest"],
                           "source_files_digest": digest(canonical(manifest["source_files"])),
                           "selected_documents_digest": digest(canonical(selected)),
                           "selection": "first_run_ids_ascending", "limit": limit,
                           "source_kind": "directory" if source.is_dir() else "archive",
                           "duplicate_input_files": manifest["summary"]["duplicate_file_count"]}
        report["historical"].update(available_runs=len(manifest["runs"]), selected_runs=len(selected))
        stage = "service_setup"
        project_id = "historical-regression"
        with isolated_store(sqlite_test, report["cleanup"]) as store:
            app = create_app(store=store, allowed_origins=[])
            with TestClient(app, raise_server_exceptions=False, client=('127.0.0.1', 50000)) as client:
                def request(label, method, url, status, **kwargs):
                    began = time.perf_counter()
                    try:
                        response = client.request(method, url, **kwargs)
                    finally:
                        timings[label].append((time.perf_counter() - began) * 1000)
                    http_statuses[str(response.status_code)] += 1
                    require(response.status_code == status, label + "_http_status")
                    require(response.headers.get("cache-control") == "no-store", label + "_cache_control")
                    return response

                stage = "load_selected_document"
                for entry, raw in selected_documents(source, manifest, selected):
                    run_id = entry["run_id"]
                    report["historical"]["attempted_runs"] += 1
                    try:
                        document = decode(raw)
                        root = "/api/v1/projects/" + project_id + "/traces"
                        revision_url = root + "/" + quote(run_id, safe="") + "/revisions/1"
                        headers = {"Content-Type": "application/json", "Idempotency-Key": "corpus:" + entry["sha256"]}
                        stage = "append"
                        accepted = request(stage, "POST", root, 201, content=raw, headers=headers).json()
                        validate_revision(accepted, entry, document, raw, project_id)
                        stage = "metadata"
                        fetched = request(stage, "GET", revision_url, 200).json()
                        require(fetched == {key: accepted[key] for key in ("revision", "index")}, "metadata_changed")
                        stage = "content"
                        content = request(stage, "GET", revision_url + "/content", 200)
                        require(content.content == raw and digest(content.content) == entry["sha256"], "read_bytes_mismatch")
                        require(content.headers.get("content-type") == "application/json", "content_type_mismatch")
                        stage = "retry"
                        repeated = request(stage, "POST", root, 200, content=raw, headers=headers).json()
                        require(repeated == {**accepted, "created": False}, "idempotent_result_changed")
                        stage = "index_records"
                        groups = store.repository.rows("""SELECT kind,status,count(*) AS n FROM trace_index_records
                            WHERE project_id=:project_id AND run_id=:run_id AND revision=1
                            GROUP BY kind,status""", {"project_id": project_id, "run_id": run_id})
                        require({(row["kind"], row["status"]): row["n"] for row in groups} ==
                                dict(Counter((span["kind"], span["status"]) for span in document["spans"])), "indexed_status_distribution_mismatch")
                        report["historical"]["verified_runs"] += 1
                        aggregate_kinds.update(span["kind"] for span in document["spans"])
                        aggregate_statuses.update(span["status"] for span in document["spans"])
                        aggregate_tools.update(entry["tool_status"])
                    except Exception as error:
                        report["errors"].append({"run_id": run_id, "stage": stage,
                            "code": str(error) if isinstance(error, EvaluationMismatch) else "service_verification_failed"})
                    finally:
                        if progress and (report["historical"]["attempted_runs"] % 50 == 0
                                         or report["historical"]["attempted_runs"] == len(selected)):
                            progress({"attempted": report["historical"]["attempted_runs"],
                                      "verified": report["historical"]["verified_runs"], "errors": len(report["errors"])})
                    stage = "load_selected_document"
                run_id, stage = None, "database_invariants"
                tables = (*FORBIDDEN_TABLES, "trace_heads", "trace_revisions", "trace_revision_requests", "trace_indexes", "trace_index_records")
                report["database_counts"] = {table: store.repository.rows("SELECT count(*) AS n FROM " + table)[0]["n"] for table in tables}
                require(all(report["database_counts"][table] == 0 for table in FORBIDDEN_TABLES), "analysis_or_job_created")
                require(all(report["database_counts"][table] == len(selected) for table in
                            ("trace_heads", "trace_revisions", "trace_revision_requests", "trace_indexes")), "duplicate_or_missing_revision_index")
                require(report["database_counts"]["trace_index_records"] == sum(entry["record_count"] for entry in selected), "total_index_records_mismatch")
        require(all(report["cleanup"].values()), "cleanup_incomplete")
        report["state"] = "passed" if not report["errors"] and report["historical"]["verified_runs"] == len(selected) else "failed"
    except Exception as error:
        report["errors"].append({"run_id": run_id, "stage": stage,
            "code": str(error) if isinstance(error, EvaluationMismatch) else "evaluation_failed"})
    report["counts"] = {"verified_records": sum(aggregate_kinds.values()), "record_kinds": dict(sorted(aggregate_kinds.items())),
                        "record_statuses": dict(sorted(aggregate_statuses.items())), "tool_statuses": dict(sorted(aggregate_tools.items())),
                        "http_statuses": dict(sorted(http_statuses.items())), "error_count": len(report["errors"])}
    report["timings"] = {key: timing_summary(values) for key, values in sorted(timings.items())}
    report["elapsed_ms"] = round((time.perf_counter() - start) * 1000, 3)
    report["notes"] = ["Historical samples are unique fixed-manifest run IDs; duplicate files are not additional runs.",
                       "Derived runs are separate and this evaluator generates none.",
                       "Timings are sequential in-process HTTP service calls, including persistence; they exclude external network latency.",
                       "Capture gaps, source fragments and user turns are not inferred or repaired.",
                       "Source package is read-only; source commands and locators are never executed or fetched."]
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="New aggregate report under ignored var/")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--sqlite-test", action="store_true", help="Explicit disposable SQLite test mode; otherwise TEST_DATABASE_URL is required")
    args = parser.parse_args(argv)
    try:
        output = args.output.resolve()
        require(output.is_relative_to((ROOT / "var").resolve()), "output_must_be_ignored_var")
        require(not args.input.is_dir() or not output.is_relative_to(args.input.resolve()), "output_must_be_outside_source")
        require(not output.exists(), "output_already_exists")
        result = evaluate(args.input, args.manifest, sqlite_test=args.sqlite_test, limit=args.limit,
                          progress=lambda value: print(json.dumps({"progress": value}), file=sys.stderr, flush=True))
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("x", encoding="utf-8") as stream:
            os.chmod(output, 0o600)
            json.dump(result, stream, ensure_ascii=False, sort_keys=True, indent=2)
            stream.write("\n")
        print(json.dumps({"state": result["state"], "historical": result["historical"], "counts": result["counts"], "cleanup": result["cleanup"]}))
        return 0 if result["state"] == "passed" else 1
    except Exception as error:
        print(json.dumps({"state": "failed", "code": str(error) if isinstance(error, EvaluationMismatch) else "invalid_evaluation_configuration"}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
