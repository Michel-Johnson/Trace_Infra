"""Flat ATIF adapter primitives. Unsupported nested content stays in the archive."""
from .common import VERSION, Conversion, base, content_ref, issue, reject, searchable_content, source_ref


def convert(raw, source, binding):
    return convert_version(raw, source, binding, "ATIF-v1.7")


def convert_version(raw, source, binding, adapter, *, context_management=False, adapter_version=VERSION):
    agent, steps = source.get("agent"), source.get("steps")
    if not isinstance(agent, dict) or not isinstance(steps, list) or not isinstance(agent.get("name"), str):
        reject("SOURCE_SCHEMA_INVALID", "", "ATIF 需要 agent 和 steps。", f"使用 {adapter} 导出，或先运行来源自己的 validator。")
    out = base(raw, binding, {"name": agent["name"], "version": agent.get("version")}, adapter, adapter_version)
    result = Conversion(out)
    if source.get("session_id"):
        out["segments"][0]["session"] = {"namespace": adapter, "id": source["session_id"]}
    out["capture"]["coverage"].update(tools="partial", model_requests="partial", messages="partial")
    out["extensions"]["atif.metadata"] = {k: v for k, v in source.items() if k not in ("steps", "subagent_trajectories")}
    if source.get("subagent_trajectories") or source.get("continued_trajectory_ref"):
        result.issues.append(issue("EXTERNAL_OR_NESTED_TRAJECTORY_UNMAPPED", "/subagent_trajectories", "子代理或续接原文已归档，当前 flat adapter 不将它伪装成完整子图。", "增加对应的递归适配器；保持该运行采集范围为 partial。", classification="unsupported"))
    proposals, seen_steps, observed_calls, turn, sequence = {}, set(), set(), None, 0
    external_refs = []
    for index, step in enumerate(steps):
        path = f"/steps/{index}"
        if not isinstance(step, dict) or not isinstance(step.get("step_id"), int) or isinstance(step.get("step_id"), bool) or step["step_id"] < 1 or step["step_id"] in seen_steps:
            reject("STEP_ID_INVALID", path + "/step_id", "step_id 必须是正整数且在来源文档内唯一。", "保留真实步骤身份，修复重复步骤或选择正确的续接片段。")
        seen_steps.add(step["step_id"])
        if context_management and step["step_id"] != index + 1:
            reject("STEP_ID_NOT_SEQUENTIAL", path + "/step_id", "ATIF-v1.8 step_id 必须从 1 连续递增。", "恢复来源步骤顺序；不要重编号后冒充原始轨迹。")
        sid = f"step-{step['step_id']}"
        if step.get("is_copied_context"):
            out["events"].append({"id": sid + "-inherited", "type": "other", "segment_id": "segment-1", "data": content_ref(step, path), "source_refs": [source_ref(path)]})
            result.issues.append(issue("INHERITED_CONTEXT_EXCLUDED", path, "继承内容保留为来源事件，不产生新请求或消费。", "按父轨迹引用查看继承上下文。", classification="exact"))
            continue
        role = {"user": "user", "agent": "assistant", "system": "system"}.get(step.get("source"), "other")
        mid = sid + "-message"
        if role == "user":
            turn = sid + "-turn"
            out["turns"].append({"id": turn, "input_message_ids": [mid], "status": "unknown", "source_refs": [source_ref(path)]})
        message = {"id": mid, "role": role, "turn_id": turn, "content": searchable_content(step.get("message"), path + "/message") if "message" in step else {"state": "missing"}, "source_refs": [source_ref(path)]}
        out["messages"].append(message)
        model_id = None
        count = step.get("llm_call_count")
        if count is not None and (isinstance(count, bool) or not isinstance(count, int) or count < 0):
            reject("CALL_COUNT_INVALID", path + "/llm_call_count", "调用数必须为非负整数或 null。", "不知道时保留 null，不填 1。")
        if role == "assistant" and count != 0:
            model_id = sid + "-model"
            metrics = step.get("metrics") or {}
            if not isinstance(metrics, dict):
                reject("METRICS_INVALID", path + "/metrics", "metrics 必须是对象。", "修正来源 metrics 结构。")
            counters = {"input_tokens": metrics.get("prompt_tokens"), "output_tokens": metrics.get("completion_tokens"), "cache_read_tokens": metrics.get("cached_tokens")}
            usage = {"accounting": "total", "completeness": "complete" if all(counters[k] is not None for k in ("input_tokens", "output_tokens")) else "partial", "total": counters, "source_refs": [source_ref(path + "/metrics")]} if metrics else None
            s = {"id": model_id, "kind": "model" if count == 1 else "model_batch", "name": "ATIF agent step", "agent_id": "root", "segment_id": "segment-1", "turn_id": turn,
                 "status": "unknown", "output": message["content"], "source_refs": [source_ref(path)],
                 "model": {"provider": None, "requested_model": None, "response_model": step.get("model_name", agent.get("model_name")), "request_count": count, "context_id": None, "usage": usage},
                 "order": {"stream_id": "atif-steps", "sequence": index}}
            if context_management:
                token_evidence = {}
                for key in ("prompt_token_ids", "completion_token_ids", "logprobs"):
                    if key in metrics:
                        values = metrics[key]
                        if not isinstance(values, list):
                            reject("TOKEN_VECTOR_INVALID", path + "/metrics/" + key, "ATIF-v1.8 token 向量必须是数组。", "保留原始数组；未知时省略该字段。")
                        token_evidence[key] = {"count": len(values), "ref": source_ref(path + "/metrics/" + key)}
                if "cost_usd" in metrics:
                    token_evidence["cost_usd"] = {"value": metrics["cost_usd"], "ref": source_ref(path + "/metrics/cost_usd")}
                if token_evidence:
                    s["extensions"] = {"atif.metric_evidence": token_evidence}
            if count == 1:
                s["call"] = {"invocation_id": model_id, "attempt": 1, "provider_request_id": None}
            else:
                result.issues.append(issue("AGGREGATED_CALL_BOUNDARY", path + "/llm_call_count", "该 step 未提供单次请求边界，保留聚合记录。", "由原生 adapter 补充每次调用，或保持 request_count 未知。", classification="ambiguous"))
            out["spans"].append(s)
        if count == 0 and (step.get("metrics") is not None or step.get("reasoning_content") is not None):
            reject("DETERMINISTIC_STEP_HAS_MODEL_DATA", path, "零模型调用的确定性步骤不能带模型用量。", "按 ATIF 1.7 约束修正 llm_call_count 或提供正确来源。")
        calls = step.get("tool_calls") or []
        if not isinstance(calls, list):
            reject("TOOL_CALLS_INVALID", path + "/tool_calls", "tool_calls 需要数组。", "保留工具调用 ID、名字和参数。")
        for ci, call in enumerate(calls):
            cp = path + f"/tool_calls/{ci}"
            if not isinstance(call, dict) or not isinstance(call.get("tool_call_id"), str) or not isinstance(call.get("function_name"), str) or not isinstance(call.get("arguments"), dict):
                reject("TOOL_PROPOSAL_INVALID", cp, "工具提案缺少 ID、名称或参数对象。", "按原始 tool call 补全；不要生成执行结果。")
            external_id = call["tool_call_id"]
            if external_id in proposals:
                reject("TOOL_PROPOSAL_ID_CONFLICT", cp, "来源工具提案 ID 重复，无法可靠关联结果。", "检查 copied context 标记或嵌套轨迹作用域。")
            proposal = {"id": sid + f"-proposal-{ci}", "call_id": external_id, "name": call["function_name"], "arguments": searchable_content(call["arguments"], cp + "/arguments"), "message_id": mid, "model_span_id": model_id, "source_refs": [source_ref(cp)]}
            out["tool_calls"].append(proposal)
            proposals[external_id] = (proposal, cp, call)
        observations = (step.get("observation") or {}).get("results", [])
        if not isinstance(observations, list):
            reject("OBSERVATIONS_INVALID", path + "/observation", "observation.results 需要数组。", "保留每个 source_call_id 与对应结果。")
        for oi, observation in enumerate(observations):
            op = path + f"/observation/results/{oi}"
            if not isinstance(observation, dict):
                reject("OBSERVATION_INVALID", op, "工具结果需要对象。", "使用 ATIF observation result 结构。")
            delegated = observation.get("subagent_trajectory_ref")
            if context_management and delegated is not None:
                if not isinstance(delegated, list):
                    reject("SUBAGENT_REFS_INVALID", op + "/subagent_trajectory_ref", "ATIF-v1.8 子轨迹引用必须是数组。", "保留 trajectory_path 或 trajectory_id，不以内嵌正文代替引用。")
                for ri, ref in enumerate(delegated):
                    rp = op + f"/subagent_trajectory_ref/{ri}"
                    if not isinstance(ref, dict) or not isinstance(ref.get("trajectory_path"), str) or not ref["trajectory_path"]:
                        reject("EXTERNAL_SUBAGENT_REF_INVALID", rp, "外部子轨迹引用缺少 trajectory_path。", "提供可解析的 trajectory_path；session_id 不能代替文档引用。")
                    source_id = f"external-step-{step['step_id']}-{oi}-{ri}"
                    out["sources"].append({"id": source_id, "state": "missing", "locator": ref["trajectory_path"], "media_type": "application/json", "sha256": None})
                    external_refs.append({"source_id": source_id, "session_id": ref.get("session_id"), "trajectory_path": ref["trajectory_path"],
                                          "summary": (ref.get("extra") or {}).get("summary") if isinstance(ref.get("extra") or {}, dict) else None,
                                          "source_ref": source_ref(rp)})
                continue
            matched = proposals.get(observation.get("source_call_id"))
            if not matched:
                out["events"].append({"id": sid + f"-observation-{oi}", "type": "other", "segment_id": "segment-1", "data": content_ref(observation, op), "source_refs": [source_ref(op)]})
                result.issues.append(issue("OBSERVATION_UNLINKED", op, "未关联结果保留为事件，没有虚构工具执行。", "补充 source_call_id 或原生执行记录。", classification="ambiguous"))
                continue
            proposal, cp, call = matched
            if proposal["id"] in observed_calls:
                reject("OBSERVATION_MULTIPLICITY_AMBIGUOUS", op, "同一提案关联多个结果，无法区分分片和多次执行。", "补充原生执行 ID/attempt，或使用该来源的流式 adapter；不要把结果条数当执行次数。")
            observed_calls.add(proposal["id"])
            tool_id = sid + f"-execution-{oi}"
            name = proposal["name"]
            tool = {"operation": {"Read": "read", "Write": "write", "Edit": "write", "Bash": "bash", "bash_command": "bash", "AskUserQuestion": "human"}.get(name, "other"), "call_id": proposal["call_id"], "proposal_id": proposal["id"]}
            if name == "Skill":
                tool["skill"] = {"name": call["arguments"].get("skill"), "action": "invoke"}
            elif name == "Read":
                parts = str(call["arguments"].get("file_path", call["arguments"].get("path", ""))).replace("\\", "/").split("/")
                if parts[-1] == "SKILL.md":
                    tool["skill"] = {"name": parts[-2] if len(parts) > 1 and parts[-2] else None, "action": "load"}
            s = {"id": tool_id, "kind": "tool", "name": name, "agent_id": "root", "segment_id": "segment-1", "turn_id": turn,
                 "status": "unknown", "tool": tool, "input": proposal["arguments"], "output": searchable_content(observation.get("content"), op + "/content") if "content" in observation else {"state": "missing"},
                 "source_refs": [source_ref(cp), source_ref(op)], "order": {"stream_id": "atif-results", "sequence": sequence}}
            sequence += 1
            out["spans"].append(s)
            if proposal.get("model_span_id"):
                out["links"].append({"from": proposal["model_span_id"], "to": tool_id, "type": "invokes", "source_refs": [source_ref(cp), source_ref(op)]})
            out["messages"].append({"id": tool_id + "-result", "role": "tool", "turn_id": turn, "tool_span_id": tool_id, "content": s["output"], "source_refs": [source_ref(op)]})
        context = step.get("extra", {}).get("context_management") if isinstance(step.get("extra"), dict) else None
        if context_management and isinstance(context, dict):
            if context.get("type") != "compaction" or context.get("boundary") != "replace":
                reject("CONTEXT_BOUNDARY_UNSUPPORTED", path + "/extra/context_management", "仅支持证据明确的 replace compaction 边界。", "为新的 context 管理语义增加独立映射，不按字段相似度套用。")
            refs = [source_ref(path)] + [{"source_id": item["source_id"], "pointer": ""} for item in external_refs if item["source_ref"]["pointer"].startswith(path + "/")]
            out["events"].append({"id": sid + "-compaction", "type": "compaction", "segment_id": "segment-1",
                                  "context_change": {"before": None, "after": None,
                                                     "summary": searchable_content(step.get("message"), path + "/message")},
                                  "data": content_ref(step, path), "source_refs": refs,
                                  "order": {"stream_id": "atif-steps", "sequence": index}})
        elif role in ("system", "other") or step.get("timestamp"):
            out["events"].append({"id": sid + "-source-event", "type": "other", "segment_id": "segment-1", "data": content_ref(step, path), "source_refs": [source_ref(path)]})
    if external_refs:
        out["extensions"]["atif.external_trajectory_refs"] = external_refs
        result.issues.append(issue("EXTERNAL_SUBAGENT_TRAJECTORY_UNREAD", "/steps", "子代理轨迹仅以外部路径被引用，本次输入未提供其字节。", "单独提供引用文件并验证哈希后再递归导入；当前不推断其对象和 token。", classification="unsupported"))
    model_spans = [span for span in out["spans"] if span["kind"] in ("model", "model_batch")]
    step_totals = {
        "prompt_tokens": sum((span["model"].get("usage") or {}).get("total", {}).get("input_tokens") or 0 for span in model_spans),
        "completion_tokens": sum((span["model"].get("usage") or {}).get("total", {}).get("output_tokens") or 0 for span in model_spans),
    }
    final_metrics = source.get("final_metrics")
    if context_management and isinstance(final_metrics, dict) and any(
        final_metrics.get("total_" + key) is not None and final_metrics.get("total_" + key) != value
        for key, value in step_totals.items()
    ):
        result.issues.append(issue("FINAL_METRICS_SCOPE_DIFFERS", "/final_metrics", "最终总量与当前轨迹可归属的逐步用量不同，未把差额分配给外部子轨迹。", "提供子轨迹字节后按其独立请求边界核对总量。", classification="ambiguous"))
    result.mapping = {"source_steps": len(steps), "tool_proposals": len(out["tool_calls"]), "tool_executions_with_results": sum(s["kind"] == "tool" for s in out["spans"]),
                      "model_steps": len(model_spans), "step_usage_totals": step_totals, "external_subagent_refs": len(external_refs),
                      "compactions": sum(event["type"] == "compaction" for event in out["events"])}
    result.issues += [issue("EXECUTION_TIME_MISSING", "/spans", "step.timestamp 已保留在来源事件，没有用相邻时间填造工具耗时。", "补采执行起止时间与时钟域。"),
                      issue("REQUEST_CONTEXT_MISSING", "/contexts", "没有当次完整请求快照，聊天历史不自动成为模型实际输入。", "采集实际 request payload、工具定义及参数。")]
    return result
