"""Shared facts for native-source adapters; never infer missing events."""
from datetime import datetime
from pathlib import PurePosixPath
import re

from .common import source_ref


SKILL_COMMAND = re.compile(r"^\s*(load|invoke)\s+([^\s]+)\s*$", re.IGNORECASE)


def instant(value):
    if not isinstance(value, str):
        return None
    try:
        point = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return point if point.utcoffset() is not None else None


def elapsed_ms(value, origin):
    point = instant(value)
    return round((point - origin).total_seconds() * 1000, 6) if point and origin else None


def timing(clock_id, start_ms=None, end_ms=None, duration_ms=None):
    if start_ms is None and end_ms is None and duration_ms is None:
        return None
    value = {"clock_id": clock_id, "start_ms": start_ms, "end_ms": end_ms}
    if duration_ms is not None:
        value.update(duration_ms=duration_ms, duration_basis="source_reported",
                     duration_scope="unknown")
    return value


def operation(name):
    return {"read": "read", "write": "write", "edit": "write", "bash": "bash",
            "shell": "bash",
            "askuserquestion": "human"}.get(str(name).lower(), "other")


def skill(name, arguments):
    if str(name).lower() == "skill":
        if isinstance(arguments, dict):
            value = arguments.get("skill") or arguments.get("name")
            return {"name": str(value) if value else None, "action": "invoke"}
        if isinstance(arguments, str):
            matched = SKILL_COMMAND.fullmatch(arguments)
            if matched:
                return {"name": matched.group(2), "action": matched.group(1).lower()}
        return {"name": None, "action": "invoke"}
    arguments = arguments if isinstance(arguments, dict) else {}
    if str(name).lower() == "read":
        path = arguments.get("file_path") or arguments.get("path")
        if isinstance(path, str) and PurePosixPath(path.replace("\\", "/")).name == "SKILL.md":
            parent = PurePosixPath(path.replace("\\", "/")).parent.name
            return {"name": parent or None, "action": "load"}
    return None


def tool_value(name, arguments, call_id=None, proposal_id=None):
    value = {"operation": operation(name)}
    if call_id is not None:
        value["call_id"] = call_id
    if proposal_id is not None:
        value["proposal_id"] = proposal_id
    label = skill(name, arguments)
    if label:
        value["skill"] = label
    return value


def total_usage(value, pointer):
    if not isinstance(value, dict):
        return None
    aliases = {
        "input_tokens": ("input_tokens", "prompt_tokens"),
        "output_tokens": ("output_tokens", "completion_tokens"),
        "cache_read_tokens": ("cache_read_input_tokens", "cached_tokens", "cache_read_tokens"),
        "cache_write_tokens": ("cache_creation_input_tokens", "cache_creation_tokens", "cache_write_tokens"),
        "reasoning_tokens": ("thinking_tokens", "reasoning_tokens"),
    }
    counters = {}
    for target, names in aliases.items():
        counters[target] = next((value[name] for name in names if name in value), None)
    if not any(item is not None for item in counters.values()):
        return None
    return {"accounting": "total",
            "completeness": "complete" if counters["input_tokens"] is not None and counters["output_tokens"] is not None else "partial",
            "total": counters, "source_refs": [source_ref(pointer)]}


def claude_stream_usage(value, pointer):
    """Claude reports fresh, cache-read and cache-written input separately.

    Canonical input_tokens includes all three; otherwise a cache hit can make
    cache_read_tokens exceed input_tokens and invalidate a real capture.
    """
    if not isinstance(value, dict):
        return None
    normalized = dict(value)
    fresh = value.get("input_tokens")
    cache_read = value.get("cache_read_input_tokens", value.get("cache_read_tokens"))
    cache_write = value.get("cache_creation_input_tokens",
                            value.get("cache_creation_tokens", value.get("cache_write_tokens")))
    if (type(fresh) is int and fresh >= 0 and
            all(item is None or (type(item) is int and item >= 0) for item in (cache_read, cache_write))):
        normalized["input_tokens"] = fresh + (cache_read or 0) + (cache_write or 0)
    usage = total_usage(normalized, pointer)
    if usage is None:
        return None
    counters = usage["total"]
    if all(item in (None, 0) for item in counters.values()):
        return None
    return usage
