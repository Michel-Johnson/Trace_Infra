"""Small adapter contract: bytes + explicit task binding -> document + mapping report."""
import hashlib
import json
import re
from dataclasses import dataclass, field

VERSION = "interop-preview/1"
MAX_BYTES = 256 * 1024 * 1024


def issue(code, path, message, fix, severity="warning", classification="missing"):
    return {"code": code, "path": path, "severity": severity, "classification": classification,
            "message": message, "fix": fix}


class IngestError(ValueError):
    def __init__(self, issues):
        self.issues = issues
        super().__init__("; ".join(i["code"] for i in issues))


def reject(code, path, message, fix):
    raise IngestError([issue(code, path, message, fix, "error")])


def parse(raw):
    if len(raw) > MAX_BYTES:
        reject("INPUT_TOO_LARGE", "", "输入超过 16 MiB。", "按运行拆分或使用附件引用，不要截断正文后冒充完整输入。")
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result
    try:
        data = json.loads(raw, object_pairs_hook=unique, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        json.dumps(data, allow_nan=False).encode("utf-8")
    except (ValueError, UnicodeError, RecursionError):
        reject("JSON_INVALID", "", "JSON 无效、含重复字段或非有限数值。", "修正原文件格式；同一对象的字段名只能出现一次。")
    if not isinstance(data, dict):
        reject("ROOT_OBJECT_REQUIRED", "", "需要单个运行的 JSON 对象。", "每次传入一份原生轨迹，不要传入数组。")
    return data


def source_ref(pointer=""):
    return {"source_id": "input", "pointer": pointer}


def content_ref(value, pointer, missing_null=False):
    if missing_null and value is None:
        return {"state": "missing"}
    explicitly_truncated = isinstance(value, str) and (
        "[trimmed]" in value or re.search(r"\btruncated(?:\.\.\.|…)[ \t]*$", value, re.IGNORECASE)
    )
    state = "partial" if explicitly_truncated else "complete"
    return {"state": state, "ref": source_ref(pointer)}


def searchable_content(value, pointer, missing_null=False):
    """Keep the source pointer authoritative and add a bounded exact/truncated search copy."""
    result = content_ref(value, pointer, missing_null=missing_null)
    if value is None:
        return result
    text = value if isinstance(value, str) else json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    result["search_text"] = text[:100000]
    result["search_text_state"] = (
        "exact" if result["state"] == "complete" and len(text) <= 100000 else "truncated"
    )
    return result


@dataclass
class Conversion:
    document: dict
    issues: list = field(default_factory=list)
    mapping: dict = field(default_factory=dict)


def base(raw, binding, harness, adapter, version=VERSION):
    for key in ("run_id", "query_id", "env_id"):
        if not isinstance(binding.get(key), str) or not binding[key].strip():
            reject("BINDING_REQUIRED", "/binding/" + key, f"缺少 {key}。", f"使用 --{key.replace('_', '-')} 指定平台分配的身份，不从任务文字猜测。")
    byte_hash = hashlib.sha256(raw).hexdigest()
    identity = hashlib.sha256(json.dumps({"source_sha256": byte_hash, "binding": binding, "adapter": adapter, "version": version}, sort_keys=True).encode()).hexdigest()
    return {
        "schema_version": "trace-hunter/2.0-draft.2",
        "document": {"id": "document-" + identity, "revision": 1, "previous_document_id": None, "sealed": True},
        "run": {"id": binding["run_id"], "query_id": binding["query_id"], "env_id": binding["env_id"],
                "harness": harness, "status": "unknown", "environment": {"isolation": "unknown", "network_access": "unknown"}},
        "capture": {"collector": {"name": adapter, "version": version},
                    "coverage": {"tools": "unknown", "model_requests": "unknown", "messages": "missing", "contexts": "missing", "timing": "missing"}},
        "sources": [{"id": "input", "state": "available", "locator": "source.json", "media_type": "application/json", "sha256": byte_hash}],
        "segments": [{"id": "segment-1", "session": None, "source_refs": [source_ref()]}],
        "spans": [], "messages": [], "turns": [], "contexts": [], "events": [], "links": [], "tool_calls": [],
        "extensions": {"trace_hunter.adapter": {"id": adapter, "version": version, "identity_basis": "input-byte-hash-and-explicit-binding"}},
    }
