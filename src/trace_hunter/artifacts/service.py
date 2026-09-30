"""Publish arbitrary artifact bytes with verified resource bindings, not scores.

Producer/configuration describe a claim, not evidence that code was executed.
The stable spec digest excludes publication ID, time and authenticated submitter.
Content bytes have their own digest. Reads never create computations or indexes.
"""

import base64
import binascii
from contextlib import contextmanager
import hashlib
import io
import json
import re
import uuid

from sqlalchemy import text

from ..access import Principal
from ..catalog import canonical
from ..content import ContentCorruption, ContentRef
from ..database import Conflict
from ..traces.lookup import decode as decode_hex, encode as encode_hex
from ..traces.service import identifier, utc_now
from ..resources import (MAX_INPUTS, MAX_JSON_BYTES, TOKEN_PATTERN, TARGETS, resource_ref,
    digest_json as _digest, checked_digest as _checked_digest, namespace_token as _token,
    json_object as _json_object, input_bindings as _inputs, resolve_inputs)

VERSION = "trace-hunter/artifact/1"
SPEC_VERSION = "trace-hunter/artifact-spec/1"
QUERY_VERSION = "artifact-query/1"
MAX_CONTENT_BYTES = 8 * 1024 * 1024
MIME = re.compile(r"[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+(?:;[ -~]+)?\Z", re.ASCII)
SUMMARY_COLUMNS = """a.project_id,a.artifact_id,a.artifact_type,a.descriptor_digest,
    a.content_digest,a.size_bytes,a.media_type,a.producer_name_hex,a.producer_version_hex,
    a.config_digest,a.input_count,a.actor_kind,a.actor_principal_id,a.created_at"""


def _claim_text(value):
    if value is not None and (not isinstance(value, str) or len(value) > 256):
        raise ValueError("Producer name/version must be text of at most 256 characters or null")
    encode_hex(value)
    return value


def query_options(value):
    if not isinstance(value, dict) or set(value) - {"filters", "limit", "cursor"}:
        raise ValueError("Unsupported artifact query options")
    filters = value.get("filters", {})
    if not isinstance(filters, dict) or set(filters) - {"artifact_type", "producer_name", "producer_version", "config_digest", "input_ref"}:
        raise ValueError("Unsupported artifact query filter")
    normalized = {}
    for key, item in filters.items():
        if key == "artifact_type":
            if not isinstance(item, list) or not 1 <= len(item) <= 50:
                raise ValueError("artifact_type filter requires 1 to 50 values")
            normalized[key] = sorted({_token(entry, "artifact_type") for entry in item})
        elif key in ("producer_name", "producer_version"):
            normalized[key] = _claim_text(item)
        elif key == "config_digest":
            normalized[key] = _checked_digest(item)
        else:
            normalized[key] = resource_ref(item)
    limit = value.get("limit", 50)
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    cursor = value.get("cursor")
    if cursor is not None and (not isinstance(cursor, str) or not 1 <= len(cursor) <= 4096):
        raise ValueError("Invalid artifact cursor")
    return {"filters": normalized, "limit": limit, "cursor": cursor}


class Artifacts:
    def __init__(self, repository, content):
        self.repository = repository
        self.content = content

    @contextmanager
    def _transaction(self, connection):
        if connection is None:
            with self.repository.engine.begin() as db:
                yield db
        else:
            if connection.engine is not self.repository.engine or not connection.in_transaction():
                raise ValueError("connection must be an active transaction from this repository")
            # Python sqlite3 defers BEGIN even inside SQLAlchemy engine.begin().
            # A SAVEPOINT without a physical outer transaction commits on
            # RELEASE, which would defeat the caller's multi-output rollback.
            if not self.repository.postgres and not connection.connection.driver_connection.in_transaction:
                connection.exec_driver_sql("BEGIN")
            with connection.begin_nested():
                yield connection

    @staticmethod
    def _require_project(db, project_id):
        if db.execute(text("SELECT project_id FROM projects WHERE project_id=:project"), {"project": project_id}).first() is None:
            raise KeyError("Project not found")

    _resolve_inputs = staticmethod(resolve_inputs)

    @staticmethod
    def _insert_inputs(db, project_id, artifact_id, inputs):
        rows = []
        for position, item in enumerate(inputs):
            ref = item["ref"]
            rows.append({"project_id": project_id, "artifact_id": artifact_id, "input_position": position,
                "role": item["role"], "upstream_kind": ref["kind"], "upstream_digest": ref["digest"],
                "trace_run_id": ref["id"] if ref["kind"] == "trace_revision" else None,
                "trace_revision": ref["revision"] if ref["kind"] == "trace_revision" else None,
                "selection_id": ref["id"] if ref["kind"] == "selection_snapshot" else None,
                "upstream_artifact_id": ref["id"] if ref["kind"] == "artifact" else None})
        if rows:
            columns = ",".join(rows[0])
            db.execute(text("INSERT INTO artifact_inputs(" + columns + ") VALUES(" +
                ",".join(":" + key for key in rows[0]) + ")"), rows)

    @staticmethod
    def _summary(row):
        return {"schema_version": VERSION, "kind": "artifact", "project_id": row["project_id"],
            "artifact_id": row["artifact_id"], "revision": None,
            "ref": {"kind": "artifact", "id": row["artifact_id"], "revision": None, "digest": row["descriptor_digest"]},
            "artifact_type": row["artifact_type"], "descriptor_digest": row["descriptor_digest"],
            "content": {"digest": row["content_digest"], "size_bytes": row["size_bytes"], "media_type": row["media_type"]},
            "producer_claim": {"name": decode_hex(row["producer_name_hex"]), "version": decode_hex(row["producer_version_hex"])},
            "config_digest": row["config_digest"], "input_count": row["input_count"],
            "provenance": "declared", "schema_validation": "envelope_only",
            "submitted_by": {"kind": row["actor_kind"], "principal_id": row["actor_principal_id"]},
            "created_at": row["created_at"]}

    @classmethod
    def _describe(cls, row):
        try:
            spec = json.loads(row["spec"])
            if not isinstance(spec, dict) or spec.get("schema_version") != SPEC_VERSION or _digest(spec) != row["descriptor_digest"]:
                raise ValueError()
            result = cls._summary(row)
            expected = {key: spec[key] for key in ("project_id", "artifact_type", "content", "producer_claim")}
            expected.update(config_digest=_digest(spec["config"]), input_count=len(spec["inputs"]))
            if any(result[key] != value for key, value in expected.items()):
                raise ValueError()
            result.pop("input_count")
            return {**result, **{key: spec[key] for key in ("inputs", "producer_claim", "config", "metadata")}}
        except (ValueError, TypeError, KeyError, UnicodeError):
            raise ContentCorruption("Artifact description does not match its digest") from None

    def publish(self, project_id, raw, *, media_type, artifact_type, inputs, producer_claim,
                config, metadata, request_key, actor=None, connection=None):
        identifier(project_id, "project_id")
        identifier(request_key, "request_key")
        if actor is not None and not isinstance(actor, Principal):
            raise ValueError("actor must be a server-authenticated principal")
        if actor is not None and not actor.is_operator and actor.project_id != project_id:
            raise PermissionError("Project access denied")
        if not isinstance(raw, bytes) or len(raw) > MAX_CONTENT_BYTES:
            raise ValueError("Artifact content must be bytes of at most 8 MiB")
        if not isinstance(media_type, str) or not 1 <= len(media_type) <= 255 or not media_type.isascii() or not MIME.fullmatch(media_type):
            raise ValueError("media_type must be an ASCII type/subtype of at most 255 characters without controls")
        artifact_type, inputs = _token(artifact_type, "artifact_type"), _inputs(inputs)
        if not isinstance(producer_claim, dict) or set(producer_claim) != {"name", "version"}:
            raise ValueError("producer_claim requires name and version, each text or null")
        producer = {key: _claim_text(producer_claim[key]) for key in ("name", "version")}
        config, metadata = _json_object(config, "config"), _json_object(metadata, "metadata")
        ref = self.content.put(io.BytesIO(raw), media_type=media_type, max_bytes=MAX_CONTENT_BYTES)
        spec = {"schema_version": SPEC_VERSION, "project_id": project_id, "artifact_type": artifact_type,
                "content": ref.as_dict(), "inputs": inputs, "producer_claim": producer, "config": config, "metadata": metadata}
        descriptor_digest = _digest(spec)
        values = {"project": project_id, "key": request_key, "key_hash": hashlib.sha256(request_key.encode()).hexdigest(),
                  "fingerprint": descriptor_digest, "id": "art_" + uuid.uuid4().hex}
        with self._transaction(connection) as db:
            self._require_project(db, project_id)
            created = db.execute(text("""INSERT INTO artifact_requests(project_id,request_key_hash,request_key,fingerprint,artifact_id)
                VALUES(:project,:key_hash,:key,:fingerprint,:id) ON CONFLICT(project_id,request_key_hash) DO NOTHING"""), values).rowcount == 1
            if not created:
                previous = db.execute(text("SELECT * FROM artifact_requests WHERE project_id=:project AND request_key_hash=:key_hash"), values).mappings().one()
                if previous["request_key"] != request_key or previous["fingerprint"] != descriptor_digest:
                    raise Conflict("Artifact request key already binds another description")
                values["id"] = previous["artifact_id"]
            else:
                self._resolve_inputs(db, project_id, inputs)
                row = {"project_id": project_id, "artifact_id": values["id"], "artifact_type": artifact_type,
                    "descriptor_digest": descriptor_digest, "spec": canonical(spec)[0],
                    "content_digest": ref.digest, "size_bytes": ref.size_bytes, "media_type": ref.media_type,
                    "producer_name_hex": encode_hex(producer["name"]), "producer_version_hex": encode_hex(producer["version"]),
                    "config_digest": _digest(config), "input_count": len(inputs),
                    "actor_kind": "unknown" if actor is None else "operator" if actor.is_operator else "service",
                    "actor_principal_id": actor.principal_id if actor is not None else None, "created_at": utc_now()}
                columns = ",".join(row)
                db.execute(text("INSERT INTO artifacts(" + columns + ") VALUES(" + ",".join(":" + key for key in row) + ")"), row)
                self._insert_inputs(db, project_id, values["id"], inputs)
            row = db.execute(text("SELECT * FROM artifacts WHERE project_id=:project AND artifact_id=:id"), values).mappings().one()
            result = self._describe(row)
        return {"created": created, "artifact": result}

    def get(self, project_id, artifact_id):
        identifier(project_id, "project_id")
        identifier(artifact_id, "artifact_id")
        rows = self.repository.rows("SELECT * FROM artifacts WHERE project_id=:project AND artifact_id=:id", {"project": project_id, "id": artifact_id})
        if not rows:
            raise KeyError("Artifact not found")
        return self._describe(rows[0])

    def read_content(self, project_id, artifact_id):
        artifact = self.get(project_id, artifact_id)
        with self.content.open_verified(ContentRef(**artifact["content"])) as stream:
            return stream.read()

    def query(self, project_id, value):
        identifier(project_id, "project_id")
        request = query_options(value)
        binding = _digest({"version": QUERY_VERSION, "project_id": project_id, "filters": request["filters"]})
        conditions, params = ["a.project_id=:project"], {"project": project_id, "limit": request["limit"] + 1}
        for key, item in request["filters"].items():
            if key == "artifact_type":
                names = []
                for ordinal, artifact_type in enumerate(item):
                    name = "type" + str(ordinal)
                    params[name] = artifact_type
                    names.append(":" + name)
                conditions.append("a.artifact_type IN (" + ",".join(names) + ")")
            elif key in ("producer_name", "producer_version"):
                if item is None:
                    conditions.append("a." + key + "_hex IS NULL")
                else:
                    params[key] = encode_hex(item)
                    conditions.append("a." + key + "_hex=:" + key)
            elif key == "config_digest":
                params[key] = item
                conditions.append("a.config_digest=:config_digest")
            else:
                _, _, _, target_column = TARGETS[item["kind"]]
                params.update(upstream_kind=item["kind"], upstream_id=item["id"], upstream_digest=item["digest"])
                condition = "x.upstream_kind=:upstream_kind AND x." + target_column + "=:upstream_id AND x.upstream_digest=:upstream_digest"
                if item["kind"] == "trace_revision":
                    params["upstream_revision"] = item["revision"]
                    condition += " AND x.trace_revision=:upstream_revision"
                conditions.append("EXISTS(SELECT 1 FROM artifact_inputs x WHERE x.project_id=a.project_id AND x.artifact_id=a.artifact_id AND " + condition + ")")
        collation = '"C"' if self.repository.postgres else "BINARY"
        order = "a.created_at COLLATE " + collation + ",a.artifact_id COLLATE " + collation
        if request["cursor"] is not None:
            try:
                after = json.loads(base64.b64decode(request["cursor"], altchars=b"-_", validate=True))
                if not isinstance(after, dict) or set(after) != {"version", "binding", "created_at", "artifact_id"} or after["version"] != QUERY_VERSION or after["binding"] != binding:
                    raise ValueError()
                identifier(after["created_at"], "cursor created_at")
                identifier(after["artifact_id"], "cursor artifact_id")
            except (binascii.Error, UnicodeError, ValueError, TypeError):
                raise ValueError("Invalid artifact cursor or query changed") from None
            params.update(after_at=after["created_at"], after_id=after["artifact_id"])
            conditions.append("(a.created_at COLLATE " + collation + ">:after_at OR (a.created_at=:after_at AND a.artifact_id COLLATE " + collation + ">:after_id))")
        sql = "SELECT " + SUMMARY_COLUMNS + " FROM artifacts a WHERE " + " AND ".join(conditions) + " ORDER BY " + order + " LIMIT :limit"
        with self.repository.engine.connect() as db:
            self._require_project(db, project_id)
            rows = db.execute(text(sql), params).mappings().all()
        items = [self._summary(row) for row in rows[:request["limit"]]]
        cursor = None
        if len(rows) > request["limit"]:
            last = rows[request["limit"] - 1]
            encoded = canonical({"version": QUERY_VERSION, "binding": binding,
                "created_at": last["created_at"], "artifact_id": last["artifact_id"]})[0].encode()
            cursor = base64.urlsafe_b64encode(encoded).decode()
        return {"items": items, "next_cursor": cursor, "query_digest": binding, "consistency": "live_keyset"}
