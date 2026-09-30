"""Offline design service: discovery, preparation, evidence checks and diagnostics."""
import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path

from ..contract_preview import SCHEMA, VALIDATOR, ContractError, validate, summarize, objects
from ..traces.formats import STORAGE_V2_VALIDATORS
from . import agent_benchmark, atif, atif_v18, claude, doubao, doubao_turn, v1
from .common import VERSION, Conversion, IngestError, issue, parse, reject, MAX_BYTES

# New formats register trusted code here. Input extensions never load modules.
ADAPTERS = {"trace-hunter/1.1": v1.convert, "ATIF-v1.7": atif.convert, "ATIF-v1.8": atif_v18.convert,
            "claude-export": claude.convert, "doubao-export": doubao.convert,
            "doubao-turn-export": doubao_turn.convert, "agent-benchmark/1": agent_benchmark.convert}


def pointer(parts):
    return "/" + "/".join(str(p).replace("~", "~0").replace("/", "~1") for p in parts) if parts else ""


def diagnostics(document):
    errors = []
    validator = STORAGE_V2_VALIDATORS.get(document.get("schema_version"), VALIDATOR)
    for error in validator.iter_errors(document):
        # JSON Schema's default message can quote whole prompts or tool results.
        # Report only a location, rule and actionable instruction.
        path = pointer(error.absolute_path)
        fix = {"required": "补齐该对象的必填字段；通过 describe 查看协议。",
               "additionalProperties": "将非核心字段放进有命名空间的 extensions，或修正字段名。",
               "enum": "使用该字段声明的枚举值；未知值采用协议提供的 unknown/null。",
               "const": "使用支持的协议版本和对象类型。",
               "oneOf": "选择一种互斥结构；例如 usage 只能选 total 或 components。"}.get(error.validator, "按该位置的 Schema 类型或范围修正输入，不猜造缺失事实。")
        errors.append(issue("SCHEMA_" + str(error.validator).upper(), path, "输入不符合结构规则。", fix, "error"))
        if len(errors) == 20:
            return errors
    if errors:
        return errors
    try:
        validate(document, schema_validator=validator, schema_validated=True)
    except ContractError as error:
        errors.extend(error.issues)
    except (ValueError, TypeError, RecursionError):
        # Known cross-record rules; avoid echoing untrusted record identifiers.
        errors.append(issue("SEMANTIC_INVARIANT_FAILED", "", "记录间的身份、引用、时间或消费归属不一致。",
                            "用独立 validator 定位跨记录冲突；不要仅修到 JSON 结构通过。", "error"))
    return errors


def resolve(source, pointer_value):
    if pointer_value == "":
        return source
    if not pointer_value.startswith("/"):
        raise ValueError("not JSON Pointer")
    current = source
    for token in pointer_value[1:].split("/"):
        token = token.replace("~1", "/").replace("~0", "~")
        if isinstance(current, list):
            if not token.isdigit() or (len(token) > 1 and token[0] == "0"):
                raise ValueError("invalid index")
            current = current[int(token)]
        elif isinstance(current, dict):
            current = current[token]
        else:
            raise ValueError("not a container")
    return current


def verify_source(document, raw, source=None):
    source = source if source is not None else parse(raw)
    records = {s["id"]: s for s in document["sources"]}
    if "input" not in records or records["input"].get("locator") != "source.json":
        reject("BUNDLE_SOURCE_BINDING_INVALID", "/sources", "缺少固定的原始输入绑定。", "用 prepare 生成带 source.json 的完整包。")
    if hashlib.sha256(raw).hexdigest() != records["input"]["sha256"]:
        reject("SOURCE_HASH_MISMATCH", "/sources", "原始字节与预期哈希不同。", "恢复匹配的 source.json；不要修改轨迹中的哈希来掩盖原文变化。")
    checked = 0
    for item in objects(document):
        if item.get("source_id") != "input" or "pointer" not in item:
            continue
        try:
            resolve(source, item["pointer"])
        except (KeyError, IndexError, ValueError, TypeError):
            reject("SOURCE_POINTER_MISSING", "/sources", "来源指针不能在 source.json 中定位。", "修复 adapter 的 source pointer，保持原文件不变。")
        checked += 1
    return {"input_sha256": records["input"]["sha256"], "byte_hash_verified": True, "resolved_source_pointers": checked,
            "external_sources_not_read": sum(s["id"] != "input" for s in document["sources"]),
            "scope": "Only the explicitly supplied source.json is read; external locators are not followed."}


def capabilities(document):
    spans = document["spans"]
    models = [s for s in spans if s["kind"] in ("model", "model_batch")]
    tools = [s for s in spans if s["kind"] == "tool"]
    coverage = document["capture"]["coverage"]
    contexts = {c["id"]: c for c in document.get("contexts", [])}
    def capability(known, total, complete, reason):
        available = bool(total) and known == total and complete
        return {"status": "available" if available else "partial" if known else "unavailable", "recorded": known, "observed": total,
                "reasons": [] if available else [reason]}
    timed = sum((s.get("timing", {}).get("duration_ms") is not None or
                 (s.get("timing", {}).get("start_ms") is not None and s.get("timing", {}).get("end_ms") is not None)) for s in tools)
    used = sum(bool(s["model"].get("usage")) and s["model"]["usage"]["completeness"] == "complete" for s in models)
    contextual = sum(contexts.get(s["model"].get("context_id"), {}).get("request", {}).get("state") == "complete" for s in models)
    return {
        "browse": capability(len(spans), len(spans), True, "NO_EXECUTION_RECORDS"),
        "tool_timing": capability(timed, len(tools), coverage["tools"] == "complete" and all(s.get("timing", {}).get("duration_scope") != "unknown" for s in tools), "TOOL_TIME_SCOPE_OR_CAPTURE_INCOMPLETE"),
        "token_totals": capability(used, len(models), coverage["model_requests"] == "complete", "REQUEST_USAGE_OR_CAPTURE_INCOMPLETE"),
        "request_context": capability(contextual, len(models), coverage["contexts"] == "complete" and all(s["kind"] == "model" for s in models), "EXACT_REQUEST_CONTEXT_MISSING"),
        "sft": {"status": "unavailable", "reasons": ["TRAINING_ADMISSION_NOT_IMPLEMENTED"]},
        "rl": {"status": "unavailable", "reasons": ["TOKEN_LEVEL_TRAINING_PROFILE_NOT_IMPLEMENTED"]},
    }


def report(conversion, raw, source=None):
    document = conversion.document
    errors = diagnostics(document)
    if errors:
        raise IngestError(errors)
    evidence = verify_source(document, raw, source)
    return {"ok": True, "protocol": VERSION, "live_import": False, "capabilities": capabilities(document),
            "summary": summarize(document, schema_validator=STORAGE_V2_VALIDATORS[document["schema_version"]], validated=True),
            "evidence": evidence, "issues": conversion.issues, "mapping": conversion.mapping}


def convert(raw, binding=None, source_format=None):
    source = parse(raw)
    fmt = source_format or source.get("schema_version")
    declared = source.get("schema_version")
    if fmt not in ADAPTERS or (source_format is None and declared != fmt):
        reject("FORMAT_UNSUPPORTED", "/schema_version", "来源协议版本未支持，未尝试按名字猜测格式。", "运行 describe 查看 adapter；新增来源时注册版本明确的 adapter。")
    try:
        converted = ADAPTERS[fmt](raw, source, binding or {})
    except IngestError:
        raise
    except (ValueError, KeyError, TypeError, AttributeError, IndexError, RecursionError):
        reject("SOURCE_SHAPE_UNSUPPORTED", "", "来源结构超出当前 adapter 的支持范围。", "保留原文件，增加对应来源样例与映射规则；不要删除未知字段绕过问题。")
    return converted, report(converted, raw, source)


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()


def prepare(raw, output, binding=None, source_format=None):
    conversion, details = convert(raw, binding, source_format)
    payloads = {"source.json": raw, "trace.json": encoded(conversion.document), "report.json": encoded(details)}
    manifest = {"protocol": VERSION, "document_id": conversion.document["document"]["id"],
                "files": {name: {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)} for name, data in payloads.items()}}
    payloads["manifest.json"] = encoded(manifest)
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    reused = False
    if output.is_symlink():
        reject("OUTPUT_CONFLICT", "/output", "输出目录是符号链接，未写入。", "选择独立的普通目录。")
    if output.exists():
        if not output.is_dir() or set(p.name for p in output.iterdir()) != set(payloads) or any((output / name).is_symlink() or (output / name).read_bytes() != data for name, data in payloads.items()):
            reject("OUTPUT_CONFLICT", "/output", "目标包已存在且内容不同，未覆盖。", "换一个输出目录，或核对原有包；不要自动删除已有数据。")
        reused = True
    else:
        staging = Path(tempfile.mkdtemp(prefix=".trace-stage-", dir=output.parent))
        try:
            for name, data in payloads.items():
                fd = os.open(staging / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as handle:
                    handle.write(data)
            os.rename(staging, output)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
    return {"ok": True, "live_import": False, "reused": reused, "bundle": str(output.resolve()),
            "document_id": conversion.document["document"]["id"], "issue_count": len(details["issues"]),
            "capabilities": details["capabilities"], "next": {"action": "check", "bundle": str(output.resolve())}}


def check_bundle(path, required=None):
    path = Path(path)
    if path.is_symlink():
        reject("BUNDLE_FILE_INVALID", "", "包目录不能是符号链接。", "选择 prepare 生成的普通目录。")
    # A fixed allowlist, not paths taken from the input manifest or source URLs.
    names = ("source.json", "trace.json", "report.json", "manifest.json")
    values = {}
    for name in names:
        target = path / name
        if target.is_symlink() or not target.is_file() or target.stat().st_size > MAX_BYTES * 4:
            reject("BUNDLE_FILE_INVALID", "/" + name, "包内文件缺失、过大或为符号链接。", "使用 prepare 的原始输出包；不跟随外部文件引用。")
        values[name] = target.read_bytes()
    manifest = parse(values["manifest.json"])
    if manifest.get("protocol") != VERSION or not isinstance(manifest.get("files"), dict):
        reject("BUNDLE_MANIFEST_INVALID", "/manifest.json", "包清单结构或版本无效。", "使用 prepare 生成匹配版本的包。")
    for name in names[:-1]:
        expected = manifest.get("files", {}).get(name, {})
        if not isinstance(expected, dict):
            reject("BUNDLE_MANIFEST_INVALID", "/manifest.json/files", "包文件条目结构无效。", "使用 prepare 生成的清单。")
        if expected.get("sha256") != hashlib.sha256(values[name]).hexdigest() or expected.get("bytes") != len(values[name]):
            reject("BUNDLE_HASH_MISMATCH", "/" + name, "包内文件与 manifest 不一致。", "从原始输入重新生成新包，保留旧包用于核对。")
    document = parse(values["trace.json"])
    previous = parse(values["report.json"])
    if not isinstance(previous.get("issues"), list) or not all(isinstance(item, dict) for item in previous["issues"]) or not isinstance(previous.get("mapping"), dict):
        reject("BUNDLE_REPORT_INVALID", "/report.json", "包报告结构无效。", "从原始输入重新生成包；报告不能代替校验结果。")
    details = report(Conversion(document, previous.get("issues", []), previous.get("mapping", {})), values["source.json"])
    if manifest.get("document_id") != document["document"]["id"]:
        reject("DOCUMENT_ID_MISMATCH", "/document/id", "包与轨迹的文档身份不同。", "恢复一致的 prepare 输出。")
    if required and details["capabilities"][required]["status"] != "available":
        reject("CAPABILITY_INCOMPLETE", "/capabilities/" + required, "数据尚不满足所请求的用途。", "查看 capability 的缺失原因并补采；保留未知，不填零或生成假上下文。")
    return details


def describe():
    return {"protocol": VERSION, "live_import": False, "canonical_schema": "trace-hunter/2.0-draft.2",
            "adapters": [{"format": name, "binding_required": [] if name.startswith("trace-hunter/") else ["run_id", "query_id", "env_id"]} for name in ADAPTERS],
            "actions": ["describe", "prepare", "check"], "requirements": ["browse", "tool_timing", "token_totals", "request_context", "sft", "rl"],
            "defaults": {"network_access": "unknown", "isolation": "unknown", "analysis": "not_triggered"},
            "safety": {"network_fetch": False, "execute_uploaded_code": False, "overwrite_output": False},
            "errors": {"fields": ["code", "path", "severity", "message", "fix"], "format": "JSON", "invalid_input_exit_code": 2},
            "schema_path": "contracts/schemas/trace-v2-storage-v2.schema.json"}
