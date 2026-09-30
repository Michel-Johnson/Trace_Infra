"""Register fixed operation contracts and persist explicitly requested invocations.

This module does not execute implementation keys, retrieve packages or dispatch
remote requests. Pending requests gain durable attempts through the task service.
"""

from collections import Counter
import base64
import binascii
import hashlib
import json
import re
import uuid

from referencing.exceptions import Unresolvable
from sqlalchemy import text

from ..access import Principal
from ..catalog import canonical
from ..content import ContentCorruption
from ..database import Conflict
from ..config_schema import configuration_validator
from ..resources import (MAX_INPUTS, TARGETS, checked_digest, digest_json, input_bindings,
                         json_object, namespace_token, resolve_inputs)
from ..traces.service import identifier, utc_now

OPERATION_VERSION = "trace-hunter/operation/1"
SPEC_VERSION = "trace-hunter/invocation-spec/1"
VERSION = "trace-hunter/invocation/1"
URL_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}\Z", re.ASCII)


def _url_token(value, field):
    if not isinstance(value, str) or not URL_TOKEN.fullmatch(value):
        raise ValueError(field + " must be an ASCII URL token of at most 128 characters")
    return value


def _object(value, fields):
    if not isinstance(value, dict) or set(value) != set(fields):
        raise ValueError("Object fields do not match the operation contract")
    return value


def operation_definition(value):
    _object(value, ("schema_version", "operation_id", "version", "implementation", "input_roles", "config_schema", "outputs"))
    if value["schema_version"] != OPERATION_VERSION:
        raise ValueError("Unsupported operation contract version")
    _url_token(value["operation_id"], "operation_id")
    _url_token(value["version"], "version")
    impl = _object(value["implementation"], ("host", "key", "package_digest"))
    if impl["host"] not in ("worker", "remote"):
        raise ValueError("Only explicit worker or remote computations are invocable")
    namespace_token(impl["key"], "implementation key")
    checked_digest(impl["package_digest"])
    for key in ("input_roles", "outputs"):
        rows = value[key]
        if not isinstance(rows, list) or len(rows) > MAX_INPUTS:
            raise ValueError(key + " must contain at most 100 role declarations")
        roles = []
        for row in rows:
            _object(row, ("role", "min_items", "max_items", "kinds" if key == "input_roles" else "artifact_type"))
            roles.append(namespace_token(row["role"], "role"))
            lo, hi = row["min_items"], row["max_items"]
            if type(lo) is not int or type(hi) is not int or not 0 <= lo <= hi <= MAX_INPUTS:
                raise ValueError("Role bounds must satisfy 0 <= min_items <= max_items <= 100")
            if key == "input_roles":
                kinds = row["kinds"]
                if not isinstance(kinds, list) or not kinds or any(not isinstance(kind, str) or kind not in TARGETS for kind in kinds) or len(set(kinds)) != len(kinds):
                    raise ValueError("Input kinds must be unique supported fixed resource kinds")
            else:
                namespace_token(row["artifact_type"], "output artifact_type")
        if len(set(roles)) != len(roles) or sum(row["min_items"] for row in rows) > MAX_INPUTS:
            raise ValueError("Role names must be unique and required item counts must fit the request bound")
    configuration_validator(json_object(value["config_schema"], "config_schema"))
    return json_object(value, "operation definition")


def operation_ref(value):
    _object(value, ("operation_id", "version", "digest"))
    return {"operation_id": _url_token(value["operation_id"], "operation_id"),
            "version": _url_token(value["version"], "version"), "digest": checked_digest(value["digest"])}


def _actor(actor, project_id):
    if actor is not None and not isinstance(actor, Principal):
        raise ValueError("actor must be a server-authenticated principal")
    if actor is not None and not actor.is_operator and actor.project_id != project_id:
        raise PermissionError("Project access denied")
    return {"kind": "unknown" if actor is None else "operator" if actor.is_operator else "service",
            "principal_id": actor.principal_id if actor is not None else None}


def _validate_inputs(definition, inputs, config):
    roles = {row["role"]: row for row in definition["input_roles"]}
    counts = Counter()
    for item in inputs:
        role = roles.get(item["role"])
        if role is None or item["ref"]["kind"] not in role["kinds"]:
            raise ValueError("Input role or resource kind is not accepted by the operation")
        counts[item["role"]] += 1
    if any(not row["min_items"] <= counts[name] <= row["max_items"] for name, row in roles.items()):
        raise ValueError("Input counts do not satisfy the operation role bounds")
    try:
        valid = configuration_validator(definition["config_schema"]).is_valid(config)
    except (ValueError, TypeError, RecursionError, Unresolvable):
        raise ValueError("Configuration validation could not complete") from None
    if not valid:
        raise ValueError("Configuration does not satisfy the registered operation schema")


class Invocations:
    def __init__(self, repository):
        self.repository = repository

    @staticmethod
    def _require_project(db, project_id):
        if db.execute(text("SELECT project_id FROM projects WHERE project_id=:project"), {"project": project_id}).first() is None:
            raise KeyError("Project not found")

    @staticmethod
    def _describe_operation(row):
        try:
            definition = json.loads(row["definition"])
            if digest_json(definition) != row["definition_digest"] or definition["operation_id"] != row["operation_id"] or definition["version"] != row["version"]:
                raise ValueError()
        except (ValueError, TypeError, KeyError, UnicodeError):
            raise ContentCorruption("Operation definition does not match its identity") from None
        return {"project_id": row["project_id"], "ref": {"operation_id": row["operation_id"], "version": row["version"], "digest": row["definition_digest"]},
                "definition": definition, "package_verification": "declared", "created_at": row["created_at"]}

    def register_operation(self, project_id, definition):
        identifier(project_id, "project_id")
        definition = operation_definition(definition)
        values = {"project": project_id, "id": definition["operation_id"], "version": definition["version"],
                  "digest": digest_json(definition), "definition": canonical(definition)[0], "at": utc_now()}
        with self.repository.engine.begin() as db:
            self._require_project(db, project_id)
            created = db.execute(text("""INSERT INTO operation_versions(project_id,operation_id,version,definition_digest,definition,created_at)
                VALUES(:project,:id,:version,:digest,:definition,:at) ON CONFLICT(project_id,operation_id,version) DO NOTHING"""), values).rowcount == 1
            row = db.execute(text("SELECT * FROM operation_versions WHERE project_id=:project AND operation_id=:id AND version=:version"), values).mappings().one()
            if row["definition_digest"] != values["digest"]:
                raise Conflict("Operation version already binds a different definition")
            result = self._describe_operation(row)
        return {"created": created, "operation": result}

    def get_operation(self, project_id, operation_id, version):
        identifier(project_id, "project_id")
        values = {"project": project_id, "id": _url_token(operation_id, "operation_id"), "version": _url_token(version, "version")}
        rows = self.repository.rows("SELECT * FROM operation_versions WHERE project_id=:project AND operation_id=:id AND version=:version", values)
        if not rows:
            raise KeyError("Operation version not found")
        return self._describe_operation(rows[0])

    @staticmethod
    def _describe(row):
        try:
            spec = json.loads(row["spec"])
            operation = {"operation_id": row["operation_id"], "version": row["operation_version"], "digest": row["operation_digest"]}
            if spec["schema_version"] != SPEC_VERSION or spec["project_id"] != row["project_id"] or spec["operation"] != operation or len(spec["inputs"]) != row["input_count"] or digest_json(spec) != row["spec_digest"]:
                raise ValueError()
        except (ValueError, TypeError, KeyError, UnicodeError):
            raise ContentCorruption("Invocation specification does not match its identity") from None
        return {"schema_version": VERSION, "kind": "invocation", "project_id": row["project_id"], "invocation_id": row["invocation_id"],
            "spec_digest": row["spec_digest"], "status": row["status"], "operation": operation, "inputs": spec["inputs"], "config": spec["config"],
            "requested_by": {"kind": row["actor_kind"], "principal_id": row["actor_principal_id"]}, "created_at": row["created_at"]}

    def create(self, project_id, value, *, request_key, actor=None):
        identifier(project_id, "project_id")
        identifier(request_key, "request_key")
        _object(value, ("operation", "inputs", "config"))
        operation, inputs, config = operation_ref(value["operation"]), input_bindings(value["inputs"]), json_object(value["config"], "config")
        submitter = _actor(actor, project_id)
        spec = {"schema_version": SPEC_VERSION, "project_id": project_id, "operation": operation, "inputs": inputs, "config": config}
        fingerprint = digest_json(spec)
        values = {"project": project_id, "key": request_key, "key_hash": hashlib.sha256(request_key.encode()).hexdigest(),
                  "fingerprint": fingerprint, "id": "inv_" + uuid.uuid4().hex}
        with self.repository.engine.begin() as db:
            self._require_project(db, project_id)
            created = db.execute(text("""INSERT INTO invocation_requests(project_id,request_key_hash,request_key,fingerprint,invocation_id)
                VALUES(:project,:key_hash,:key,:fingerprint,:id) ON CONFLICT(project_id,request_key_hash) DO NOTHING"""), values).rowcount == 1
            if not created:
                previous = db.execute(text("SELECT * FROM invocation_requests WHERE project_id=:project AND request_key_hash=:key_hash"), values).mappings().one()
                if previous["request_key"] != request_key or previous["fingerprint"] != fingerprint:
                    raise Conflict("Invocation request key already binds another computation")
                values["id"] = previous["invocation_id"]
            else:
                params = {"project": project_id, "id": operation["operation_id"], "version": operation["version"]}
                row = db.execute(text("SELECT * FROM operation_versions WHERE project_id=:project AND operation_id=:id AND version=:version"), params).mappings().first()
                if row is None:
                    raise KeyError("Operation version not found")
                descriptor = self._describe_operation(row)
                if descriptor["ref"]["digest"] != operation["digest"]:
                    raise Conflict("Operation digest does not match its fixed version")
                _validate_inputs(descriptor["definition"], inputs, config)
                resolve_inputs(db, project_id, inputs)
                row = {"project_id": project_id, "invocation_id": values["id"], "operation_id": operation["operation_id"], "operation_version": operation["version"],
                    "operation_digest": operation["digest"], "spec_digest": fingerprint, "spec": canonical(spec)[0], "status": "pending", "input_count": len(inputs),
                    "actor_kind": submitter["kind"], "actor_principal_id": submitter["principal_id"], "created_at": utc_now()}
                db.execute(text("INSERT INTO invocations(" + ",".join(row) + ") VALUES(" + ",".join(":" + key for key in row) + ")"), row)
                for position, item in enumerate(inputs):
                    ref = item["ref"]
                    binding = {"project_id": project_id, "invocation_id": values["id"], "input_position": position, "role": item["role"],
                        "upstream_kind": ref["kind"], "upstream_digest": ref["digest"], "trace_run_id": ref["id"] if ref["kind"] == "trace_revision" else None,
                        "trace_revision": ref["revision"] if ref["kind"] == "trace_revision" else None,
                        "selection_id": ref["id"] if ref["kind"] == "selection_snapshot" else None,
                        "upstream_artifact_id": ref["id"] if ref["kind"] == "artifact" else None}
                    db.execute(text("INSERT INTO invocation_inputs(" + ",".join(binding) + ") VALUES(" + ",".join(":" + key for key in binding) + ")"), binding)
                from ..notifications import publish
                publish(db, project_id, 'invocation.requested', values['id'])
            row = db.execute(text("SELECT * FROM invocations WHERE project_id=:project AND invocation_id=:id"), values).mappings().one()
            result = self._describe(row)
        return {"created": created, "invocation": result}

    def get(self, project_id, invocation_id):
        identifier(project_id, "project_id")
        identifier(invocation_id, "invocation_id")
        rows = self.repository.rows("SELECT * FROM invocations WHERE project_id=:project AND invocation_id=:id", {"project": project_id, "id": invocation_id})
        if not rows:
            raise KeyError("Invocation not found")
        return self._describe(rows[0])

    def _page(self, project_id, collection, *, limit=50, cursor=None):
        identifier(project_id, "project_id")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        operations = collection == "operations"
        table = "operation_versions" if operations else "invocations"
        keys = ("operation_id", "version") if operations else ("created_at", "invocation_id")
        collation = '"C"' if self.repository.postgres else "BINARY"
        first, second = (name + " COLLATE " + collation for name in keys)
        where, params = "project_id=:project", {"project": project_id, "limit": limit+1}
        if cursor is not None:
            try:
                if not isinstance(cursor, str) or not 1 <= len(cursor) <= 4096:
                    raise ValueError()
                after = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
                if not isinstance(after, dict) or set(after) != {"project", "collection", "first", "second"} or after["project"] != project_id or after["collection"] != collection:
                    raise ValueError()
                identifier(after["first"], "cursor first")
                identifier(after["second"], "cursor second")
            except (ValueError, TypeError, UnicodeError, binascii.Error):
                raise ValueError("Invalid cursor or collection changed") from None
            params.update(after_first=after["first"], after_second=after["second"])
            where += f" AND ({first}>:after_first OR ({keys[0]}=:after_first AND {second}>:after_second))"
        columns = "*" if operations else "project_id,invocation_id,operation_id,operation_version,operation_digest,spec_digest,status,input_count,actor_kind,actor_principal_id,created_at"
        with self.repository.engine.connect() as db:
            self._require_project(db, project_id)
            rows = db.execute(text(f"SELECT {columns} FROM {table} WHERE {where} ORDER BY {first},{second} LIMIT :limit"), params).mappings().all()
        next_cursor = None
        if len(rows) > limit:
            last = rows[limit-1]
            value = {"project": project_id, "collection": collection, "first": last[keys[0]], "second": last[keys[1]]}
            next_cursor = base64.urlsafe_b64encode(canonical(value)[0].encode()).decode()
        return rows[:limit], next_cursor

    def list_operations(self, project_id, *, limit=50, cursor=None):
        rows, next_cursor = self._page(project_id, "operations", limit=limit, cursor=cursor)
        items = []
        for row in rows:
            descriptor = self._describe_operation(row)
            definition = descriptor.pop("definition")
            items.append({**descriptor, "implementation": definition["implementation"],
                          "input_role_count": len(definition["input_roles"]), "output_role_count": len(definition["outputs"])})
        return {"items": items, "next_cursor": next_cursor, "consistency": "live_keyset"}

    def list(self, project_id, *, limit=50, cursor=None):
        rows, next_cursor = self._page(project_id, "invocations", limit=limit, cursor=cursor)
        items = [{"schema_version": VERSION, "kind": "invocation", "project_id": row["project_id"], "invocation_id": row["invocation_id"],
                  "spec_digest": row["spec_digest"], "status": row["status"], "input_count": row["input_count"],
                  "operation": {"operation_id": row["operation_id"], "version": row["operation_version"], "digest": row["operation_digest"]},
                  "requested_by": {"kind": row["actor_kind"], "principal_id": row["actor_principal_id"]}, "created_at": row["created_at"]}
                 for row in rows]
        return {"items": items, "next_cursor": next_cursor, "consistency": "live_keyset"}
