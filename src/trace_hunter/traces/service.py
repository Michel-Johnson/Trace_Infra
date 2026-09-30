"""Append-only trace revisions with explicit concurrency and import identity."""

import io
import json
from contextlib import nullcontext
from datetime import datetime, timezone

from sqlalchemy import text

from ..catalog import canonical
from ..content import ContentRef
from ..content.store import ContentStore
from ..database import Conflict, Repository
from .formats import inspect_document
from .lookup import encode

MAX_REVISION = 2147483647


def identifier(value, field):
    if not isinstance(value, str) or not value.strip() or len(value) > 512 or "\x00" in value:
        raise ValueError(f"{field} must be a non-empty string of at most 512 characters")
    return value


def utc_now():
    return datetime.now(timezone.utc).isoformat()


class TraceRevisions:
    def __init__(self, repository: Repository, content: ContentStore):
        self.repository = repository
        self.content = content

    def append(self, project_id: str, raw: bytes, *, request_key: str,
               expected_previous: int = 0, derivation: str = "capture", connection=None) -> dict:
        identifier(project_id, "project_id")
        identifier(request_key, "request_key")
        if type(expected_previous) is not int or not 0 <= expected_previous < MAX_REVISION:
            raise ValueError(f"expected_previous must be between 0 and {MAX_REVISION - 1}")
        if derivation not in ("capture", "supplement", "correction", "legacy_import"):
            raise ValueError("Unsupported derivation")
        _, summary = inspect_document(raw)
        run_id = identifier(summary["run_id"], "run_id")
        ref = self.content.put(io.BytesIO(raw), media_type="application/json")
        revision = expected_previous + 1
        _, fingerprint = canonical({
            "project_id": project_id, "run_id": run_id, "content": ref.as_dict(),
            "expected_previous": expected_previous, "derivation": derivation,
        })
        values = {
            "project_id": project_id, "request_key": request_key, "fingerprint": fingerprint,
            "run_id": run_id, "revision": revision, "previous": expected_previous or None,
            "expected": expected_previous, "digest": ref.digest, "size": ref.size_bytes,
            "media_type": ref.media_type, "format": summary["format_version"],
            "derivation": derivation, "metadata": canonical(summary)[0], "created_at": utc_now(),
            **{field + "_hex": encode(summary.get(field))
               for field in ("query_id", "env_id", "harness", "model", "status")},
            "title_hex": encode(summary.get("title")),
        }
        transaction = self.repository.engine.begin() if connection is None else nullcontext(connection)
        with transaction as db:
            # Native imports historically accept a project namespace directly.
            # Register it in the same transaction so every accepted namespace is
            # discoverable and can receive a scoped service principal.
            db.execute(text("""INSERT INTO projects(project_id,name,created_at)
                VALUES(:project_id,:project_id,:created_at)
                ON CONFLICT(project_id) DO NOTHING"""), values)
            previous = db.execute(text("""
                SELECT request_fingerprint,run_id,revision FROM trace_revisions
                WHERE project_id=:project_id AND request_key=:request_key
            """), values).mappings().first()
            if previous is not None:
                created = False
                if previous["request_fingerprint"] != fingerprint:
                    raise Conflict("Import request key already binds different content or options")
                target = (previous["run_id"], previous["revision"])
            else:
                db.execute(text("""
                    INSERT INTO traces(project_id,run_id,latest_revision,query_id_hex,env_id_hex,
                        harness_hex,model_hex,status_hex,title_hex,created_at,updated_at)
                    VALUES(:project_id,:run_id,0,:query_id_hex,:env_id_hex,:harness_hex,
                        :model_hex,:status_hex,:title_hex,:created_at,:created_at)
                    ON CONFLICT(project_id,run_id) DO NOTHING
                """), values)
                if not self.repository.postgres:
                    db.execute(text("""UPDATE traces SET updated_at=updated_at
                        WHERE project_id=:project_id AND run_id=:run_id"""), values)
                lock = " FOR UPDATE" if self.repository.postgres else ""
                head = db.execute(text("""SELECT latest_revision FROM traces
                    WHERE project_id=:project_id AND run_id=:run_id""" + lock), values).mappings().one()
                # A concurrent same-key request may have committed while this
                # transaction waited for the trace lock.
                bound = db.execute(text("""SELECT request_fingerprint,run_id,revision
                    FROM trace_revisions WHERE project_id=:project_id AND request_key=:request_key"""), values).mappings().first()
                if bound is not None:
                    if bound["request_fingerprint"] != fingerprint:
                        raise Conflict("Import request key already binds different content or options")
                    created = False
                    target = (bound["run_id"], bound["revision"])
                elif head["latest_revision"] != expected_previous:
                    raise Conflict("Trace revision changed; read the current head before appending")
                else:
                    db.execute(text("""
                    INSERT INTO trace_revisions(project_id,run_id,revision,request_key,request_fingerprint,
                        content_digest,size_bytes,media_type,format_version,previous_revision,derivation,
                        metadata,query_id_hex,env_id_hex,harness_hex,model_hex,status_hex,
                        projector_version,projection_state,identity_basis,created_at)
                    VALUES(:project_id,:run_id,:revision,:request_key,:fingerprint,:digest,:size,
                        :media_type,:format,:previous,:derivation,:metadata,:query_id_hex,:env_id_hex,
                        :harness_hex,:model_hex,:status_hex,NULL,'unindexed',
                        :identity_basis,:created_at)
                """), {**values, "identity_basis": "legacy_compatibility"
                         if summary["format_version"] == "trace-hunter/1.0" else "source"})
                    advanced = db.execute(text("""
                        UPDATE traces SET latest_revision=:revision,query_id_hex=:query_id_hex,
                        env_id_hex=:env_id_hex,harness_hex=:harness_hex,model_hex=:model_hex,
                        status_hex=:status_hex,title_hex=:title_hex,updated_at=:created_at
                        WHERE project_id=:project_id AND run_id=:run_id AND latest_revision=:expected
                    """), values).rowcount
                    if advanced != 1:
                        raise Conflict("Trace revision changed; read the current head before appending")
                    created = True
                    target = (run_id, revision)
            row = db.execute(text("""
                SELECT * FROM trace_revisions
                WHERE project_id=:project_id AND run_id=:run_id AND revision=:revision
            """), {"project_id": project_id, "run_id": target[0], "revision": target[1]}).mappings().one()
            descriptor = self._describe(row)
        return {"created": created, "revision": descriptor}

    @staticmethod
    def _describe(row):
        return {
            "kind": "trace_revision", "project_id": row["project_id"],
            "run_id": row["run_id"], "revision": row["revision"],
            "content": {"digest": row["content_digest"], "size_bytes": row["size_bytes"],
                        "media_type": row["media_type"]},
            "format_version": row["format_version"],
            "previous_revision": row["previous_revision"],
            "derivation": row["derivation"], "metadata": json.loads(row["metadata"]),
            "created_at": row["created_at"],
        }

    def get(self, project_id: str, run_id: str, revision: int | None = None) -> dict:
        identifier(project_id, "project_id")
        identifier(run_id, "run_id")
        if revision is not None and (type(revision) is not int or not 1 <= revision <= MAX_REVISION):
            raise ValueError(f"revision must be between 1 and {MAX_REVISION}")
        params = {"project_id": project_id, "run_id": run_id, "revision": revision}
        where = "r.revision=:revision" if revision is not None else """r.revision=(
            SELECT latest_revision FROM traces
            WHERE project_id=:project_id AND run_id=:run_id)"""
        rows = self.repository.rows("""
            SELECT r.* FROM trace_revisions r
            WHERE r.project_id=:project_id AND r.run_id=:run_id AND """ + where, params)
        if not rows:
            raise KeyError("Trace revision not found")
        return self._describe(rows[0])

    def read(self, project_id: str, run_id: str, revision: int) -> bytes:
        descriptor = self.get(project_id, run_id, revision)
        ref = ContentRef(**descriptor["content"])
        with self.content.open_verified(ref) as source:
            return source.read()

    def history(self, project_id: str, run_id: str, *, before: int | None = None,
                limit: int = 50) -> dict:
        identifier(project_id, "project_id")
        identifier(run_id, "run_id")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        if before is not None and (type(before) is not int or not 1 <= before <= MAX_REVISION):
            raise ValueError(f"before must be between 1 and {MAX_REVISION}")
        self.get(project_id, run_id)
        params = {"project_id": project_id, "run_id": run_id, "before": before, "limit": limit + 1}
        condition = " AND revision < :before" if before is not None else ""
        rows = self.repository.rows("""
            SELECT * FROM trace_revisions
            WHERE project_id=:project_id AND run_id=:run_id""" + condition +
            " ORDER BY revision DESC LIMIT :limit", params)
        return {"items": [self._describe(row) for row in rows[:limit]],
                "next_before": rows[limit - 1]["revision"] if len(rows) > limit else None}
