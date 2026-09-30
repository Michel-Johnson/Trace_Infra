#!/usr/bin/env python3
"""Run synthetic Cases through real API handlers and installed plugin processes.

Uses disposable storage, never DATABASE_URL or the online service. The HTTP
transport is in-process; the official worker launches actual Python plugins.
"""

import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src"), str(ROOT / "apps/api")]

from fastapi.testclient import TestClient
from scripts.evaluate_backend_corpus import implementation_digest, isolated_store
from trace_hunter.workers import OfficialWorker
from trace_hunter_api.app import create_app

CASES = ROOT / "examples/platform-acceptance/cases.json"
DATA_FILE = CASES.parent / "business-scenarios.json"
PROJECT = "acceptance-cases"
PREFIX = "/api/v1/projects/" + PROJECT


def digest(raw):
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def pointer(document, path):
    value = document
    for part in path.lstrip("/").split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        value = value[int(part)] if isinstance(value, list) else value[part]
    return value


class CaseFlow:
    def __init__(self, store, app, stack, capture=None):
        self.store = store
        self.capture = capture
        self.operator = stack.enter_context(TestClient(app, client=("127.0.0.1", 45000)))
        self.client = stack.enter_context(TestClient(app, client=("198.51.100.21", 45001)))
        for project in (PROJECT, "other-project"):
            response = self.operator.post("/api/v1/projects", json={"project_id": project, "name": project})
            assert response.status_code == 201, response.text
        principal = self.operator.post(PREFIX + "/principals", json={"name": "Case test agent", "scopes": [
            "traces:read", "traces:write", "selections:write", "operations:read",
            "invocations:write", "invocations:read", "invocations:execute", "artifacts:read",
            "artifacts:write", "traces:index",
        ]})
        assert principal.status_code == 201, principal.text
        credential = self.operator.post("/api/v1/principals/" + principal.json()["principal_id"] + "/credentials",
                                        json={"ttl_seconds": 3600})
        assert credential.status_code == 201, credential.text
        self.auth = {"Authorization": "Bearer " + credential.json()["token"]}
        self.worker = OfficialWorker(store, worker_id="case-acceptance-worker")
        self.operations = {}
        for installed in self.worker.registry.installed:
            response = self.operator.post(PREFIX + "/operations", json=installed.definition)
            assert response.status_code == 201, response.text
            self.operations[installed.ref["operation_id"]] = response.json()["operation"]["ref"]
        self.current = None
        self.inputs = {}
        self.results = {}

    def request(self, stage, method, path, status=200, *, key=None, **kwargs):
        headers = dict(self.auth)
        if key is not None:
            headers["Idempotency-Key"] = key
        if "content" in kwargs:
            headers["Content-Type"] = "application/json"
        start = time.perf_counter()
        response = self.client.request(method, path, headers=headers, **kwargs)
        if self.capture:
            self.capture.http(stage, response, start, status)
        self.current["requests"].append({"stage": stage, "method": method, "path": path,
            "status": response.status_code, "expected_status": status,
            "elapsed_ms": round((time.perf_counter() - start) * 1000, 3)})
        if response.status_code != status:
            raise AssertionError(f"{stage}: {method} {path}: HTTP {response.status_code}, expected {status}")
        return response

    def check(self, name, actual, expected):
        self.current["checks"].append({"name": name, "actual": actual, "expected": expected,
                                       "passed": actual == expected})
        if actual != expected:
            raise AssertionError(f"{name}: {actual!r} != {expected!r}")

    def work_counts(self):
        return {table: self.store.repository.rows("SELECT count(*) AS n FROM " + table)[0]["n"]
                for table in ("invocations", "artifacts", "analyses", "evaluation_jobs", "plugin_invocations")}

    def import_trace(self, raw, key, *, previous=0, status=201):
        return self.request("import", "POST", PREFIX + "/traces", status, key=key,
                            params={"expected_previous": previous}, content=raw).json()

    def freeze(self, filters, key):
        return self.request("selection", "POST", PREFIX + "/selections", 201, key=key,
                            json={"filters": filters}).json()["selection"]

    def members(self, selected, limit=2):
        path = PREFIX + "/selections/" + selected["selection_id"]
        items, after = [], None
        while True:
            params = {"limit": limit}
            if after is not None:
                params["after"] = after
            page = self.request("selection", "GET", path + "/members", params=params).json()
            items.extend(page["items"])
            after = page["next_after"]
            if after is None:
                break
        manifest = self.request("selection", "GET", path + "/manifest")
        self.check("manifest_digest", digest(manifest.content), selected["manifest"]["digest"])
        self.check("manifest_matches_pages", items, manifest.json()["members"])
        self.check("complete_membership", len(items), selected["member_count"])
        return items

    def execute(self, operation, role, ref, key):
        body = {"operation": self.operations[operation], "inputs": [{"role": role, "ref": ref}], "config": {}}
        accepted = self.request("submit", "POST", PREFIX + "/invocations", 201, key=key, json=body).json()
        invocation = accepted["invocation"]
        repeated = self.request("submit", "POST", PREFIX + "/invocations", key=key, json=body).json()
        self.check("invocation_idempotency", repeated["invocation"], invocation)
        self.check("pending_before_worker", invocation["status"], "pending")
        started = time.perf_counter()
        run = self.worker.run_once(PROJECT, invocation_id=invocation["invocation_id"])
        if self.capture:
            self.capture.worker(invocation, run, started)
        self.current["executions"].append({"operation": operation, "invocation_id": invocation["invocation_id"],
            "state": run["state"], "elapsed_ms": round((time.perf_counter() - started) * 1000, 3)})
        self.check("real_plugin_completed", run["state"], "succeeded")
        path = PREFIX + "/invocations/" + invocation["invocation_id"]
        receipt = self.request("result", "GET", path + "/results/1").json()
        self.check("persisted_receipt", receipt, run["result"])
        self.check("persisted_task_state", self.request("result", "GET", path).json()["status"], "succeeded")
        artifact_ref = receipt["outputs"][0]["artifact"]
        artifact_path = PREFIX + "/artifacts/" + artifact_ref["id"]
        artifact = self.request("evidence", "GET", artifact_path).json()
        content = self.request("evidence", "GET", artifact_path + "/content")
        self.check("artifact_content_digest", digest(content.content), artifact["content"]["digest"])
        self.check("fixed_input_lineage", artifact["inputs"], body["inputs"])
        self.check("fixed_method_version", artifact["producer_claim"], {"name": operation, "version": "1.0.0"})
        found = self.request("evidence", "POST", PREFIX + "/artifacts/query",
                             json={"filters": {"input_ref": ref}}).json()
        self.check("find_result_from_input", artifact_ref["id"] in [item["artifact_id"] for item in found["items"]], True)
        return artifact_ref, content.json()

    def trace_case(self, case):
        raw = (CASES.parent / case["trace"]).read_bytes()
        document = json.loads(raw)
        expected = case["expected"]
        run_id = document["run"]["id"]
        before = self.work_counts()
        accepted = self.import_trace(raw, case["case_id"])
        repeated = self.import_trace(raw, case["case_id"], status=200)
        self.check("import_idempotency", repeated["revision"], accepted["revision"])
        self.check("duplicate_not_created", repeated["created"], False)
        self.check("index_ready", accepted["index"]["state"], "complete")
        for field in ("records", "models", "model_batches", "tools", "waits"):
            self.check("index_" + field, accepted["index"]["counts"][field], expected[field])
        query = {"filters": {"query_id": [document["run"]["query_id"]]},
                 "fields": ["run_id", "revision", "record_count", "model_count", "tool_count"], "limit": 1}
        found = self.request("search", "POST", PREFIX + "/traces/query", json=query).json()
        self.check("query_result", found["items"], [{"run_id": run_id, "revision": 1,
            "record_count": expected["records"], "model_count": expected["models"], "tool_count": expected["tools"]}])
        aggregate = self.request("aggregate", "POST", PREFIX + "/traces/aggregate",
                                 json={"filters": query["filters"]}).json()["totals"]
        self.check("aggregate_error_denominator", aggregate["error_rate"]["denominator"], expected["known_outcomes"])
        self.check("aggregate_error_fraction", aggregate["error_rate"]["value"], expected["error_fraction"])
        selected = self.freeze(query["filters"], case["case_id"] + "-selection")
        members = self.members(selected)
        self.check("case_member_count", len(members), 1)
        member = members[0]
        ref = {"kind": "trace_revision", "id": member["run_id"], "revision": member["revision"],
               "digest": member["content_digest"]}
        self.check("selected_exact_source", ref["digest"], digest(raw))
        path = PREFIX + "/traces/" + run_id + "/revisions/1/content"
        content = self.request("read", "GET", path)
        self.check("source_bytes_unchanged", content.content == raw, True)
        for location, value in case["preserved_values"].items():
            self.check("preserved:" + location, pointer(content.json(), location), value)
        self.check("reading_did_not_start_work", self.work_counts(), before)
        counts_ref, counts = self.execute("official.record-counts", "source", ref, case["case_id"] + "-counts")
        self.check("plugin_record_count", counts["records"], expected["records"])
        self.check("plugin_statuses", counts["statuses"], expected["statuses"])
        self.check("capture_coverage_preserved", counts["capture_coverage"],
                   document["capture"]["coverage"] if "capture" in document else document["coverage"])
        report_ref, report = self.execute("official.record-report", "counts", counts_ref, case["case_id"] + "-report")
        for field, expected_field in (("records", "records"), ("record_errors", "errors"),
                                      ("known_outcomes", "known_outcomes"), ("error_fraction", "error_fraction")):
            self.check("report_" + field, report[field], expected[expected_field])
        self.check("no_invented_quality_verdict", report["quality_verdict"], None)
        self.check("original_still_readable", self.request("evidence", "GET", path).content == raw, True)
        self.inputs[case["case_id"]] = {"raw": raw, "document": document, "ref": ref}
        self.results[case["case_id"]] = {"selection_id": selected["selection_id"], "counts_ref": counts_ref, "report_ref": report_ref}
        self.current["input_digest"] = digest(raw)
        self.current["result_refs"] = self.results[case["case_id"]]

    def batch_case(self, expected):
        before = self.work_counts()
        preview = self.request("search", "POST", PREFIX + "/traces/query", json={"limit": 1}).json()
        self.check("preview_is_one_row", len(preview["items"]), 1)
        aggregate = self.request("aggregate", "POST", PREFIX + "/traces/aggregate", json={}).json()["totals"]
        self.check("batch_size", aggregate["matched_revisions"], expected["members"])
        for field in ("records", "models", "model_batches", "tools", "waits"):
            self.check("batch_" + field, aggregate["counts"][field], expected[field])
        for field in ("ok", "error", "unknown"):
            self.check("batch_" + field, aggregate["record_status"][field], expected[field])
        selected = self.freeze({}, "whole-batch")
        original_members = self.members(selected)
        self.check("full_selection_not_preview", len(original_members), expected["members"])
        source = self.inputs["CASE-001"]
        changed = json.loads(source["raw"])
        changed["spans"] = [span for span in changed["spans"] if span["id"] != "write"]
        changed["links"] = [link for link in changed["links"] if link["to"] != "write"]
        changed["evidence"] = []
        self.import_trace(json.dumps(changed).encode(), "correct-first", previous=1)
        late = json.loads(source["raw"])
        late["run"]["id"] = "later-run"
        self.import_trace(json.dumps(late).encode(), "late-arrival")
        repeated = self.request("selection", "POST", PREFIX + "/selections", key="whole-batch", json={}).json()
        self.check("selection_retry_stays_fixed", repeated["selection"], selected)
        self.check("members_survive_new_data", self.members(selected), original_members)
        latest = self.request("search", "POST", PREFIX + "/traces/query",
                              json={"filters": {"run_id": [source["ref"]["id"]]}}).json()["items"][0]
        self.check("live_query_sees_revision_2", latest["revision"], 2)
        self.check("live_query_sees_changed_count", latest["record_count"], 3)
        self.check("batch_reads_do_not_execute", self.work_counts(), before)
        _, old_counts = self.execute("official.record-counts", "source", source["ref"], "frozen-original-counts")
        self.check("execution_uses_frozen_revision", old_counts["records"], 4)
        empty = self.freeze({"query_id": ["no-such-case"]}, "empty-query")
        self.check("empty_selection_is_known_zero", self.members(empty), [])
        self.current["result_refs"] = {"selection_id": selected["selection_id"]}

    def access_case(self):
        self.request("access", "POST", "/api/v1/projects/other-project/traces/query", 403, json={})
        artifact_id = self.results["CASE-001"]["report_ref"]["id"]
        self.request("access", "GET", "/api/v1/projects/other-project/artifacts/" + artifact_id, 403)
        self.request("access", "GET", PREFIX + "/artifacts/" + artifact_id)


def run_suite(*, sqlite_test, suite="extended", capture_dir=None):
    manifest = json.loads(CASES.read_text())
    assert manifest["synthetic"] is True
    started = time.perf_counter()
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, capture_output=True)
    report = {"suite_id": manifest["suite_id"], "suite_version": "2.0.0" if suite == "extended" else manifest["version"],
        "suite": suite,
        "started_at": datetime.now(timezone.utc).isoformat(), "database": "sqlite" if sqlite_test else "postgresql",
        "transport": "in_process_real_api_handlers", "plugins": "real_official_worker_subprocesses",
        "source_commit": commit.stdout.strip() if commit.returncode == 0 else None,
        "implementation_digest": implementation_digest(),
        "runner_digest": digest(Path(__file__).read_bytes()),
        "fixture_manifest_digest": digest(CASES.read_bytes()), "cases": [], "cleanup": {},
        "not_tested": ["browser_ui", "live_network_deployment", "sancho_codex_runtime", "external_connectors",
                       "environment_replay", "training_consumption", "statistical_or_business_validity"],
        "capability_gaps": []}
    if suite == "extended":
        files = [ROOT / 'scripts/platform_acceptance_scenarios.py', DATA_FILE, CASES.parent / 'coverage-plan.json']
        files += sorted((CASES.parent / 'runtime').rglob('*.py')) + sorted((CASES.parent / 'runtime').rglob('*.json'))
        report['scenario_sources'] = {str(path.relative_to(ROOT)): digest(path.read_bytes()) for path in files}
        coverage_plan = json.loads((CASES.parent / 'coverage-plan.json').read_text())
        report['planned_cases'] = coverage_plan['planned_cases']
        report['coverage_areas'] = coverage_plan['areas']
    with ExitStack() as stack:
        stack.enter_context(patch.dict("os.environ", {"TRACE_HUNTER_REQUIRE_SERVICE_AUTH": "false",
                                                       "TRACE_HUNTER_OPERATOR_NETWORKS": "127.0.0.1/32,::1/128"}))
        store = stack.enter_context(isolated_store(sqlite_test, report["cleanup"]))
        from scripts.acceptance_capture import AcceptanceCapture
        capture = AcceptanceCapture(capture_dir) if capture_dir else None
        flow = CaseFlow(store, create_app(store, allowed_origins=[]), stack, capture)

        def run(case_id, title, action):
            item = {"case_id": case_id, "title": title, "status": "running", "requests": [], "checks": [], "executions": []}
            report["cases"].append(item)
            flow.current = item
            if capture:
                capture.begin(case_id)
            tick = time.perf_counter()
            try:
                action()
                item["status"] = "passed"
            except Exception as error:
                item["status"] = "failed"
                item["error"] = {"type": type(error).__name__, "message": str(error)}
            item["elapsed_ms"] = round((time.perf_counter() - tick) * 1000, 3)

        for case in manifest["cases"]:
            run(case["case_id"], case["title"], lambda case=case: flow.trace_case(case))
        if all(item["status"] == "passed" for item in report["cases"]):
            run("FLOW-007", "批量分页、完整选集与版本变更", lambda: flow.batch_case(manifest["batch_expected"]))
            run("FLOW-008", "同一 Agent 的项目权限边界", flow.access_case)
        else:
            for case_id in ('FLOW-007', 'FLOW-008'):
                report['cases'].append({'case_id': case_id, 'title': '依赖基础 Case', 'status': 'blocked',
                    'reason': 'Baseline prerequisites failed', 'checks': [], 'requests': [], 'executions': []})
        if suite == "extended":
            from scripts.platform_acceptance_scenarios import WorkflowScenarios
            for case_id, title, action in WorkflowScenarios(flow).actions():
                run(case_id, title, action)
        flow.current = {"requests": []}
        # Capability discovery is outside a Case's timeline.
        flow.capture = None
        capabilities = flow.request("capabilities", "GET", "/api/v1/query-capabilities").json()
        report["query_capabilities"] = capabilities
        if "skill" not in capabilities["filter_fields"]:
            report["capability_gaps"].append("运行级查询尚无 skill/API 过滤；本轮按 query_id 固定 Case，不能声称已支持跨轨迹技能搜索。")
        if "turn_count" not in capabilities["fields"]:
            report["capability_gaps"].append("用户轮次在 v2 原文保留；查询没有轮次统计字段，本轮未将模型数或片段数冒充用户轮数。")
        report["capability_gaps"].append("现有计数插件逐条接受 trace_revision；选集需展开为固定成员执行，尚无整组选集统计插件。")
    report["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 3)
    report["summary"] = {state: sum(item["status"] == state for item in report["cases"]) for state in ("passed", "failed", "blocked")}
    report["summary"]["worker_attempts"] = sum(len(item["executions"]) for item in report["cases"])
    report["summary"]["worker_attempts_note"] = "Includes failures before a plugin process launches; not a model-call count."
    report["summary"]["http_requests"] = sum(len(item["requests"]) for item in report["cases"])
    if suite == 'extended':
        actual = [item['case_id'] for item in report['cases']]
        if sorted(actual) != sorted(coverage_plan['executable_ids']):
            raise AssertionError('Executed Case IDs do not match the fixed coverage plan')
        report['summary']['planned_not_run'] = len(report['planned_cases'])
    if capture:
        capture.finish(report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    database = parser.add_mutually_exclusive_group(required=True)
    database.add_argument("--sqlite-test", action="store_true")
    database.add_argument("--postgres-test", action="store_true", help="Use TEST_DATABASE_URL and a disposable random schema")
    parser.add_argument("--output", type=Path, default=ROOT / "var/platform-acceptance/report.json")
    parser.add_argument("--suite", choices=("baseline", "extended"), default="extended")
    parser.add_argument("--capture-dir", type=Path, help="Export this synthetic rerun's exact HTTP bodies and worker receipts locally")
    args = parser.parse_args()
    report = run_suite(sqlite_test=args.sqlite_test, suite=args.suite, capture_dir=args.capture_dir)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"report": str(args.output), **report["summary"], "elapsed_ms": report["elapsed_ms"]}, ensure_ascii=False))
    return 1 if report["summary"]["failed"] or report["summary"]["blocked"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
