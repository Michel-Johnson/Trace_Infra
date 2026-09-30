"""Conservative bridge for existing 1.1 inputs, including duplicate model fragments."""
import copy
import json
import math
from collections import defaultdict

from ..protocol import validate as validate_v1, InvalidTrace
from .common import Conversion, base, content_ref, issue, reject, searchable_content, source_ref


def convert(raw, source, binding):
    try:
        validate_v1(source)
    except InvalidTrace:
        reject("SOURCE_SCHEMA_INVALID", "", "输入不符合已发布的 1.1 协议。", "先通过原 1.1 validator 校验，保持来源 ID 和引用一致。")
    embedded = {"run_id": source["run"]["id"], "query_id": source["run"]["query_id"], "env_id": source["run"]["env_id"]}
    if any(value is not None and embedded.get(key) != value for key, value in binding.items()):
        reject("BINDING_CONFLICT", "/binding", "显式身份与原轨迹身份不同。", "移除覆盖参数；跨 Case 重新分配必须由任务组织者明确处理。")
    out = base(raw, embedded, {"name": source["run"]["harness"], "version": None},
               "trace-hunter/1.1", "interop-preview/2")
    result = Conversion(out)
    out["run"]["status"] = source["run"]["status"] if source["run"]["status"] != "partial" else "unknown"
    if source["run"]["status"] == "partial":
        result.issues.append(issue("RUN_STATUS_AMBIGUOUS", "/run/status", "旧 partial 状态保留在来源，不能等同于成功结束。", "可补采执行终态；当前不推断成功。", classification="ambiguous"))
    out["run"]["environment"] = {k: copy.deepcopy(v) for k, v in source["environment"].items() if k in ("isolation", "network_access", "observed_at", "tool_versions")}
    out["extensions"]["trace_hunter.v1_run"] = copy.deepcopy(source["run"])
    out["clocks"] = [{"id": "legacy-clock", "kind": "unknown", "origin_at": None, "uncertainty_ms": None}]
    out["extensions"]["trace_hunter.v1_time_basis"] = source["run"]["time_basis"]
    old_sources = {}
    for index, original in enumerate(source["sources"]):
        name = "legacy-source-" + str(index)
        old_sources[original["id"]] = name
        out["sources"].append({"id": name, "state": "external", "sha256": original["sha256"], "locator": original["name"]})
    out["phases"] = [{"id": p["id"], "name": p["name"], "purpose": p["purpose"],
                      "timing": {"clock_id": "legacy-clock", "start_ms": p["start_ms"], "end_ms": p["end_ms"]},
                      "source_refs": [source_ref(f"/phases/{i}")]} for i, p in enumerate(source["phases"])]
    out["capture"]["coverage"].update(copy.deepcopy(source["coverage"]))
    groups = defaultdict(list)
    for index, s in enumerate(source["spans"]):
        key = ("request", s["agent_id"], s["request_id"], s["attempt"]) if s["kind"] == "model" else ("record", s["id"])
        groups[key].append((index, s))
    ids = {s["id"]: records[0][1]["id"] for records in groups.values() for _, s in records}
    result.mapping = {"source_model_records": sum(s["kind"] == "model" for s in source["spans"]),
                      "normalized_model_requests": sum(records[0][1]["kind"] == "model" for records in groups.values()),
                      "merged_fragments": sum(len(records) - 1 for records in groups.values()), "source_span_ids": ids}
    source_tools = [s for s in source["spans"] if s["kind"] == "tool"]
    complete_sequence = all("sequence" in s for s in source_tools)
    for records in groups.values():
        index, first = records[0]
        path = f"/spans/{index}"
        s = {"id": first["id"], "kind": first["kind"], "name": first["name"], "agent_id": first["agent_id"], "segment_id": "segment-1",
             "phase_id": first["phase_id"], "parent_id": ids.get(first["parent_id"]), "status": first["status"],
             "source_refs": [source_ref(f"/spans/{i}") for i, _ in records]}
        # Source fragments are observations of the same request. Never infer its
        # execution endpoint from the final fragment's publication timestamp.
        t = {"clock_id": "legacy-clock"}
        for field in ("start_ms", "end_ms", "duration_ms"):
            values = {old[field] for _, old in records}
            t[field] = next(iter(values)) if len(values) == 1 else None
            if len(values) > 1:
                result.issues.append(issue("FRAGMENT_TIME_AMBIGUOUS", path + "/" + field, "同一请求的片段时间不同，归一化边界保持未知。", "补采请求开始/结束事件；原片段均保留在 source.json。", classification="ambiguous"))
        s["timing"] = t
        if t["duration_ms"] is not None:
            t.update(duration_basis="source_reported", duration_scope="unknown")
            if t["start_ms"] is not None and t["end_ms"] is not None and not math.isclose(t["end_ms"] - t["start_ms"], t["duration_ms"], abs_tol=0.001):
                result.issues.append(issue("TIMING_MEASUREMENTS_DIFFER", path + "/duration_ms", "上报时长与起止时间差不同，两套来源值均保留。", "核对精度与计时范围；展示上报值时注明来源，区间统计仍使用端点。", classification="ambiguous"))
        for field in ("phase_id", "parent_id", "status"):
            if len({old[field] for _, old in records}) > 1:
                reject("REQUEST_IDENTITY_AMBIGUOUS", path + "/" + field, "同请求片段的阶段、父级或终态冲突。", "先确认来源请求身份；不要按文字相似度合并。")
        for field in ("input", "output"):
            if all(old[field] == first[field] for _, old in records):
                s[field] = searchable_content(first[field], path + "/" + field, missing_null=True)
            else:
                s[field] = {"state": "missing"}
                result.issues.append(issue("FRAGMENT_CONTENT_NOT_ASSEMBLED", path + "/" + field, "多片段内容已归档，但不冒充拼接后的最终响应。", "由原生流式 adapter 根据分片语义重组。", classification="ambiguous"))
        if s["kind"] == "model":
            s["call"] = {"invocation_id": first["request_id"], "attempt": first["attempt"], "provider_request_id": None}
            populated = [old["usage"] for _, old in records if old["usage"] is not None]
            unique = {json.dumps(u, sort_keys=True) for u in populated}
            usage = None
            if len(unique) == 1:
                counters = {("reasoning_tokens" if k == "thinking_tokens" else k): v for k, v in populated[0].items()}
                usage = {"accounting": "total", "completeness": "complete" if all(counters[k] is not None for k in ("input_tokens", "output_tokens")) else "partial",
                         "total": counters, "source_refs": [source_ref(f"/spans/{i}/usage") for i, old in records if old["usage"] is not None]}
            elif len(unique) > 1:
                result.issues.append(issue("USAGE_CONFLICT", path + "/usage", "同一请求的 usage 冲突，未采用任何一个作为总量。", "提供有终态标记的权威 usage；不要取最大值或相加。", classification="ambiguous"))
            s["model"] = {"provider": None, "requested_model": None, "response_model": None, "request_count": 1, "context_id": None, "usage": usage}
        elif s["kind"] == "tool":
            s["tool"] = {"operation": first["operation"] if first["operation"] in ("read", "write", "bash", "human") else "other"}
            if "skill" in first:
                s["tool"]["skill"] = copy.deepcopy(first["skill"])
            elif first["operation"] == "skill":
                result.issues.append(issue("SKILL_SEMANTICS_UNKNOWN", path + "/operation", "旧 skill 操作没有加载/调用证据，保留原名。", "补充 skill.action；不默认改成 read。", classification="ambiguous"))
            # Only tools share the source's global tool sequence. Using input
            # positions for models would collide and falsely promise one order.
            s["order"] = {"stream_id": "v1-tools" if complete_sequence else "v1-input-position", "sequence": first["sequence"] if complete_sequence else index}
        elif s["kind"] == "wait":
            s["wait"] = {"reason": "human_input" if first["operation"] == "human" else "unknown"}
        for _, original in records:
            original_ref = {"source_id": old_sources[original["source"]["source_id"]], "pointer": original["source"]["pointer"]}
            if original_ref not in s["source_refs"]:
                s["source_refs"].append(original_ref)
        out["spans"].append(s)
    seen = set()
    for index, link in enumerate(source["links"]):
        if link["type"] == "follows":
            # 1.1 enumerated this relation but never froze its direction.
            # Do not turn an under-specified link into a claimed dependency.
            out["events"].append({"id": "legacy-link-" + str(index), "type": "other", "segment_id": "segment-1", "data": content_ref(link, f"/links/{index}"), "source_refs": [source_ref(f"/links/{index}")]})
            result.issues.append(issue("LEGACY_LINK_DIRECTION_UNSPECIFIED", f"/links/{index}", "旧 follows 关系保留为来源事件，没有改写为因果依赖。", "由来源 adapter 明确方向后再映射 depends_on。", classification="ambiguous"))
            continue
        kind = link["type"]
        start, end = ids[link["from"]], ids[link["to"]]
        key = (start, end, kind)
        if start != end and key not in seen:
            out["links"].append({"from": start, "to": end, "type": kind, "source_refs": [source_ref(f"/links/{index}")]})
            seen.add(key)
    if source["evidence"]:
        out["artifacts"] = [{"id": "legacy-evidence", "kind": "other", "content": content_ref(source["evidence"], "/evidence"), "source_refs": [source_ref("/evidence")]}]
    timed = [s for s in out["spans"] if s["kind"] == "tool"]
    out["capture"]["coverage"]["timing"] = "partial" if timed else "missing"
    result.issues += [issue("REQUEST_CONTEXT_MISSING", "/contexts", "1.1 未定义完整请求上下文，不能由聊天历史重构。", "后续采集实际 request body、工具定义与参数。"),
                      issue("MODEL_IDENTITY_UNVERIFIED", "/run/model", "原 model 展示标签已保留，但不等同于供应商计费模型身份。", "采集 provider 与实际请求/响应 model ID。")]
    return result
