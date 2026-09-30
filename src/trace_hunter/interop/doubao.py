"""Direct Doubao runtime/sqlite export to Trace Hunter v2 draft adapter."""
from .common import Conversion, base, content_ref, issue, reject, searchable_content, source_ref
from .native_common import elapsed_ms, instant, timing, tool_value


def _is_real_user(message):
    if message.get("role") != "user" or message.get("is_meta") or message.get("isCompactSummary"):
        return False
    content = message.get("content")
    return not (isinstance(content, list) and content and all(isinstance(item, dict) and item.get("type") == "tool_result" for item in content))


def convert(raw, source, binding):
    manifest = source.get("manifest")
    if not isinstance(manifest, dict) or not isinstance(source.get("tool_calls", []), list):
        reject("SOURCE_SCHEMA_INVALID", "", "豆包导出缺少 manifest/tool_calls。", "使用原始 trajectory 或 sqlite trajectory 导出。")
    runtime_calls = source.get("runtime_tool_calls") or []
    messages = source.get("native_messages") or []
    if not isinstance(runtime_calls, list) or not isinstance(messages, list) or ("native_tool_calls" in source and not isinstance(source["native_tool_calls"], list)):
        reject("SOURCE_SCHEMA_INVALID", "", "豆包消息和工具执行集合必须是数组。", "保留 native_messages/runtime_tool_calls/native_tool_calls 原始数组。")
    origin = instant(manifest.get("actual_submission_observed_at"))
    if origin is None:
        reject("SOURCE_TIME_INVALID", "/manifest/actual_submission_observed_at", "豆包导出缺少合法提交时间。", "保留带时区的提交观察时间。")
    out = base(raw, binding, {"name": "Doubao Work", "version": manifest.get("client_version")}, "doubao-export")
    result = Conversion(out)
    out["phases"] = []
    out["segments"][0]["session"] = {"namespace": "doubao-export", "id": str(manifest.get("conversation_id") or binding["run_id"])}
    out["clocks"] = [{"id": "doubao-clock", "kind": "wall", "origin_at": origin.isoformat(), "uncertainty_ms": None}]
    out["extensions"]["doubao.manifest"] = dict(manifest)
    out["capture"]["coverage"].update(tools="partial", model_requests="missing", messages="partial", timing="partial")
    sqlite = isinstance(source.get("native_tool_calls"), list)
    end_value = manifest.get("native_final_file_mtime") if sqlite else manifest.get("completed_observed_at")
    phase = {"id": "task", "purpose": "task", "name": "任务执行", "source_refs": [source_ref("/manifest")]}
    measured = timing("doubao-clock", 0, elapsed_ms(end_value, origin))
    if measured:
        phase["timing"] = measured
    out["phases"].append(phase)

    current_turn = None
    message_ids = {}
    for index, message in enumerate(messages):
        path = f"/native_messages/{index}"
        if not isinstance(message, dict):
            reject("MESSAGE_INVALID", path, "豆包 native message 必须是对象。", "保留原始消息结构。")
        role = message.get("role") if message.get("role") in ("system", "developer", "user", "assistant", "tool") else "other"
        message_id = f"doubao-message-{index}"
        if _is_real_user(message):
            current_turn = f"doubao-turn-{index}"
            out["turns"].append({"id": current_turn, "input_message_ids": [message_id], "status": "unknown", "source_refs": [source_ref(path)]})
        out["messages"].append({"id": message_id, "role": role, "turn_id": current_turn,
                                "content": searchable_content(message.get("content"), path + "/content", missing_null=True), "source_refs": [source_ref(path)]})
        message_ids[index] = (message_id, current_turn)

    proposals = {}
    native_calls = source.get("tool_calls") or []
    for index, call in enumerate(native_calls):
        path = f"/tool_calls/{index}"
        if not isinstance(call, dict) or not call.get("tool_call_id") or not call.get("name"):
            reject("TOOL_PROPOSAL_INVALID", path, "豆包 tool call 缺少 ID 或名称。", "保留 tool_call_id/name/arguments。")
        call_id, proposal_id = str(call["tool_call_id"]), f"doubao-proposal-{index}"
        if call_id in proposals:
            reject("TOOL_PROPOSAL_ID_CONFLICT", path, "豆包 tool_call_id 重复。", "检查重复导出记录。")
        message = message_ids.get(call.get("native_message_index"), (None, None))
        out["tool_calls"].append({"id": proposal_id, "call_id": call_id, "name": str(call["name"]),
                                  "arguments": searchable_content(call.get("arguments"), path + "/arguments", missing_null=True),
                                  "model_span_id": None, "message_id": message[0], "source_refs": [source_ref(path)]})
        proposals[call_id] = (proposal_id, index, call, message[1])

    runtime_by_id = {str(item.get("runtime_id")): item for item in runtime_calls if isinstance(item, dict) and item.get("runtime_id")}
    executions = []
    consumed = set()
    if sqlite:
        for index, call in enumerate(source["native_tool_calls"]):
            matched = call.get("runtime_match")
            if isinstance(matched, dict):
                runtime = matched
            elif isinstance(matched, int) and not isinstance(matched, bool) and 0 <= matched < len(runtime_calls):
                runtime = runtime_calls[matched]
            else:
                runtime = runtime_by_id.get(str(matched), {})
            if not runtime and call.get("name") == "Read" and len(runtime_by_id) == 1:
                runtime = next(iter(runtime_by_id.values()))
            executions.append(("native_tool_calls", index, call, runtime, str(call.get("id") or f"native-{index}"), call.get("name")))
            consumed.add(str(call.get("id")))
    else:
        for index, runtime in enumerate(runtime_calls):
            call_id = str(runtime.get("native_tool_call_id")) if runtime.get("native_tool_call_id") is not None else None
            native = proposals.get(call_id, (None, None, {}, None))[2] if call_id else {}
            executions.append(("runtime_tool_calls", index, native, runtime, str(runtime.get("runtime_id") or f"runtime-{index}"), runtime.get("name") or native.get("name")))
            if call_id:
                consumed.add(call_id)
        for call_id, (_, index, call, _) in proposals.items():
            if call_id not in consumed:
                has_result = call.get("result") is not None or isinstance(call.get("native_result"), dict)
                has_runtime = isinstance(call.get("timing"), dict) and any(call["timing"].get(key) is not None for key in ("started_at", "ended_at", "duration_ms"))
                if has_result or has_runtime:
                    executions.append(("tool_calls", index, call, {}, call_id, call.get("name")))
                else:
                    result.issues.append(issue("TOOL_PROPOSAL_NOT_EXECUTED", f"/tool_calls/{index}", "工具提案没有执行证据，未生成 Span。", "若实际执行，请补充 runtime start、timing 或结果。", classification="exact"))

    for sequence, (collection, index, call, runtime, span_id, name) in enumerate(executions):
        path = f"/{collection}/{index}"
        arguments = call.get("arguments") if isinstance(call, dict) else None
        if arguments is None and isinstance(runtime, dict):
            arguments = runtime.get("command") or runtime.get("input_path_metadata")
        call_id = str(call.get("tool_call_id")) if isinstance(call, dict) and call.get("tool_call_id") is not None else span_id
        proposal = proposals.get(call_id)
        time_value = runtime.get("timing", {}) if isinstance(runtime, dict) else {}
        if not time_value and isinstance(call, dict):
            time_value = call.get("timing") or {}
        start, end = elapsed_ms(time_value.get("started_at"), origin), elapsed_ms(time_value.get("ended_at"), origin)
        duration = time_value.get("duration_ms")
        output = None
        if isinstance(call, dict):
            output = call.get("result")
            if output is None and isinstance(call.get("native_result"), dict):
                output = call["native_result"].get("content")
        input_pointer = path + ("/arguments" if collection != "runtime_tool_calls" else "/command")
        output_pointer = path + "/result"
        refs = [source_ref(path)]
        if collection == "runtime_tool_calls" and proposal:
            native_path = f"/tool_calls/{proposal[1]}"
            input_pointer = native_path + "/arguments"
            output_pointer = native_path + "/native_result/content"
            refs.append(source_ref(native_path))
        elif collection == "tool_calls" and isinstance(call.get("native_result"), dict) and output is not None:
            output_pointer = path + "/native_result/content"
        span = {"id": span_id, "kind": "tool", "name": str(name or "unknown"), "agent_id": "root", "segment_id": "segment-1",
                "turn_id": proposal[3] if proposal else None, "phase_id": "task", "parent_id": None,
                "status": "error" if (isinstance(call, dict) and call.get("is_error")) else "unknown",
                "tool": tool_value(name, arguments, call_id, proposal[0] if proposal else None),
                "input": searchable_content(arguments, input_pointer, missing_null=True),
                "output": searchable_content(output, output_pointer, missing_null=True),
                "order": {"stream_id": "doubao-tools", "sequence": sequence}, "source_refs": refs}
        measured = timing("doubao-clock", start, end, duration)
        if measured:
            span["timing"] = measured
        out["spans"].append(span)

    result.mapping = {"source_tool_proposals": len(native_calls), "tool_executions": len(executions),
                      "native_messages": len(messages), "variant": "sqlite" if sqlite else "runtime"}
    result.issues += [issue("MODEL_REQUESTS_NOT_CAPTURED", "/spans", "豆包导出未提供可验证的单次模型请求边界。", "补采原始请求/响应事件后再生成 model span。"),
                      issue("REQUEST_CONTEXT_MISSING", "/contexts", "豆包导出未提供完整请求上下文。", "补采实际 request payload、工具定义和模型参数。")]
    return result
