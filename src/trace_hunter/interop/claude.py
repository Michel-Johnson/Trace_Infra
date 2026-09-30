"""Direct Claude execution-export to Trace Hunter v2 draft adapter."""
import json
from collections import defaultdict

from .common import Conversion, base, content_ref, issue, reject, searchable_content, source_ref
from .native_common import claude_stream_usage, elapsed_ms, instant, timing, tool_value


def convert(raw, source, binding):
    meta, phases = source.get("meta"), source.get("phases")
    timeline, executions = source.get("timeline"), source.get("tool_calls")
    if not isinstance(meta, dict) or not isinstance(phases, list) or not isinstance(timeline, list) or not isinstance(executions, list):
        reject("SOURCE_SCHEMA_INVALID", "", "Claude 导出缺少 meta/phases/timeline/tool_calls。", "使用 Claude execution trace 原始导出，不要传入已归一化 Trace。")
    origin = instant(meta.get("started_local"))
    if origin is None:
        reject("SOURCE_TIME_INVALID", "/meta/started_local", "Claude 导出缺少合法起始时间。", "保留带时区的 started_local。")
    out = base(raw, binding, {"name": "Claude Code", "version": meta.get("version")}, "claude-export")
    result = Conversion(out)
    out["phases"] = []
    out["segments"][0]["session"] = {"namespace": "claude-export", "id": str(meta.get("session_id") or binding["run_id"])}
    out["clocks"] = [{"id": "claude-clock", "kind": "wall", "origin_at": origin.isoformat(), "uncertainty_ms": None}]
    out["extensions"]["claude.meta"] = {key: value for key, value in meta.items() if key not in ("prompt", "messages")}
    out["capture"]["coverage"].update(tools="partial", model_requests="partial", messages="partial", timing="partial")
    for index, phase in enumerate(phases):
        if not isinstance(phase, dict) or "id" not in phase:
            reject("PHASE_INVALID", f"/phases/{index}", "Claude phase 缺少身份。", "保留原始 phase id。")
        start, end = elapsed_ms(phase.get("start_local"), origin), elapsed_ms(phase.get("end_local"), origin)
        item = {"id": str(phase["id"]), "purpose": "export" if phase.get("purpose") == "export" or phase.get("id") == 3 else "task",
                "name": str(phase.get("name") or phase["id"]), "source_refs": [source_ref(f"/phases/{index}")]}
        measured = timing("claude-clock", start, end)
        if measured:
            item["timing"] = measured
        out["phases"].append(item)

    current_turn = None
    seen_user_rows = set()
    request_rows = defaultdict(list)
    proposal_rows = {}
    proposal_turns = {}
    for index, event in enumerate(timeline):
        if not isinstance(event, dict):
            reject("TIMELINE_EVENT_INVALID", f"/timeline/{index}", "Claude timeline 事件必须是对象。", "保留原始事件结构。")
        path = f"/timeline/{index}"
        kind = event.get("kind")
        if kind == "user_message":
            source_row = event.get("row", index)
            if source_row in seen_user_rows:
                result.issues.append(issue("DUPLICATE_USER_SNAPSHOT", path, "重复导出快照未生成新的用户轮次。", "原始记录保留在 source.json；按 row 核对来源。", classification="exact"))
                continue
            seen_user_rows.add(source_row)
            message_id = f"timeline-{index}-message"
            role = "user" if event.get("is_meta") is False else "system" if event.get("is_meta") is True else "other"
            if role == "user":
                current_turn = f"timeline-{index}-turn"
                out["turns"].append({"id": current_turn, "input_message_ids": [message_id], "status": "unknown", "source_refs": [source_ref(path)]})
            out["messages"].append({"id": message_id, "role": role, "turn_id": current_turn if role == "user" else None,
                                    "content": searchable_content(event.get("text"), path + "/text", missing_null=True), "source_refs": [source_ref(path)]})
        elif kind == "assistant_message":
            message_id = f"timeline-{index}-message"
            out["messages"].append({"id": message_id, "role": "assistant", "turn_id": current_turn,
                                    "content": searchable_content(event.get("text"), path + "/text", missing_null=True), "source_refs": [source_ref(path)]})
            if event.get("request_id"):
                request_rows[str(event["request_id"])].append((index, event, current_turn, message_id))
            calls = event.get("tool_uses") or []
            if not isinstance(calls, list):
                reject("TOOL_PROPOSALS_INVALID", path + "/tool_uses", "Claude tool_uses 必须是数组。", "保留原始 tool use 列表。")
            for call_index, call in enumerate(calls):
                cp = path + f"/tool_uses/{call_index}"
                if not isinstance(call, dict) or not call.get("tool_use_id") or not call.get("tool_name"):
                    reject("TOOL_PROPOSAL_INVALID", cp, "Claude tool_use 缺少 ID 或名称。", "保留 tool_use_id/tool_name/input。")
                call_id = str(call["tool_use_id"])
                signature = (call.get("tool_name"), json.dumps(call.get("input"), sort_keys=True, ensure_ascii=False))
                if call_id in proposal_rows and proposal_rows[call_id][0] != signature:
                    reject("TOOL_PROPOSAL_ID_CONFLICT", cp, "同一 Claude tool_use_id 内容冲突。", "检查重复片段或损坏导出。")
                proposal_rows.setdefault(call_id, (signature, cp, call, message_id, event.get("request_id")))
                proposal_turns[call_id] = current_turn

    model_ids = {}
    for request_index, (request_id, rows) in enumerate(request_rows.items()):
        first_index, first, turn_id, _ = rows[0]
        model_id = f"claude-model-{request_index}"
        model_ids[request_id] = model_id
        usages = {json.dumps(row.get("usage"), sort_keys=True) for _, row, _, _ in rows if row.get("usage") is not None}
        usage = claude_stream_usage(first.get("usage"), f"/timeline/{first_index}/usage") if len(usages) == 1 else None
        if len(usages) > 1:
            result.issues.append(issue("USAGE_CONFLICT", f"/timeline/{first_index}/usage", "同一请求片段的 usage 冲突。", "提供权威终态 usage；当前不取最大值或求和。", classification="ambiguous"))
        end_values = {elapsed_ms(row.get("ts_utc"), origin) for _, row, _, _ in rows if row.get("ts_utc")}
        span = {"id": model_id, "kind": "model", "name": "Claude model request", "agent_id": "root", "segment_id": "segment-1",
                "turn_id": turn_id, "phase_id": str(first.get("phase")) if first.get("phase") is not None else None,
                "parent_id": None, "status": "unknown", "call": {"invocation_id": request_id, "attempt": 1, "provider_request_id": request_id},
                "model": {"provider": "anthropic", "requested_model": meta.get("model"), "response_model": None,
                          "request_count": 1, "context_id": None, "output_message_ids": [row[3] for row in rows], "usage": usage},
                "source_refs": [source_ref(f"/timeline/{row[0]}") for row in rows]}
        if len(end_values) == 1:
            measured = timing("claude-clock", None, next(iter(end_values)))
            if measured:
                span["timing"] = measured
        out["spans"].append(span)

    proposals = {}
    for position, (call_id, (_, path, call, message_id, request_id)) in enumerate(proposal_rows.items()):
        proposal_id = f"claude-proposal-{position}"
        proposals[call_id] = proposal_id
        out["tool_calls"].append({"id": proposal_id, "call_id": call_id, "name": str(call["tool_name"]),
                                  "arguments": searchable_content(call.get("input"), path + "/input", missing_null=True),
                                  "model_span_id": model_ids.get(str(request_id)), "message_id": message_id, "source_refs": [source_ref(path)]})

    for index, call in enumerate(executions):
        path = f"/tool_calls/{index}"
        if not isinstance(call, dict) or not call.get("tool_use_id") or not call.get("tool_name"):
            reject("TOOL_EXECUTION_INVALID", path, "Claude tool execution 缺少 ID 或名称。", "保留 tool_use_id/tool_name/input/result。")
        call_id, name = str(call["tool_use_id"]), str(call["tool_name"])
        start, end = elapsed_ms(call.get("ts_utc"), origin), elapsed_ms(call.get("ended_ts_utc"), origin)
        span = {"id": call_id, "kind": "tool", "name": name, "agent_id": "root", "segment_id": "segment-1",
                "turn_id": proposal_turns.get(call_id), "phase_id": str(call.get("phase")) if call.get("phase") is not None else None,
                "parent_id": None, "status": "error" if call.get("is_error") else "ok",
                "tool": tool_value(name, call.get("input"), call_id, proposals.get(call_id)),
                "input": searchable_content(call.get("input"), path + "/input", missing_null=True),
                "output": searchable_content(call.get("result"), path + "/result", missing_null=True),
                "order": {"stream_id": "claude-tools", "sequence": index}, "source_refs": [source_ref(path)]}
        measured = timing("claude-clock", start, end, call.get("duration_s") * 1000 if isinstance(call.get("duration_s"), (int, float)) else None)
        if measured:
            span["timing"] = measured
        out["spans"].append(span)
        proposal = next((item for item in out["tool_calls"] if item["id"] == proposals.get(call_id)), None)
        if proposal and proposal.get("model_span_id"):
            out["links"].append({"from": proposal["model_span_id"], "to": call_id, "type": "invokes", "source_refs": [source_ref(path)]})

    result.mapping = {"source_timeline_events": len(timeline), "source_tool_executions": len(executions),
                      "model_requests": len(request_rows), "tool_proposals": len(proposals)}
    result.issues.append(issue("REQUEST_CONTEXT_MISSING", "/contexts", "Claude 导出未提供每次模型请求的完整 request body。", "补采原始请求上下文；聊天历史不自动等同于实际请求。"))
    return result
