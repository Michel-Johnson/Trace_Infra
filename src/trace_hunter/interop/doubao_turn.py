"""Adapter for Doubao evaluation exports shaped as case/raw_turn/calls."""
import hashlib
import json
import math
import re

from .common import Conversion, base, issue, reject, searchable_content, source_ref
from .native_common import instant, timing, tool_value


EXIT_CODE = re.compile(r"\bexited with code\s+(-?\d+)\b")
ERROR_RESULT = re.compile(r"^\s*ERROR(?:\s|:|$)", re.IGNORECASE)
METADATA_PAIR = re.compile(r"(?:^|\s)([^\s=]+)=([^\s]+)")
SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def _status(result):
    if not isinstance(result, str):
        return "unknown"
    matched = EXIT_CODE.search(result)
    if matched:
        return "ok" if int(matched.group(1)) == 0 else "error"
    return "error" if ERROR_RESULT.search(result) else "unknown"


def _run_metadata(calls):
    for index, call in enumerate(calls):
        if not isinstance(call, dict) or str(call.get("tool_name")).lower() != "context":
            continue
        command = call.get("call_content", call.get("command"))
        result = call.get("result")
        if not (isinstance(command, str) and command.endswith("record-run-metadata") and isinstance(result, str)):
            continue
        pairs = {matched.group(1): matched.group(2) for matched in METADATA_PAIR.finditer(result)}
        if pairs:
            return index, pairs
    return None, {}


def _metadata_value(metadata, suffix):
    matches = [value for key, value in metadata.items() if key == suffix or key.endswith("-" + suffix)]
    return matches[0] if len(matches) == 1 else None


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _span_id(call_id, index):
    identity = str(call_id).strip() if call_id is not None else ""
    basis = "source:" + identity if identity else f"index:{index}"
    return "doubao-turn-tool-" + hashlib.sha256(basis.encode()).hexdigest()[:24]


def convert(raw, source, binding):
    turn = source.get("raw_turn")
    if not isinstance(turn, dict) or not isinstance(turn.get("calls"), list):
        reject("SOURCE_SCHEMA_INVALID", "/raw_turn", "导出缺少 raw_turn.calls。", "保留 case/raw_turn/calls 原始结构。")
    if "user_prompt" in turn and turn["user_prompt"] is not None and not isinstance(turn["user_prompt"], str):
        reject("SOURCE_SCHEMA_INVALID", "/raw_turn/user_prompt", "user_prompt 必须是字符串或 null。", "保留原始用户输入文本。")

    calls = turn["calls"]
    out = base(raw, binding, {"name": "Doubao Work", "version": None}, "doubao-turn-export")
    result = Conversion(out)
    out["phases"] = [{"id": "task", "purpose": "task", "name": "任务执行",
                      "source_refs": [source_ref("/raw_turn")]}]
    metadata = {key: source[key] for key in ("case_id", "case_index", "domain", "query", "raw_trace_sha256", "source") if key in source}
    metadata["turn_index"] = turn.get("turn_index")
    metadata_index, run_metadata = _run_metadata(calls)
    if run_metadata:
        metadata["run_metadata"] = run_metadata
        metadata["run_metadata_source_ref"] = source_ref(f"/raw_turn/calls/{metadata_index}/result")
    out["extensions"]["doubao.turn_export"] = metadata

    declared_hash = source.get("raw_trace_sha256")
    source_file = source.get("source", {}).get("source_file") if isinstance(source.get("source"), dict) else None
    if isinstance(declared_hash, str) and SHA256.fullmatch(declared_hash) and isinstance(source_file, str) and source_file:
        out["sources"].append({"id": "raw-trace", "state": "external", "locator": source_file,
                               "media_type": "application/x-ndjson", "sha256": declared_hash})

    declared_status = _metadata_value(run_metadata, "status")
    if declared_status in ("completed", "failed", "cancelled", "unknown"):
        out["run"]["status"] = declared_status

    session_id = source.get("session_id")
    if session_id is None and isinstance(source.get("source"), dict):
        session_id = source["source"].get("source_session_index")
    if session_id is not None:
        out["segments"][0]["session"] = {"namespace": "doubao-turn-export", "id": str(session_id)}

    prompt = turn.get("user_prompt")
    turn_id = None
    if prompt is not None:
        turn_id = "doubao-turn"
        message_id = "doubao-turn-user"
        out["messages"].append({"id": message_id, "role": "user", "turn_id": turn_id,
                                "content": searchable_content(prompt, "/raw_turn/user_prompt"),
                                "source_refs": [source_ref("/raw_turn/user_prompt")]})
        out["turns"].append({"id": turn_id, "input_message_ids": [message_id], "status": "unknown",
                             "source_refs": [source_ref("/raw_turn")]})

    parsed_starts = {
        index: instant(call.get("start_time"))
        for index, call in enumerate(calls) if isinstance(call, dict)
    }
    valid_starts = [value for value in parsed_starts.values() if value is not None]
    origin = min(valid_starts) if valid_starts else None
    if origin is not None:
        out["clocks"] = [{"id": "doubao-turn-clock", "kind": "wall", "origin_at": origin.isoformat(),
                          "uncertainty_ms": None}]

    unique = []
    by_call_id = {}
    duplicate_refs = {}
    for index, call in enumerate(calls):
        path = f"/raw_turn/calls/{index}"
        if not isinstance(call, dict) or not isinstance(call.get("tool_name"), str) or not call["tool_name"].strip():
            reject("TOOL_EXECUTION_INVALID", path, "工具执行缺少 tool_name。", "保留每条执行的工具名和原始字段。")
        call_id = call.get("tool_call_id")
        explicit_id = str(call_id).strip() if call_id is not None else ""
        if explicit_id and explicit_id in by_call_id:
            previous_index, previous = by_call_id[explicit_id]
            if _canonical(previous) != _canonical(call):
                reject("TOOL_EXECUTION_ID_CONFLICT", path, "同一 tool_call_id 对应不同执行内容。", "修复导出器的调用身份；不要静默合并不同执行。")
            duplicate_refs.setdefault(previous_index, []).append(index)
            result.issues.append(issue("DUPLICATE_TOOL_EXECUTION_MERGED", path, "重复导出的同一次执行已合并。",
                                       "无需修改原文件；Span 保留全部来源位置。", classification="exact"))
            continue
        if explicit_id:
            by_call_id[explicit_id] = (index, call)
        unique.append((index, call, explicit_id))

    timed = 0
    statuses = {"ok": 0, "error": 0, "unknown": 0}
    for sequence, (index, call, explicit_id) in enumerate(unique):
        path = f"/raw_turn/calls/{index}"
        name = call["tool_name"]
        if call.get("call_content") is not None:
            arguments, input_pointer = call["call_content"], path + "/call_content"
        elif call.get("command") is not None:
            arguments, input_pointer = call["command"], path + "/command"
        else:
            arguments, input_pointer = None, path + "/call_content"
        output = call.get("result")
        output_pointer = path + "/result"
        call_id = explicit_id or f"source-call-{index}"
        refs = [source_ref(path)] + [source_ref(f"/raw_turn/calls/{other}") for other in duplicate_refs.get(index, [])]
        state = _status(output)
        statuses[state] += 1
        span = {"id": _span_id(explicit_id, index), "kind": "tool", "name": name,
                "agent_id": "root", "segment_id": "segment-1", "turn_id": turn_id,
                "phase_id": "task", "parent_id": None, "status": state,
                "tool": tool_value(name, arguments, call_id),
                "input": searchable_content(arguments, input_pointer, missing_null=True),
                "output": searchable_content(output, output_pointer, missing_null=True),
                "order": {"stream_id": "doubao-turn-calls", "sequence": sequence}, "source_refs": refs}
        point = parsed_starts.get(index)
        start = round((point - origin).total_seconds() * 1000, 6) if point and origin else None
        duration = call.get("duration_ms")
        if not _number(duration):
            duration = call.get("result_duration_ms")
        if not _number(duration):
            duration = None
        measured = timing("doubao-turn-clock" if origin is not None else None, start,
                          start + duration if start is not None and duration is not None else None, duration)
        if measured:
            span["timing"] = measured
            timed += 1
        out["spans"].append(span)

    out["capture"]["coverage"].update(
        tools="complete", model_requests="missing", contexts="missing",
        messages="partial" if prompt is not None else "missing",
        timing="complete" if unique and timed == len(unique) else "partial" if timed else "missing")
    result.mapping = {"variant": "case-raw-turn-calls", "source_tool_executions": len(calls),
                      "tool_executions": len(unique), "duplicates_merged": len(calls) - len(unique),
                      "timed_executions": timed, "statuses": statuses,
                      "run_metadata_fields": sorted(run_metadata)}
    result.issues += [
        issue("MODEL_REQUESTS_NOT_CAPTURED", "/spans", "该导出只有工具执行，没有可验证的模型请求边界。", "补采模型请求/响应事件后再生成 model span。"),
        issue("REQUEST_CONTEXT_MISSING", "/contexts", "该导出没有模型实际收到的 Context。", "补采真实 request payload；不能从聊天历史反推。"),
    ]
    return result
