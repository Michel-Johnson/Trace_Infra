"""Observed-only model-context visibility checks; never infer missing history."""
from datetime import datetime, timedelta


def _instant(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo is not None else None


def _available_before(content, timing, clocks):
    available = content.get("available_at") if isinstance(content, dict) else None
    if not available:
        return None
    start = timing.get("start_ms")
    clock_id = timing.get("clock_id")
    if "at_ms" in available:
        if start is None or available.get("clock_id") != clock_id:
            return None
        return available["at_ms"] <= start
    observed = _instant(available.get("observed_at"))
    clock = clocks.get(clock_id, {})
    origin = _instant(clock.get("origin_at"))
    if observed is None or origin is None or start is None:
        return None
    return observed <= origin + timedelta(milliseconds=start)


def evaluate(document):
    """Return one body-free pass/fail/unknown report per model request record."""
    messages = {item["id"]: item for item in document.get("messages", [])}
    contexts = {item["id"]: item for item in document.get("contexts", [])}
    clocks = {item["id"]: item for item in document.get("clocks", [])}
    reports = {}
    for span in document.get("spans", []):
        if span["kind"] not in ("model", "model_batch"):
            continue
        context_id = span["model"].get("context_id")
        context = contexts.get(context_id)
        issues = []
        if context is None:
            issues.append({"code": "REQUEST_CONTEXT_UNKNOWN", "object_id": context_id})
        else:
            request = context["request"]
            if request.get("state") != "complete":
                issues.append({"code": "REQUEST_SNAPSHOT_INCOMPLETE", "object_id": context_id})
            request_visible = _available_before(request, span.get("timing", {}), clocks)
            if request_visible is False:
                issues.append({"code": "FUTURE_CONTEXT_REQUEST", "object_id": context_id})
            elif request_visible is None:
                issues.append({"code": "REQUEST_AVAILABILITY_UNKNOWN", "object_id": context_id})
            output_ids = set(span["model"].get("output_message_ids", []))
            for message_id in context["message_ids"]:
                if message_id in output_ids:
                    issues.append({"code": "MODEL_OUTPUT_IN_REQUEST_CONTEXT", "object_id": message_id})
                message = messages.get(message_id)
                if message is None:
                    continue  # Structural validation reports the dangling reference.
                visible = _available_before(message["content"], span.get("timing", {}), clocks)
                if visible is False:
                    issues.append({"code": "FUTURE_MESSAGE_IN_REQUEST_CONTEXT", "object_id": message_id})
                elif visible is None:
                    issues.append({"code": "MESSAGE_AVAILABILITY_UNKNOWN", "object_id": message_id})
        failed = any(issue["code"].startswith("FUTURE_") or
                     issue["code"] == "MODEL_OUTPUT_IN_REQUEST_CONTEXT" for issue in issues)
        reports[span["id"]] = {
            "status": "fail" if failed else ("unknown" if issues else "pass"),
            "context_id": context_id,
            "issues": issues,
        }
    return reports
