"""Lossless, rebuildable query metadata; original revision metadata stays intact."""

import json
import re

from sqlalchemy import text

from ..content import ContentCorruption

FIELDS = ("query_id", "env_id", "harness", "model", "status")
INDEXED_FIELDS = frozenset(("query_id", "env_id", "harness"))
PREFIX_HEX_LENGTH = 128
BACKFILL_PAGE_SIZE = 500
HEX = re.compile(r"(?:[0-9a-f]{2})*\Z", re.ASCII)


class LookupUnavailable(ContentCorruption):
    def __init__(self):
        super().__init__("Trace revision lookup is incomplete; run an explicit lookup backfill")


def encode(value):
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("Lookup metadata must be text or null")
    try:
        return value.encode("utf-8").hex()
    except UnicodeError:
        raise ValueError("Lookup metadata must be valid UTF-8") from None


def decode(value):
    if value is None:
        return None
    if not isinstance(value, str) or not HEX.fullmatch(value):
        raise ContentCorruption("Trace revision lookup contains invalid hexadecimal text")
    try:
        return bytes.fromhex(value).decode("utf-8")
    except UnicodeError:
        raise ContentCorruption("Trace revision lookup contains invalid UTF-8") from None


def register(db, project_id, run_id, revision, metadata):
    """Register inside the revision transaction; a conflicting row is corruption."""
    if not isinstance(metadata, dict):
        raise ValueError("Revision metadata must be an object")
    values = {"project_id": project_id, "run_id": run_id, "revision": revision,
              **{field + "_hex": encode(metadata.get(field)) for field in FIELDS}}
    columns = ",".join(values)
    created = db.execute(text("INSERT INTO trace_revision_lookup(" + columns + ") VALUES(" +
        ",".join(":" + key for key in values) + ") ON CONFLICT(project_id,run_id,revision) DO NOTHING"), values).rowcount == 1
    row = db.execute(text("""SELECT query_id_hex,env_id_hex,harness_hex,model_hex,status_hex
        FROM trace_revision_lookup WHERE project_id=:project_id AND run_id=:run_id AND revision=:revision"""), values).mappings().one()
    if any(row[field + "_hex"] != values[field + "_hex"] for field in FIELDS):
        raise ContentCorruption("Trace revision lookup does not match immutable revision metadata")
    return created


def backfill(db):
    """Fill missing rows in bounded pages within the caller's transaction.

    Existing rows are not rewritten. This is an explicit migration/maintenance
    operation, never a query-side repair or a source-content read.
    """
    postgres = db.dialect.name == "postgresql"
    collation = '"C"' if postgres else "BINARY"
    count, after = 0, None
    while True:
        seek = "" if after is None else f""" AND (r.project_id COLLATE {collation},
            r.run_id COLLATE {collation},r.revision) > (:after_project,:after_run,:after_revision)"""
        params = {"limit": BACKFILL_PAGE_SIZE}
        if after is not None:
            params.update(after_project=after["project_id"], after_run=after["run_id"], after_revision=after["revision"])
        rows = db.execute(text(f"""SELECT r.project_id,r.run_id,r.revision,r.metadata
            FROM trace_revisions r LEFT JOIN trace_revision_lookup l
                ON l.project_id=r.project_id AND l.run_id=r.run_id AND l.revision=r.revision
            WHERE l.run_id IS NULL {seek}
            ORDER BY r.project_id COLLATE {collation},r.run_id COLLATE {collation},r.revision
            LIMIT :limit"""), params).mappings().all()
        if not rows:
            return count
        for row in rows:
            try:
                metadata = json.loads(row["metadata"])
            except (ValueError, TypeError):
                raise ContentCorruption("Trace revision metadata cannot be decoded for lookup backfill") from None
            count += register(db, row["project_id"], row["run_id"], row["revision"], metadata)
        after = rows[-1]
