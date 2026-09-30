"""Shared fixed resource references and bounded JSON values for domain services.

Resolution checks existing, same-project resources at a caller-owned transaction.
It never reads source bodies, resolves latest, creates indexes or starts work.
"""

import json
import re
from sqlalchemy import text
from .catalog import canonical
from .database import Conflict
from .traces.service import MAX_REVISION, identifier

MAX_JSON_BYTES = 64 * 1024
MAX_INPUTS = 100
TOKEN_PATTERN = r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}"
TOKEN = re.compile(TOKEN_PATTERN + r"\Z", re.ASCII)
DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z", re.ASCII)
REF_FIELDS = {"kind", "id", "revision", "digest"}
TARGETS = {
    "trace_revision": ("trace_revisions", "run_id", "content_digest", "trace_run_id"),
    "selection_snapshot": ("selection_snapshots", "selection_id", "manifest_digest", "selection_id"),
    "artifact": ("artifacts", "artifact_id", "descriptor_digest", "upstream_artifact_id"),
}


def digest_json(value):
    return "sha256:" + canonical(value)[1]


def checked_digest(value):
    if not isinstance(value, str) or not DIGEST.fullmatch(value):
        raise ValueError("digest must be sha256:<64 lowercase hexadecimal characters>")
    return value


def namespace_token(value, field):
    if not isinstance(value, str) or not TOKEN.fullmatch(value):
        raise ValueError(field + " must be an ASCII namespace token of at most 128 characters")
    return value


def resource_ref(value):
    if not isinstance(value, dict) or set(value) != REF_FIELDS or not isinstance(value.get("kind"), str) or value["kind"] not in TARGETS:
        raise ValueError("Expected a fixed trace_revision, selection_snapshot or artifact reference")
    identifier(value["id"], "reference id")
    checked_digest(value["digest"])
    revision = value["revision"]
    if value["kind"] == "trace_revision":
        if type(revision) is not int or not 1 <= revision <= MAX_REVISION:
            raise ValueError("Trace reference revision must be a fixed positive platform revision")
    elif revision is not None:
        raise ValueError("Immutable selection/artifact references require revision null")
    return {key: value[key] for key in ("kind", "id", "revision", "digest")}


def _json_value(value):
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("JSON object keys must be strings")
        for child in value.values():
            _json_value(child)
    elif isinstance(value, list):
        for child in value:
            _json_value(child)
    elif value is not None and type(value) not in (str, int, float, bool):
        raise ValueError("Only JSON values are supported")


def json_object(value, field):
    if not isinstance(value, dict):
        raise ValueError(field + " must be a JSON object")
    try:
        _json_value(value)
        payload = canonical(value)[0]
        if len(payload.encode()) > MAX_JSON_BYTES:
            raise ValueError()
        return json.loads(payload)
    except (TypeError, ValueError, RecursionError, UnicodeError):
        raise ValueError(field + " must be valid JSON of at most 64 KiB") from None


def input_bindings(value):
    if not isinstance(value, list) or len(value) > MAX_INPUTS:
        raise ValueError("inputs must contain 0 to 100 fixed references")
    result = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"role", "ref"}:
            raise ValueError("Each input requires role and ref")
        result.append({"role": namespace_token(item["role"], "input role"), "ref": resource_ref(item["ref"])})
    return result


def resolve_inputs(db, project_id, inputs):
    # At most three lookups regardless of input count; no body reads or latest
    # resolution. Each target is immutable and its typed FK preserves binding.
    found = {}
    for kind, (table, id_column, digest_column, _) in TARGETS.items():
        targets = sorted({(item["ref"]["id"], item["ref"]["revision"]) for item in inputs if item["ref"]["kind"] == kind})
        if not targets:
            continue
        conditions, params = [], {"project": project_id}
        for ordinal, (resource_id, revision) in enumerate(targets):
            params["id" + str(ordinal)] = resource_id
            condition = id_column + "=:id" + str(ordinal)
            if kind == "trace_revision":
                params["revision" + str(ordinal)] = revision
                condition += " AND revision=:revision" + str(ordinal)
            conditions.append("(" + condition + ")")
        revision_column = "revision" if kind == "trace_revision" else "NULL"
        rows = db.execute(text(f"SELECT {id_column} AS id,{revision_column} AS revision,{digest_column} AS digest "
            f"FROM {table} WHERE project_id=:project AND (" + " OR ".join(conditions) + ")"), params).mappings()
        for row in rows:
            found[(kind, row["id"], row["revision"])] = row["digest"]
    for item in inputs:
        ref = item["ref"]
        actual = found.get((ref["kind"], ref["id"], ref["revision"]))
        if actual is None:
            raise KeyError("Artifact upstream resource not found in this project")
        if actual != ref["digest"]:
            raise Conflict("Artifact upstream digest does not match the fixed resource")
