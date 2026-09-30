"""Turn-scoped Claude Code stream export to the canonical Trace fact model."""

import json
import hashlib
import re

from .interop.claude import convert as convert_claude_export
from .interop.common import searchable_content, source_ref
from .interop.native_common import claude_stream_usage, elapsed_ms, instant, timing, tool_value


def _text(blocks):
    if isinstance(blocks, str):
        return blocks
    if not isinstance(blocks, list):
        return None
    parts = [block.get("text") for block in blocks
             if isinstance(block, dict) and block.get("type") == "text"
             and isinstance(block.get("text"), str)]
    return "\n".join(parts) if parts else None


def _cli_error(tool_name, arguments, result):
    """Recognize only our CLI's structured first-line failure envelope."""
    if str(tool_name).lower() != "bash" or not isinstance(arguments, dict):
        return None
    command = arguments.get("command")
    if not isinstance(command, str) or "trace_hunter_cli.py" not in command:
        return None
    output = _text(result)
    if not isinstance(output, str):
        return None
    lines = output.splitlines()
    first = lines[0] if lines else ""
    if re.fullmatch(r"Exit code [1-9][0-9]*", first):
        first = lines[1] if len(lines) > 1 else ""
    if len(first) > 16384 or not first.startswith("{"):
        return None
    try:
        value = json.loads(first)
    except ValueError:
        return None
    if (not isinstance(value, dict) or set(value) != {"error", "status", "details"} or
            not isinstance(value["error"], str) or not value["error"] or
            (value["status"] is not None and type(value["status"]) is not int) or
            not isinstance(value["details"], list)):
        return None
    return value


def _link_body_contexts(document, raw, sources):
    models = [span for span in document["spans"] if span["kind"] == "model"]
    by_message = {(span.get("call") or {}).get("invocation_id"): span for span in models}
    by_uuid = {item["value"].get("uuid"): (item["value"].get("message") or {}).get("id")
               for item in raw.get("events") or []
               if isinstance(item, dict) and isinstance(item.get("value"), dict)}
    for entry in raw.get("body_index") or []:
        if not isinstance(entry, dict):
            continue
        request = entry.get("request_source_id")
        response = entry.get("response_source_id")
        if request not in sources:
            continue
        span = by_message.get(str(entry.get("message_id") or ""))
        if span is None:
            span = by_message.get(by_uuid.get(entry.get("message_uuid")))
        if span is None or span["model"].get("context_id"):
            continue
        pointer = {"source_id": request, "pointer": ""}
        context_id = "context-" + hashlib.sha256(request.encode()).hexdigest()[:24]
        preview = sources[request].get("search_preview")
        content = {"state": "complete", "ref": pointer}
        if isinstance(preview, str):
            content["search_text"] = preview
            content["search_text_state"] = (
                "exact" if sources[request]["content_ref"]["size_bytes"] <= 4096 else "truncated")
        document["contexts"].append({"id": context_id, "message_ids": [],
                                     "request": content, "source_refs": [pointer],
                                     "attributes": {"capture_basis": "actual_api_request"}})
        span["model"]["context_id"] = context_id
        span["source_refs"].append(pointer)
        if response in sources:
            span["source_refs"].append({"source_id": response, "pointer": ""})
        if entry.get("request_id"):
            span["call"]["provider_request_id"] = str(entry["request_id"])
    if models:
        covered = sum(bool(span["model"].get("context_id")) for span in models)
        document["capture"]["coverage"]["contexts"] = (
            "complete" if covered == len(models) and not (raw.get("recorder") or {}).get("issues") else
            "partial" if covered else "missing")


def _attach_telemetry(document, raw, origin):
    models = [span for span in document["spans"] if span["kind"] == "model"]
    by_provider = {(span.get("call") or {}).get("provider_request_id"): span
                   for span in models if (span.get("call") or {}).get("provider_request_id")}
    responses, requests, usage_logs = [], [], []
    directly_linked_usage = set()
    for index, item in enumerate(raw.get("otlp") or []):
        value = item.get("value") if isinstance(item, dict) else None
        if not isinstance(value, dict):
            continue
        for resource in value.get("resourceLogs") or []:
            for scope in resource.get("scopeLogs") or []:
                for record in scope.get("logRecords") or []:
                    attrs = {attribute.get("key"): next(iter(attribute.get("value", {}).values()), None)
                             for attribute in record.get("attributes") or [] if isinstance(attribute, dict)}
                    if attrs.get("event.name") == "api_response_body" and attrs.get("message.id"):
                        try:
                            responses.append((index, str(attrs["message.id"]), int(record["timeUnixNano"])))
                        except (KeyError, TypeError, ValueError, OverflowError):
                            pass
                    if attrs.get("event.name") != "api_request":
                        continue
                    try:
                        usage_logs.append((index, int(record["timeUnixNano"]), attrs))
                        usage_position = len(usage_logs) - 1
                    except (KeyError, TypeError, ValueError, OverflowError):
                        usage_position = None
                    span = by_provider.get(attrs.get("request_id"))
                    if span is None:
                        continue
                    if usage_position is not None:
                        directly_linked_usage.add(usage_position)
                    duration = attrs.get("duration_ms")
                    if duration is not None and record.get("timeUnixNano"):
                        try:
                            duration = float(duration)
                            at = int(record["timeUnixNano"]) / 1_000_000 - origin.timestamp() * 1000
                            measured = timing("claude-clock", at - duration, at) if at >= duration >= 0 else None
                            if measured:
                                span["timing"] = measured
                        except (TypeError, ValueError, OverflowError):
                            pass
                    span["source_refs"].append(source_ref(f"/otlp/{index}"))
                    if span["model"].get("usage") is None:
                        span["model"]["usage"] = _telemetry_usage(attrs, index)
        for resource in value.get("resourceSpans") or []:
            for scope in resource.get("scopeSpans") or []:
                for otel_span in scope.get("spans") or []:
                    attrs = {attribute.get("key"): next(iter(attribute.get("value", {}).values()), None)
                             for attribute in otel_span.get("attributes") or [] if isinstance(attribute, dict)}
                    if attrs.get("span.type") != "llm_request":
                        continue
                    try:
                        start, end = int(otel_span["startTimeUnixNano"]), int(otel_span["endTimeUnixNano"])
                    except (KeyError, TypeError, ValueError, OverflowError):
                        continue
                    if 0 < start <= end:
                        requests.append((index, start, end))
    # Claude 2.1.274 omits request_id on api_request. Its usage log and the
    # response-body log can differ by a millisecond even for one request.
    # Only associate logs when the match is unique in BOTH directions within
    # this small clock window. Nearby parallel requests remain unknown.
    usage_match_window_ns = 5_000_000
    response_matches = [[position for position, (_, request_at, _) in enumerate(usage_logs)
                         if abs(request_at - response_at) <= usage_match_window_ns]
                        for _, _, response_at in responses]
    usage_match_counts = [0] * len(usage_logs)
    for matches in response_matches:
        for position in matches:
            usage_match_counts[position] += 1
    for response_position in sorted(range(len(responses)), key=lambda index: responses[index][2]):
        log_index, message_id, _ = responses[response_position]
        span = by_provider.get(message_id)
        matches = response_matches[response_position]
        if (span is None or span["model"].get("usage") is not None or
                len(matches) != 1 or usage_match_counts[matches[0]] != 1 or
                matches[0] in directly_linked_usage):
            continue
        position = matches[0]
        source_index, _, attrs = usage_logs[position]
        span["model"]["usage"] = _telemetry_usage(attrs, source_index)
        if span["model"].get("usage") is not None:
            span["source_refs"].extend((source_ref(f"/otlp/{source_index}"),
                                        source_ref(f"/otlp/{log_index}")))
    # Claude 2.1.274 omits request_id from api_request events. Its response-body
    # log carries message.id, which is the stream model invocation ID, and the
    # llm_request Span ends at nearly the same instant. Require a unique, close,
    # one-to-one match; do not infer timing for ambiguous parallel requests.
    unused = set(range(len(requests)))
    for log_index, message_id, response_at in sorted(responses, key=lambda item: item[2]):
        span = by_provider.get(message_id)
        if span is None or span.get("timing", {}).get("start_ms") is not None:
            continue
        candidates = sorted((abs(requests[position][2] - response_at), position)
                            for position in unused
                            if abs(requests[position][2] - response_at) <= 2_000_000_000)
        if not candidates or (len(candidates) > 1 and
                              candidates[1][0] - candidates[0][0] < 50_000_000):
            continue
        _, position = candidates[0]
        trace_index, start, end = requests[position]
        measured = timing("claude-clock", start / 1_000_000 - origin.timestamp() * 1000,
                          end / 1_000_000 - origin.timestamp() * 1000)
        if measured:
            span["timing"] = measured
            span["source_refs"].extend((source_ref(f"/otlp/{trace_index}"),
                                        source_ref(f"/otlp/{log_index}")))
            unused.remove(position)
    if models and all((span.get("timing") or {}).get("start_ms") is not None and
                      (span.get("timing") or {}).get("end_ms") is not None
                      for span in document["spans"] if span.get("kind") in ("model", "tool", "agent")):
        document["capture"]["coverage"]["timing"] = "complete"


def _telemetry_usage(attrs, index):
    values = {}
    for key in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens"):
        value = attrs.get(key)
        if (type(value) is int and value >= 0) or (isinstance(value, str) and value.isdecimal()):
            values[key] = int(value)
    return claude_stream_usage(values, f"/otlp/{index}")


def _unified_stream_order(document, raw):
    """Order root model/tool facts by observed Claude events, not by kind."""
    models, proposals, results = {}, {}, {}
    for index, entry in enumerate(raw.get("events") or []):
        event = entry.get("value") if isinstance(entry, dict) else None
        if not isinstance(event, dict):
            continue
        message = event.get("message") or {}
        if event.get("type") == "assistant":
            if isinstance(message.get("id"), str):
                models.setdefault(message["id"], index)
            for block in message.get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("id"):
                    proposals.setdefault(str(block["id"]), index)
        elif event.get("type") == "user":
            for block in message.get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_result" and block.get("tool_use_id"):
                    results.setdefault(str(block["tool_use_id"]), index)
    known, unknown = [], []
    for original, span in enumerate(document["spans"]):
        position = None
        if span.get("agent_id") == "root" and span["kind"] == "model":
            position = models.get((span.get("call") or {}).get("invocation_id"))
        elif span.get("agent_id") == "root" and span["kind"] == "tool":
            call_id = (span.get("tool") or {}).get("call_id")
            position = results.get(call_id, proposals.get(call_id))
        if position is None:
            unknown.append(span)
        else:
            known.append((position, 0 if span["kind"] == "model" else 1, original, span))
    known.sort(key=lambda row: row[:3])
    for sequence, (_, _, _, span) in enumerate(known):
        span["order"] = {"stream_id": "claude-events", "sequence": sequence}
    if unknown:
        document["capture"].setdefault("notes", []).append(
            f"{len(unknown)} spans lack a comparable Claude stream position; their order is unknown.")
    document["spans"] = [row[3] for row in known] + unknown


def prepare(raw_bytes):
    """Archive the exact document whose JSON pointers appear in the Trace."""
    raw = json.loads(raw_bytes)
    events = raw.get("events", [])
    if not isinstance(events, list) or not raw.get("session_id") or not raw.get("turn_id"):
        raise ValueError("Claude turn capture requires session_id, turn_id and events")
    start = raw["started_at"]
    meta = {"started_local": start, "session_id": raw["session_id"],
            "model": raw.get("model"), "version": raw.get("claude_version")}
    timeline = [{"kind": "user_message", "is_meta": False,
                 "text": raw.get("prompt"), "ts_utc": start}]
    proposals, executions = {}, []
    seen_assistant = {}
    for position, entry in enumerate(events):
        if not isinstance(entry, dict) or not isinstance(entry.get("value"), dict):
            continue
        event, at = entry["value"], entry.get("at") or start
        if event.get("type") == "system" and event.get("subtype") == "init":
            meta["model"] = event.get("model") or meta["model"]
            meta["version"] = event.get("claude_code_version") or meta["version"]
        if event.get("type") == "assistant":
            message = event.get("message") or {}
            identifier = message.get("id") or event.get("uuid") or str(position)
            if identifier in seen_assistant:
                earlier = seen_assistant[identifier]
                if message.get("usage") is not None:
                    earlier["usage"] = message["usage"]
                latest_text = _text(message.get("content"))
                if latest_text and len(latest_text) > len(earlier.get("text") or ""):
                    earlier["text"] = latest_text
                known = {item["tool_use_id"] for item in earlier["tool_uses"]}
                for block in message.get("content") or []:
                    if (isinstance(block, dict) and block.get("type") == "tool_use" and
                            block.get("id") and str(block["id"]) not in known):
                        use = {"tool_use_id": str(block["id"]),
                               "tool_name": str(block.get("name") or "unknown"),
                               "input": block.get("input")}
                        proposals[use["tool_use_id"]] = {**use, "at": at}
                        earlier["tool_uses"].append(use)
                        known.add(use["tool_use_id"])
                continue
            blocks = message.get("content") or []
            uses = []
            for block in blocks if isinstance(blocks, list) else []:
                if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("id"):
                    use = {"tool_use_id": str(block["id"]), "tool_name": str(block.get("name") or "unknown"),
                           "input": block.get("input")}
                    proposals[use["tool_use_id"]] = {**use, "at": at}
                    uses.append(use)
            assistant = {"kind": "assistant_message", "text": _text(blocks),
                         "tool_uses": uses, "request_id": message.get("id") or identifier,
                         "usage": message.get("usage"), "ts_utc": at,
                         "parent_tool_use_id": event.get("parent_tool_use_id")}
            timeline.append(assistant)
            seen_assistant[identifier] = assistant
        elif event.get("type") == "user":
            message = event.get("message") or {}
            blocks = message.get("content") or []
            for block in blocks if isinstance(blocks, list) else []:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                call_id = str(block.get("tool_use_id") or "")
                proposal = proposals.get(call_id)
                if not call_id or proposal is None:
                    continue
                cli_error = _cli_error(proposal["tool_name"], proposal["input"], block.get("content"))
                executions.append({"tool_use_id": call_id, "tool_name": proposal["tool_name"],
                                   "input": proposal["input"], "result": block.get("content"),
                                   "is_error": bool(block.get("is_error")) or cli_error is not None,
                                   "cli_error_status": cli_error["status"] if cli_error else None,
                                   "cli_error_observed": cli_error is not None,
                                   "ts_utc": proposal["at"],
                                   "ended_ts_utc": at})
    if len(timeline) == 1:
        final = next((entry["value"].get("result") for entry in reversed(events)
                      if isinstance(entry, dict) and isinstance(entry.get("value"), dict)
                      and entry["value"].get("type") == "result"), None)
        if final:
            timeline.append({"kind": "assistant_message", "text": final,
                             "request_id": "result", "ts_utc": raw.get("ended_at") or start})
    source = {**raw, "meta": meta, "phases": [{"id": "turn", "name": "Agent turn",
        "start_local": start, "end_local": raw.get("ended_at") or start}],
        "timeline": timeline, "tool_calls": executions, "events": events}
    return json.dumps(source, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()


def convert(prepared_bytes, archive_ref):
    """Only observed messages become facts; absent request context stays unknown."""
    source = json.loads(prepared_bytes)
    raw = source
    binding = {"run_id": "agent-" + raw["turn_id"], "query_id": raw["turn_id"],
               "env_id": "trace-hunter-agent"}
    result = convert_claude_export(prepared_bytes, source, binding)
    document = result.document
    document["run"]["status"] = ("completed" if raw.get("state") == "succeeded" else
                                   "cancelled" if raw.get("state") == "cancelled" else "failed")
    document["run"]["attributes"] = {"agent.session_id": raw["session_id"],
                                       "agent.turn_id": raw["turn_id"]}
    document["segments"][0]["session"] = {"namespace": "trace-hunter-agent",
                                             "id": raw["session_id"]}
    document["sources"][0].update({"locator": "content:" + archive_ref.digest,
                                    "sha256": archive_ref.digest.removeprefix("sha256:")})
    recorder = raw.get("recorder") or {}
    document["capture"]["collector"] = {"name": "Agent Trace Recorder",
                                             "version": str(recorder.get("version") or "legacy")}
    document["capture"]["coverage"]["contexts"] = "missing"
    origin = instant(document["clocks"][0]["origin_at"])
    sources = {}
    for source in raw.get("recorder_sources") or []:
        if not isinstance(source, dict) or not isinstance(source.get("content_ref"), dict):
            continue
        ref = source["content_ref"]
        if source.get("id") in sources or not ref.get("digest", "").startswith("sha256:"):
            continue
        sources[source["id"]] = source
        document["sources"].append({"id": source["id"],
                                    "sha256": ref["digest"].removeprefix("sha256:"),
                                    "locator": "content:" + ref["digest"],
                                    "media_type": "application/json", "state": "available"})
    tool_by_id = {span["id"]: span for span in document["spans"] if span["kind"] == "tool"}
    hook_starts = {}
    hook_status = {}
    for index, entry in enumerate(raw.get("hooks") or []):
        value = entry.get("value") if isinstance(entry, dict) else None
        if not isinstance(value, dict):
            continue
        kind = value.get("hook_event_name")
        call_id = str(value.get("tool_use_id") or "")
        if kind == "PreToolUse" and call_id:
            hook_starts.setdefault(call_id, (index, entry))
        elif kind in ("PostToolUse", "PostToolUseFailure") and call_id:
            hook_status[call_id] = (index, entry)
        elif kind in ("PreCompact", "PostCompact"):
            document["events"].append({"id": f"compact-hook-{index}", "type": "compaction",
                                      "segment_id": "segment-1", "source_refs": [source_ref(f"/hooks/{index}")],
                                      "context_change": {"before": None, "after": None,
                                                         "summary": searchable_content(None, f"/hooks/{index}",
                                                                                       missing_null=True)},
                                      "clock_id": "claude-clock",
                                      "at_ms": elapsed_ms(entry.get("at"), origin)})
    for call_id, (index, finished) in hook_status.items():
        event = finished["value"]
        span = tool_by_id.get(call_id)
        if span is None:
            name = str(event.get("tool_name") or "unknown")[:256]
            span = {"id": call_id[:256], "kind": "tool", "name": name,
                    "agent_id": str(event.get("agent_id") or "root")[:256],
                    "segment_id": "segment-1", "parent_id": None,
                    "status": "unknown", "tool": tool_value(name, event.get("tool_input"), call_id, None),
                    "source_refs": [source_ref(f"/hooks/{index}")]}
            document["spans"].append(span)
            tool_by_id[call_id] = span
        elif source_ref(f"/hooks/{index}") not in span["source_refs"]:
            span["source_refs"].append(source_ref(f"/hooks/{index}"))
        span["status"] = "error" if event.get("hook_event_name") == "PostToolUseFailure" else "ok"
        first = hook_starts.get(call_id)
        if first:
            span["source_refs"].append(source_ref(f"/hooks/{first[0]}"))
            measured = timing("claude-clock", elapsed_ms(first[1].get("at"), origin),
                              elapsed_ms(finished.get("at"), origin))
            if measured:
                span["timing"] = measured
        if span["name"] == "Skill":
            skill_input = event.get("tool_input") or (first[1]["value"].get("tool_input") if first else None)
            if isinstance(skill_input, dict):
                name = skill_input.get("skill") or skill_input.get("name")
                if isinstance(name, str) and name:
                    span["tool"]["skill"] = {"name": name[:256], "action": "invoke"}
    for execution in raw.get("tool_calls") or []:
        cli_error = _cli_error(execution.get("tool_name"), execution.get("input"),
                               execution.get("result"))
        if cli_error is None:
            continue
        span = tool_by_id.get(execution.get("tool_use_id"))
        if span is not None:
            span["status"] = "error"
            span["attributes"] = {**span.get("attributes", {}),
                                  "trace_hunter.cli_error_basis": "structured_tool_output",
                                  "trace_hunter.cli_http_status": cli_error["status"]}
    started = {}
    agent_spans = {}
    for index, entry in enumerate(raw.get("hooks") or []):
        if not isinstance(entry, dict) or not isinstance(entry.get("value"), dict):
            continue
        event = entry["value"]
        agent_id = event.get("agent_id")
        if not isinstance(agent_id, str) or not agent_id:
            continue
        kind = event.get("hook_event_name")
        if kind == "SubagentStart":
            started[agent_id] = (index, entry)
        elif kind == "SubagentStop":
            first_index, first = started.pop(agent_id, (None, None))
            slug = hashlib.sha256(agent_id.encode()).hexdigest()[:24]
            agent = {"id": "subagent-" + slug, "kind": "agent",
                     "name": str(event.get("agent_type") or "Claude subagent")[:256],
                     "agent_id": agent_id[:256], "segment_id": "segment-1", "parent_id": None,
                     "status": "ok", "source_refs": [source_ref(f"/hooks/{index}")]}
            if first is not None:
                agent["source_refs"].insert(0, source_ref(f"/hooks/{first_index}"))
                measured = timing("claude-clock", elapsed_ms(first.get("at"), origin),
                                  elapsed_ms(entry.get("at"), origin))
                if measured:
                    agent["timing"] = measured
            if agent_id in agent_spans:
                previous = agent_spans[agent_id]
                previous["source_refs"] = (previous["source_refs"] + agent["source_refs"])[:100]
            else:
                document["spans"].append(agent)
                agent_spans[agent_id] = agent
    for agent_id, (index, first) in started.items():
        if agent_id in agent_spans:
            continue
        document["spans"].append({
            "id": "subagent-" + hashlib.sha256(agent_id.encode()).hexdigest()[:24],
            "kind": "agent", "name": str(first["value"].get("agent_type") or "Claude subagent")[:256],
            "agent_id": agent_id[:256], "segment_id": "segment-1", "parent_id": None,
            "status": "unknown", "source_refs": [source_ref(f"/hooks/{index}")]})
    known_agents = {span["agent_id"]: span["id"] for span in document["spans"]
                    if span["kind"] == "agent"}
    for transcript_index, transcript in enumerate(raw.get("subagent_transcripts") or []):
        if not isinstance(transcript, dict) or transcript.get("state") != "complete":
            continue
        agent_id = str(transcript.get("agent_id") or "unknown")[:256]
        prefix = hashlib.sha256(agent_id.encode()).hexdigest()[:16]
        proposals = {}
        seen_models = set()
        for record_index, record in enumerate(transcript.get("records") or []):
            if not isinstance(record, dict) or record.get("type") not in ("assistant", "user"):
                continue
            blocks = (record.get("message") or {}).get("content") or []
            if not isinstance(blocks, list):
                continue
            message = record.get("message") or {}
            request_id = message.get("id")
            if record["type"] == "assistant" and isinstance(request_id, str) and request_id not in seen_models:
                seen_models.add(request_id)
                pointer = f"/subagent_transcripts/{transcript_index}/records/{record_index}"
                model_span = {"id": f"child-{prefix}-model-{hashlib.sha256(request_id.encode()).hexdigest()[:16]}",
                              "kind": "model", "name": "Claude subagent model request",
                              "agent_id": agent_id, "segment_id": "segment-1",
                              "parent_id": known_agents.get(agent_id), "status": "unknown",
                              "call": {"invocation_id": request_id, "attempt": 1,
                                       "provider_request_id": request_id},
                              "model": {"provider": "anthropic", "requested_model": message.get("model"),
                                        "request_count": 1, "context_id": None,
                                        "output_message_ids": [],
                                        "usage": claude_stream_usage(message.get("usage"), pointer + "/message/usage")},
                              "source_refs": [source_ref(pointer)]}
                measured = timing("claude-clock", None, elapsed_ms(record.get("timestamp"), origin))
                if measured:
                    model_span["timing"] = measured
                document["spans"].append(model_span)
            base_path = f"/subagent_transcripts/{transcript_index}/records/{record_index}/message/content"
            for block_index, block in enumerate(blocks):
                if not isinstance(block, dict):
                    continue
                path = base_path + f"/{block_index}"
                if block.get("type") == "text" and isinstance(block.get("text"), str):
                    document["messages"].append({
                        "id": f"child-{prefix}-message-{record_index}-{block_index}",
                        "role": record["type"], "attributes": {"agent_id": agent_id},
                        "content": searchable_content(block["text"], path + "/text"),
                        "source_refs": [source_ref(path)]})
                elif block.get("type") == "tool_use" and block.get("id"):
                    call_id = str(block["id"])
                    proposals[call_id] = (block, path, record.get("timestamp"))
                    document["tool_calls"].append({
                        "id": f"child-{prefix}-proposal-{hashlib.sha256(call_id.encode()).hexdigest()[:16]}",
                        "call_id": call_id[:256], "name": str(block.get("name") or "unknown")[:256],
                        "arguments": searchable_content(block.get("input"), path + "/input", missing_null=True),
                        "source_refs": [source_ref(path)], "attributes": {"agent_id": agent_id}})
                elif block.get("type") == "tool_result":
                    call_id = str(block.get("tool_use_id") or "")
                    proposal = proposals.get(call_id)
                    if proposal is None:
                        continue
                    use, use_path, started_at = proposal
                    name = str(use.get("name") or "unknown")[:256]
                    proposal_id = f"child-{prefix}-proposal-{hashlib.sha256(call_id.encode()).hexdigest()[:16]}"
                    span = {"id": f"child-{prefix}-tool-{hashlib.sha256(call_id.encode()).hexdigest()[:16]}",
                            "kind": "tool", "name": name, "agent_id": agent_id,
                            "segment_id": "segment-1", "parent_id": known_agents.get(agent_id),
                            "status": "error" if block.get("is_error") else "ok",
                            "tool": tool_value(name, use.get("input"), call_id, proposal_id),
                            "input": searchable_content(use.get("input"), use_path + "/input", missing_null=True),
                            "output": searchable_content(block.get("content"), path + "/content", missing_null=True),
                            "source_refs": [source_ref(use_path), source_ref(path)]}
                    measured = timing("claude-clock", elapsed_ms(started_at, origin),
                                      elapsed_ms(record.get("timestamp"), origin))
                    if measured:
                        span["timing"] = measured
                    document["spans"].append(span)
    _link_body_contexts(document, raw, sources)
    _attach_telemetry(document, raw, origin)
    _unified_stream_order(document, raw)
    stream_coverage = raw.get("stream_coverage") or {"state": "unknown"}
    if stream_coverage.get("state") == "partial":
        for field, state in document["capture"]["coverage"].items():
            if state == "complete":
                document["capture"]["coverage"][field] = "partial"
    document["extensions"]["trace_hunter.agent"] = {
        "turn_id": raw["turn_id"], "session_id": raw["session_id"],
        "raw_stream_ref": archive_ref.as_dict(), "mapping_issues": result.issues,
        "hook_coverage": raw.get("hook_coverage") or {"state": "missing"},
        "stream_coverage": stream_coverage,
        "recorder": recorder, "source_count": len(sources),
        "subagent_transcripts": [{"agent_id": item.get("agent_id"), "state": item.get("state")}
                                 for item in raw.get("subagent_transcripts") or []]}
    return document
