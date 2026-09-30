"""Adapter for the versioned Agent Trace Benchmark envelope and its known variants."""
import json
import re
from collections import Counter
from datetime import datetime

from .common import Conversion, base, issue, reject, searchable_content, source_ref
from .native_common import operation


FORMAT = "agent-benchmark/1"
ADAPTER_VERSION = "1.1.0"
_DURATION = re.compile(
    r"^PT(?:(?P<hours>\d+(?:\.\d+)?)H)?(?:(?P<minutes>\d+(?:\.\d+)?)M)?(?:(?P<seconds>\d+(?:\.\d+)?)S)?$"
)
_RETURNCODE = re.compile(r"<returncode>\s*(-?\d+)\s*</returncode>", re.IGNORECASE)


def _content(value, pointer, *, missing_null=False):
    return searchable_content(value, pointer, missing_null=missing_null)


def _role(value):
    return value if value in {"system", "developer", "user", "assistant", "tool"} else "other"


def _status(value):
    normalized = str(value).strip().lower()
    if normalized in {"ok", "success", "succeeded"}:
        return "ok"
    if normalized in {"error", "failed", "failure"}:
        return "error"
    if normalized in {"running", "cancelled"}:
        return normalized
    return "unknown"


def _explicit_text_status(value):
    if isinstance(value, str) and value.lstrip().upper().startswith("[ERROR]"):
        return "error"
    return "unknown"


def _duration_ms(value):
    if not isinstance(value, str):
        return None
    matched = _DURATION.fullmatch(value)
    if not matched or not any(matched.groupdict().values()):
        return None
    return 1000 * (
        float(matched.group("hours") or 0) * 3600
        + float(matched.group("minutes") or 0) * 60
        + float(matched.group("seconds") or 0)
    )


def _token_count(value, path):
    if value is None:
        return None
    if isinstance(value, bool):
        reject("TOKEN_COUNT_INVALID", path, "token 计数不能是布尔值。", "保留非负整数或十进制整数字符串。")
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, float) and value >= 0 and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.isdigit():
        return int(value)
    reject("TOKEN_COUNT_INVALID", path, "token 计数不是非负整数。", "保留非负整数或十进制整数字符串；未知时省略。")


def _instant(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.utcoffset() is not None else None


def _query(out, source):
    turn_id = "turn-query"
    message_id = "message-query"
    out["messages"].append({
        "id": message_id,
        "role": "user",
        "turn_id": turn_id,
        "content": _content(source["query"], "/query"),
        "source_refs": [source_ref("/query")],
    })
    out["turns"].append({
        "id": turn_id,
        "input_message_ids": [message_id],
        "status": "unknown",
        "source_refs": [source_ref("/query")],
    })
    return turn_id, message_id


def _model(out, span_id, message_id, turn_id, content, pointer, model=None, *, order=None):
    span = {
        "id": span_id,
        "kind": "model",
        "name": "assistant response",
        "agent_id": "root",
        "segment_id": "segment-1",
        "turn_id": turn_id,
        "status": "unknown",
        "output": content,
        "call": {"invocation_id": span_id, "attempt": 1, "provider_request_id": None},
        "model": {
            "provider": None,
            "requested_model": model,
            "response_model": model,
            "request_count": 1,
            "context_id": None,
            "usage": None,
            "output_message_ids": [message_id],
        },
        "source_refs": [source_ref(pointer)],
    }
    if order is not None:
        span["order"] = order
    out["spans"].append(span)
    return span


def _envelope(raw, source, binding):
    required = ("id", "benchmark", "query", "source_id", "source_url", "license")
    if any(not isinstance(source.get(key), str) or not source[key] for key in required):
        reject("SOURCE_SCHEMA_INVALID", "", "Agent benchmark 封套缺少稳定字符串字段。", "保留 id、benchmark、query、source_id、source_url、license 和 trace。")
    if not isinstance(source.get("trace"), (dict, list)):
        reject("SOURCE_SCHEMA_INVALID", "/trace", "trace 必须是对象或数组。", "使用未改写的 benchmark JSON 文件。")
    metadata = source.get("metadata")
    if metadata is not None and not isinstance(metadata, dict):
        reject("SOURCE_SCHEMA_INVALID", "/metadata", "metadata 必须是对象或缺失。", "不要把 metadata 序列化成字符串。")
    harness = {"name": source["benchmark"], "version": None}
    out = base(raw, binding, harness, FORMAT, ADAPTER_VERSION)
    out["segments"][0]["session"] = {"namespace": FORMAT, "id": source["id"]}
    out["extensions"]["agent_benchmark.envelope"] = {
        "id": source["id"],
        "benchmark": source["benchmark"],
        "source_id": source["source_id"],
        "source_url": source["source_url"],
        "license": source["license"],
        "subset": source.get("subset"),
        "access": source.get("access"),
        "metadata": metadata,
    }
    _query(out, source)
    return out


def _variant(trace):
    if isinstance(trace, list):
        if trace and all(isinstance(step, dict) and "action" in step for step in trace):
            return "rootse-steps"
        return None
    if isinstance(trace.get("messages"), list) and trace["messages"] and isinstance(trace.get("tools"), list):
        return "apb-messages"
    spans = trace.get("spans")
    if isinstance(spans, list) and spans and all(isinstance(span, dict) and isinstance(span.get("raw"), str) and isinstance(span.get("id"), str) for span in spans):
        return "tel-raw-spans"
    if isinstance(spans, list) and spans and all(isinstance(span, dict) and isinstance(span.get("span_name"), str) for span in spans):
        return "trail-otel"
    if isinstance(trace.get("files"), list):
        return "ctb-embedded-files"
    return None


def _apb(out, source, result):
    trace = source["trace"]
    messages = trace["messages"]
    if not all(isinstance(message, dict) and isinstance(message.get("role"), str) and isinstance(message.get("content"), str) for message in messages):
        reject("SOURCE_SCHEMA_INVALID", "/trace/messages", "APB messages 必须含字符串 role/content。", "保留原始聊天消息结构。")
    current_turn = "turn-query"
    proposals = {}
    observed = set()
    non_assistant_requests = 0
    model_name = (source.get("metadata") or {}).get("model")
    for index, message in enumerate(messages):
        path = f"/trace/messages/{index}"
        role = _role(message["role"])
        if role == "user" and message["content"] == source["query"] and index == next((i for i, item in enumerate(messages) if item.get("role") == "user"), -1):
            out["messages"][0]["source_refs"].append(source_ref(path))
            current_turn = "turn-query"
            continue
        message_id = f"apb-message-{index}"
        if role == "user":
            current_turn = f"apb-turn-{index}"
            out["turns"].append({"id": current_turn, "input_message_ids": [message_id], "status": "unknown", "source_refs": [source_ref(path)]})
        content = _content(message["content"], path + "/content")
        normalized = {"id": message_id, "role": role, "turn_id": current_turn if role != "system" else None,
                      "content": content, "source_refs": [source_ref(path)]}
        model_span = None
        if role == "assistant":
            model_span = _model(out, f"apb-model-{index}", message_id, current_turn, content, path, model_name,
                                order={"stream_id": "apb-messages", "sequence": index})
        calls = message.get("tool_calls") or []
        if not isinstance(calls, list):
            reject("TOOL_CALLS_INVALID", path + "/tool_calls", "tool_calls 必须是数组。", "保留 OpenAI 风格工具调用数组。")
        for call_index, call in enumerate(calls):
            call_path = path + f"/tool_calls/{call_index}"
            function = call.get("function") if isinstance(call, dict) else None
            if not isinstance(call, dict) or not isinstance(call.get("id"), str) or not isinstance(function, dict) or not isinstance(function.get("name"), str) or "arguments" not in function:
                reject("TOOL_PROPOSAL_INVALID", call_path, "工具提案缺少 id、function.name 或 arguments。", "保留原始工具调用字段。")
            if call["id"] in proposals:
                reject("TOOL_PROPOSAL_ID_CONFLICT", call_path + "/id", "工具提案 ID 重复。", "按单次运行保留唯一 call id。")
            arguments = _content(function["arguments"], call_path + "/function/arguments")
            proposal = None
            if role == "assistant":
                proposal_id = f"apb-proposal-{index}-{call_index}"
                proposal = {"id": proposal_id, "call_id": call["id"], "name": function["name"],
                            "arguments": arguments, "message_id": message_id,
                            "model_span_id": model_span["id"] if model_span else None,
                            "source_refs": [source_ref(call_path)]}
                out["tool_calls"].append(proposal)
            else:
                non_assistant_requests += 1
            proposals[call["id"]] = (proposal, call_path, function["name"], arguments)
        if role == "tool":
            call_id = message.get("tool_call_id")
            matched = proposals.get(call_id)
            if matched:
                proposal, call_path, request_name, arguments = matched
                span_id = f"apb-tool-{index}"
                span = {"id": span_id, "kind": "tool", "name": str(message.get("name") or request_name),
                        "agent_id": "root", "segment_id": "segment-1", "turn_id": current_turn,
                        "status": _explicit_text_status(message["content"]),
                        "tool": {"operation": operation(request_name), "call_id": call_id,
                                 "proposal_id": proposal["id"] if proposal else None},
                        "input": arguments, "output": content,
                        "order": {"stream_id": "apb-tools", "sequence": len(observed)},
                        "source_refs": [source_ref(call_path), source_ref(path)]}
                out["spans"].append(span)
                normalized["tool_span_id"] = span_id
                observed.add(call_id)
                if proposal and proposal["model_span_id"]:
                    out["links"].append({"from": proposal["model_span_id"], "to": span_id, "type": "invokes", "source_refs": span["source_refs"]})
        out["messages"].append(normalized)
    answer = trace.get("answer_text")
    if "answer_text" in trace:
        out.setdefault("artifacts", []).append({"id": "apb-answer", "kind": "output",
            "content": _content(answer, "/trace/answer_text", missing_null=True), "source_refs": [source_ref("/trace/answer_text")]})
    result.mapping.update(source_messages=len(messages), model_responses=sum(s["kind"] == "model" for s in out["spans"]),
                          source_tool_requests=len(proposals), tool_proposals=len(out["tool_calls"]),
                          non_assistant_tool_requests=non_assistant_requests,
                          tool_executions=len(observed), unmatched_tool_requests=len(proposals) - len(observed),
                          unmatched_tool_proposals=sum(
                              proposal is not None and call_id not in observed
                              for call_id, (proposal, _, _, _) in proposals.items()))
    out["capture"]["coverage"].update(tools="complete" if len(proposals) == len(observed) else "partial", model_requests="partial", messages="complete", contexts="missing", timing="missing")
    if non_assistant_requests:
        result.issues.append(issue(
            "NON_ASSISTANT_TOOL_REQUESTS", "/trace/messages",
            "来源把部分工具请求放在非 assistant 消息中；已保留真实执行，但未伪造模型 Tool proposal。",
            "来源若能证明这些请求由模型提出，应使用 assistant 消息或提供独立模型调用关联。",
            classification="ambiguous"))


def _rootse(out, source, result):
    steps = source["trace"]
    action_count = 0
    for index, step in enumerate(steps):
        path = f"/trace/{index}"
        span_id = f"rootse-step-{index}"
        step_status = _status(step.get("state"))
        action_list = isinstance(step["action"], list)
        raw_actions = step["action"] if action_list else [step["action"]]
        actions = [action for action in raw_actions if action not in (None, "")]
        if step_status == "unknown" and any(isinstance(action, dict) and action.get("call_ok") is False for action in actions):
            step_status = "error"
        out["spans"].append({"id": span_id, "kind": "agent", "name": f"Step {step.get('index') or index + 1}",
            "agent_id": "root", "segment_id": "segment-1", "turn_id": "turn-query", "status": step_status,
            "input": _content({"thought": step.get("thought"), "response": step.get("response")}, path),
            "output": _content(step.get("observation"), path + "/observation", missing_null=True),
            "order": {"stream_id": "rootse-steps", "sequence": index}, "source_refs": [source_ref(path)]})
        for action_index, action in enumerate(actions):
            action_path = path + (f"/action/{action_index}" if action_list else "/action")
            if isinstance(action, dict) and isinstance(action.get("func_name"), str):
                name, arguments, call_ok = action["func_name"], action.get("arguments"), action.get("call_ok")
                input_pointer = action_path + "/arguments"
            elif isinstance(action, dict) and isinstance(action.get("tool"), str):
                name, arguments, call_ok = action["tool"], action.get("input"), action.get("call_ok")
                input_pointer = action_path + "/input"
            elif isinstance(action, str):
                name, arguments, call_ok = (action.strip().split(None, 1) or ["action"])[0], action, None
                input_pointer = action_path
            else:
                reject("TOOL_EXECUTION_INVALID", action_path, "ROOTSE action 无法识别。", "保留字符串 action、tool/input 或 func_name/arguments/call_ok 结构。")
            if arguments is None:
                arguments = {}
            action_count += 1
            call_id = f"rootse-call-{index}-{action_index}"
            out["spans"].append({"id": call_id, "kind": "tool", "name": name, "agent_id": "root",
                "segment_id": "segment-1", "turn_id": "turn-query", "parent_id": span_id,
                "status": "ok" if call_ok is True else "error" if call_ok is False else "unknown",
                "tool": {"operation": operation(name), "call_id": call_id, "proposal_id": None},
                "input": _content(arguments, input_pointer), "output": {"state": "missing"},
                "order": {"stream_id": "rootse-actions", "sequence": action_count - 1}, "source_refs": [source_ref(action_path)]})
    result.mapping.update(source_steps=len(steps), tool_executions=action_count)
    out["capture"]["coverage"].update(tools="partial", model_requests="missing", messages="partial", contexts="missing", timing="missing")
    result.issues.append(issue("AGGREGATE_OBSERVATION_UNATTRIBUTED", "/trace", "每步 observation 未提供逐 action 归属，仅保存在步骤输出中。", "来源补充每次 action 的独立结果后再建立一一对应。", classification="ambiguous"))


def _tel(out, source, result):
    spans = source["trace"]["spans"]
    seen = set()
    for index, span in enumerate(spans):
        path = f"/trace/spans/{index}"
        if span["id"] in seen:
            reject("SPAN_ID_CONFLICT", path + "/id", "TEL span id 重复。", "保留运行内唯一 span id。")
        seen.add(span["id"])
        out["spans"].append({"id": "tel-" + span["id"], "kind": "other", "name": "TEL raw step " + span["id"],
            "agent_id": "root", "segment_id": "segment-1", "turn_id": "turn-query",
            "status": _explicit_text_status(span["raw"]), "output": _content(span["raw"], path + "/raw"),
            "order": {"stream_id": "tel-spans", "sequence": index}, "source_refs": [source_ref(path)]})
    result.mapping.update(source_spans=len(spans), opaque_steps=len(spans))
    out["capture"]["coverage"].update(tools="unknown", model_requests="unknown", messages="partial", contexts="missing", timing="missing")
    result.issues.append(issue("OPAQUE_STEP_SEMANTICS", "/trace/spans", "TEL 仅提供有序 raw 正文，未猜测其模型或工具边界。", "来源补充 span 类型、调用关系与时间。", classification="unsupported"))


def _flatten(spans, base_path="/trace/spans"):
    result = []
    for index, span in enumerate(spans):
        path = f"{base_path}/{index}"
        result.append((span, path))
        children = span.get("child_spans") or []
        if not isinstance(children, list):
            reject("SOURCE_SCHEMA_INVALID", path + "/child_spans", "TRAIL child_spans 必须是数组。", "保留嵌套 OTel span 结构。")
        result.extend(_flatten(children, path + "/child_spans"))
    return result


def _trail(out, source, result):
    flattened = _flatten(source["trace"]["spans"])
    ids = [span.get("span_id") for span, _ in flattened]
    if any(not isinstance(value, str) or not value for value in ids):
        reject("SPAN_ID_INVALID", "/trace/spans", "TRAIL span_id 必须是非空字符串。", "保留原始 OTel span_id。")
    instants = [_instant(span.get("timestamp")) for span, _ in flattened]
    valid_instants = [value for value in instants if value is not None]
    origin = min(valid_instants) if valid_instants else None
    if origin:
        out["clocks"] = [{"id": "trail-wall", "kind": "wall", "origin_at": origin.isoformat().replace("+00:00", "Z"), "uncertainty_ms": None}]
    totals = Counter(ids)
    occurrences = {}
    normalized_ids = []
    for value in ids:
        occurrences[value] = occurrences.get(value, 0) + 1
        suffix = "" if totals[value] == 1 else f"-occurrence-{occurrences[value]}"
        normalized_ids.append("trail-" + value + suffix)
    unique_id_map = {value: normalized_ids[index] for index, value in enumerate(ids) if totals[value] == 1}
    model_count = tool_count = timed_count = 0
    for index, (((span, path), instant), normalized_id) in enumerate(zip(zip(flattened, instants), normalized_ids)):
        attributes = span.get("span_attributes") or {}
        if not isinstance(attributes, dict):
            reject("SOURCE_SCHEMA_INVALID", path + "/span_attributes", "TRAIL span_attributes 必须是对象。", "保留原始 OTel attributes。")
        source_kind = str(attributes.get("openinference.span.kind") or "").upper()
        kind = "model" if source_kind == "LLM" else "tool" if source_kind == "TOOL" else "agent" if source_kind in {"AGENT", "CHAIN"} else "other"
        normalized = {"id": normalized_id, "kind": kind, "name": span["span_name"], "agent_id": "root",
            "segment_id": "segment-1", "turn_id": "turn-query", "status": _status(span.get("status_code")),
            "source_refs": [source_ref(path)], "order": {"stream_id": "trail-spans", "sequence": index},
            "extensions": {"agent_benchmark.source_kind": source_kind or None}}
        parent = span.get("parent_span_id")
        if parent in unique_id_map:
            normalized["parent_id"] = unique_id_map[parent]
        if "input.value" in attributes:
            normalized["input"] = _content(attributes["input.value"], path + "/span_attributes/input.value", missing_null=True)
        if "output.value" in attributes:
            normalized["output"] = _content(attributes["output.value"], path + "/span_attributes/output.value", missing_null=True)
        duration = _duration_ms(span.get("duration"))
        if instant and origin and duration is not None:
            start_ms = (instant - origin).total_seconds() * 1000
            normalized["timing"] = {"clock_id": "trail-wall", "start_ms": start_ms, "end_ms": start_ms + duration,
                                    "duration_ms": duration, "duration_basis": "source_reported", "duration_scope": "elapsed"}
            timed_count += 1
        if kind == "model":
            model_count += 1
            counters = {
                "input_tokens": _token_count(attributes.get("llm.token_count.prompt"), path + "/span_attributes/llm.token_count.prompt"),
                "output_tokens": _token_count(attributes.get("llm.token_count.completion"), path + "/span_attributes/llm.token_count.completion"),
            }
            usage = None
            if any(value is not None for value in counters.values()):
                usage = {"accounting": "total", "completeness": "complete" if all(value is not None for value in counters.values()) else "partial",
                         "total": counters, "source_refs": [source_ref(path + "/span_attributes")]}
            normalized["call"] = {"invocation_id": normalized_id, "attempt": 1, "provider_request_id": None}
            normalized["model"] = {"provider": None, "requested_model": attributes.get("llm.model_name"),
                "response_model": attributes.get("llm.model_name"), "request_count": 1, "context_id": None, "usage": usage}
        elif kind == "tool":
            tool_count += 1
            name = str(attributes.get("tool.name") or span["span_name"])
            normalized["name"] = name
            normalized["tool"] = {"operation": operation(name), "call_id": normalized_id, "proposal_id": None}
        out["spans"].append(normalized)
        for field in ("events", "logs"):
            values = span.get(field) or []
            if not isinstance(values, list):
                reject("SOURCE_SCHEMA_INVALID", path + "/" + field, f"TRAIL {field} 必须是数组。", "保留原始 OTel 事件结构。")
            for event_index, value in enumerate(values):
                event_path = path + f"/{field}/{event_index}"
                out["events"].append({"id": f"trail-{field}-{index}-{event_index}", "type": "other", "segment_id": "segment-1",
                    "turn_id": "turn-query", "span_id": normalized["id"], "data": _content(value, event_path),
                    "source_refs": [source_ref(event_path)]})
    result.mapping.update(source_spans=len(flattened), model_spans=model_count, tool_spans=tool_count, timed_spans=timed_count)
    out["capture"]["coverage"].update(tools="partial", model_requests="partial", messages="partial", contexts="missing",
                                         timing="complete" if timed_count == len(flattened) else "partial")
    if any(count > 1 for count in totals.values()):
        result.issues.append(issue("DUPLICATE_SOURCE_SPAN_ID", "/trace/spans",
            "来源重复使用 span_id；已保留每个出现位置并生成唯一对象 ID。",
            "修复采集端 span_id 唯一性；重复 ID 的 parent 关系在来源消除歧义前保持未知。",
            classification="ambiguous"))


def _ctb(out, source, result):
    files = source["trace"]["files"]
    if not all(isinstance(item, dict) and isinstance(item.get("path"), str) and isinstance(item.get("content"), str) for item in files):
        reject("SOURCE_SCHEMA_INVALID", "/trace/files", "CTB files 必须包含字符串 path/content。", "保留嵌入文件原文。")
    trajectory = None
    trajectory_index = None
    debug_calls = []
    for index, item in enumerate(files):
        if item["path"].endswith(".traj.json"):
            try:
                candidate = json.loads(item["content"])
            except (ValueError, TypeError, RecursionError):
                reject("EMBEDDED_JSON_INVALID", f"/trace/files/{index}/content", "内嵌 trajectory JSON 无效。", "保留可独立解析的 mini-swe-agent trajectory。")
            if trajectory is not None:
                reject("EMBEDDED_TRAJECTORY_AMBIGUOUS", "/trace/files", "发现多个 trajectory 文件。", "每个 benchmark envelope 仅保留一个主轨迹。")
            trajectory, trajectory_index = candidate, index
        elif item["path"].endswith("/debug.json"):
            try:
                candidate = json.loads(item["content"])
            except (ValueError, TypeError, RecursionError):
                continue
            if isinstance(candidate, dict) and isinstance(candidate.get("messages"), list) and "original_response" in candidate:
                matched = re.search(r"/episode-(\d+)/debug\.json$", item["path"])
                sequence = int(matched.group(1)) if matched else index
                debug_calls.append((sequence, index, candidate))
    if trajectory is None and debug_calls:
        debug_calls.sort()
        for sequence, file_index, call in debug_calls:
            pointer = f"/trace/files/{file_index}/content"
            message_id = f"ctb-debug-message-{file_index}"
            span_id = f"ctb-debug-model-{file_index}"
            response = _content(call.get("original_response"), pointer, missing_null=True)
            out["messages"].append({"id": message_id, "role": "assistant", "turn_id": "turn-query",
                                    "content": response, "source_refs": [source_ref(pointer)]})
            out["spans"].append({
                "id": span_id, "kind": "model", "name": "LLM call", "agent_id": "root",
                "segment_id": "segment-1", "turn_id": "turn-query", "status": "unknown",
                "input": _content(call["messages"], pointer), "output": response,
                "call": {"invocation_id": str(call.get("litellm_call_id") or span_id), "attempt": 1,
                         "provider_request_id": None},
                "model": {"provider": call.get("custom_llm_provider"), "requested_model": call.get("model"),
                          "response_model": call.get("model"), "request_count": 1, "context_id": None,
                          "usage": None, "output_message_ids": [message_id]},
                "order": {"stream_id": "ctb-debug-calls", "sequence": sequence},
                "source_refs": [source_ref(pointer)],
            })
        result.mapping.update(source_files=len(files), embedded_model_calls=len(debug_calls),
                              projected_auxiliary_files=0)
        out["capture"]["coverage"].update(tools="unknown", model_requests="complete", messages="partial",
                                             contexts="partial", timing="missing")
        result.issues.append(issue("AUXILIARY_FILES_NOT_PROJECTED", "/trace/files",
            "辅助文件保留在不可变原件中；查询投影仅保留每次真实模型请求与响应。",
            "如需搜索其他附件，为对应文件类型增加显式投影规则。", classification="exact"))
        return
    if not isinstance(trajectory, dict) or trajectory.get("trajectory_format") != "mini-swe-agent-1" or not isinstance(trajectory.get("messages"), list):
        reject("SOURCE_SHAPE_UNSUPPORTED", "/trace/files", "CTB 仅支持 mini-swe-agent-1 内嵌轨迹。", "为新的 trajectory_format 注册独立版本。")
    pointer = f"/trace/files/{trajectory_index}/content"
    messages = trajectory["messages"]
    if not all(isinstance(message, dict) and isinstance(message.get("role"), str) and isinstance(message.get("content"), str) for message in messages):
        reject("SOURCE_SCHEMA_INVALID", pointer, "mini-swe-agent messages 必须含字符串 role/content。", "保留原始 trajectory messages。")
    current_turn = "turn-query"
    model_name = ((source.get("metadata") or {}).get("model") or (trajectory.get("info") or {}).get("model"))
    shell_count = 0
    for index, message in enumerate(messages):
        role = _role(message["role"])
        if role == "user" and message["content"] == source["query"] and index == next((i for i, item in enumerate(messages) if item.get("role") == "user"), -1):
            out["messages"][0]["source_refs"].append(source_ref(pointer))
            continue
        message_id = f"ctb-message-{index}"
        returncode = _RETURNCODE.search(message["content"])
        normalized_role = "tool" if role == "user" and returncode else role
        if normalized_role == "user":
            current_turn = f"ctb-turn-{index}"
            out["turns"].append({"id": current_turn, "input_message_ids": [message_id], "status": "unknown", "source_refs": [source_ref(pointer)]})
        content = _content(message["content"], pointer)
        normalized = {"id": message_id, "role": normalized_role, "turn_id": current_turn if normalized_role != "system" else None,
                      "content": content, "source_refs": [source_ref(pointer)]}
        if normalized_role == "assistant":
            _model(out, f"ctb-model-{index}", message_id, current_turn, content, pointer, model_name,
                   order={"stream_id": "ctb-messages", "sequence": index})
        elif normalized_role == "tool" and returncode:
            shell_count += 1
            previous = messages[index - 1] if index else None
            span_id = f"ctb-shell-{index}"
            out["spans"].append({"id": span_id, "kind": "tool", "name": "shell", "agent_id": "root", "segment_id": "segment-1",
                "turn_id": current_turn, "status": "ok" if int(returncode.group(1)) == 0 else "error",
                "tool": {"operation": "bash", "call_id": span_id, "proposal_id": None},
                "input": _content(previous["content"], pointer) if isinstance(previous, dict) and previous.get("role") == "assistant" else {"state": "missing"},
                "output": content, "order": {"stream_id": "ctb-shell", "sequence": shell_count - 1}, "source_refs": [source_ref(pointer)]})
            normalized["tool_span_id"] = span_id
        out["messages"].append(normalized)
    for index, item in enumerate(files):
        if index == trajectory_index:
            continue
        item_pointer = f"/trace/files/{index}/content"
        out.setdefault("artifacts", []).append({"id": f"ctb-file-{index}", "kind": "other",
            "content": _content(item["content"], item_pointer), "source_refs": [source_ref(item_pointer)]})
    result.mapping.update(source_files=len(files), embedded_messages=len(messages), model_responses=sum(s["kind"] == "model" for s in out["spans"]), shell_executions=shell_count)
    out["capture"]["coverage"].update(tools="partial", model_requests="partial", messages="complete", contexts="missing", timing="partial")
    result.issues.append(issue("EMBEDDED_JSON_POINTER_COARSE", pointer, "消息来自 JSON 字符串内嵌轨迹，source ref 定位到不可变文件正文并在 mapping 中保留消息序号。", "来源改为结构化 messages 后可提供逐消息 JSON Pointer。", classification="ambiguous"))


def convert(raw, source, binding):
    out = _envelope(raw, source, binding)
    variant = _variant(source["trace"])
    if variant is None:
        reject("SOURCE_SHAPE_UNSUPPORTED", "/trace", "benchmark trace 变体未注册。", "先定义稳定签名、事件边界和正文归属，再扩展 agent-benchmark/1。")
    result = Conversion(out)
    out["extensions"]["agent_benchmark.variant"] = variant
    dispatch = {"apb-messages": _apb, "rootse-steps": _rootse, "tel-raw-spans": _tel,
                "trail-otel": _trail, "ctb-embedded-files": _ctb}
    dispatch[variant](out, source, result)
    result.mapping = {"format": FORMAT, "adapter_version": ADAPTER_VERSION, "variant": variant, **result.mapping}
    result.issues.append(issue("REQUEST_CONTEXT_MISSING", "/contexts", "来源未明确标注每次模型调用的完整请求快照。", "补采实际 request payload；不从累计消息反推 Context。"))
    return result
