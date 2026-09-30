"""Explicitly freeze query membership without evaluating or rereading sources."""

import io
import json
import uuid

from sqlalchemy import text

from ..access import Principal
from ..catalog import canonical
from ..content import ContentCorruption, ContentRef
from ..database import Conflict
from ..query.service import BASE_FROM, identifier, lookup_health, options, predicate
from ..traces.lookup import LookupUnavailable
from ..traces.service import utc_now

VERSION = "selection-snapshot/1"
MANIFEST_VERSION = "trace-hunter/selection-manifest/1"
MAX_MEMBERS = 10000


def query_spec(value):
    if not isinstance(value, dict) or set(value) - {"filters", "revisions"}:
        raise ValueError("A selection accepts filters and revisions, not a page or cursor")
    normalized = options(value)
    return {key: normalized[key] for key in ("filters", "revisions")}


class SelectionSnapshots:
    def __init__(self, repository, content):
        self.repository = repository
        self.content = content

    @staticmethod
    def _describe(row):
        return {"kind": "selection_snapshot", "project_id": row["project_id"],
                "selection_id": row["selection_id"], "query_digest": row["query_digest"],
                "query": json.loads(row["query_spec"]), "member_count": row["member_count"],
                "manifest": {"digest": row["manifest_digest"], "size_bytes": row["manifest_size"],
                             "media_type": "application/json"},
                "created_at": row["created_at"], "consistency": "fixed_revision_membership",
                "actor": {"kind": row["actor_kind"], "principal_id": row["actor_principal_id"]}}

    def freeze(self, project_id, value, *, request_key, actor=None):
        identifier(project_id, "project_id")
        identifier(request_key, "request_key")
        spec = query_spec(value)
        if actor is not None and not isinstance(actor, Principal):
            raise ValueError("actor must be a server-authenticated principal")
        if actor is not None and not actor.is_operator and actor.project_id != project_id:
            raise PermissionError("Project access denied")
        actor_kind = "unknown" if actor is None else "operator" if actor.is_operator else "service"
        actor_id = actor.principal_id if actor is not None else None
        fingerprint = canonical({"version": VERSION, "project_id": project_id, "query": spec})[1]
        values = {"project": project_id, "key": request_key, "fingerprint": fingerprint,
                  "selection": "sel_" + uuid.uuid4().hex}
        with self.repository.engine.begin() as db:
            created = db.execute(text("""INSERT INTO selection_requests(project_id,request_key,fingerprint,selection_id)
                VALUES(:project,:key,:fingerprint,:selection) ON CONFLICT(project_id,request_key) DO NOTHING"""), values).rowcount == 1
            if not created:
                previous = db.execute(text("""SELECT fingerprint,selection_id FROM selection_requests
                    WHERE project_id=:project AND request_key=:key"""), values).mappings().one()
                if previous["fingerprint"] != fingerprint:
                    raise Conflict("Selection request key already binds another query")
                values["selection"] = previous["selection_id"]
            else:
                request = options(spec)
                where, params = predicate(project_id, request, postgres=self.repository.postgres)
                health_sql, health_params = lookup_health(project_id, request, postgres=self.repository.postgres)
                params.update(health_params)
                params["limit"] = MAX_MEMBERS + 1
                order = 'r.run_id COLLATE "C"' if self.repository.postgres else 'r.run_id COLLATE BINARY'
                # One SELECT establishes membership. Subsequent head/index
                # changes cannot alter the fetched immutable revision references.
                members_sql = "SELECT r.run_id,r.revision,r.content_digest" + BASE_FROM + \
                    " WHERE " + where + f" ORDER BY {order},r.revision LIMIT :limit"
                collation = '"C"' if self.repository.postgres else "BINARY"
                # The guard and membership SELECT must see the same snapshot.
                # Otherwise an incomplete lookup can silently omit a member.
                checked_sql = "WITH health AS (" + health_sql + "),members AS (" + members_sql + ") " + \
                    "SELECT * FROM (SELECT 1 AS _health,missing_lookup AS _missing," + \
                    "NULL AS run_id,NULL AS revision,NULL AS content_digest FROM health UNION ALL " + \
                    "SELECT 0 AS _health,0 AS _missing,run_id,revision,content_digest FROM members) checked " + \
                    "ORDER BY _health DESC,run_id COLLATE " + collation + ",revision"
                checked = db.execute(text(checked_sql), params).mappings().all()
                if checked[0]["_missing"]:
                    raise LookupUnavailable()
                rows = checked[1:]
                if len(rows) > MAX_MEMBERS:
                    raise ValueError("Selection exceeds 10000 members; narrow the query")
                members = [{"position": position, "run_id": row["run_id"], "revision": row["revision"],
                            "content_digest": row["content_digest"]} for position, row in enumerate(rows)]
                manifest = {"schema_version": MANIFEST_VERSION, "project_id": project_id,
                            "selection_id": values["selection"], "query_digest": fingerprint, "members": members}
                raw = canonical(manifest)[0].encode()
                ref = self.content.put(io.BytesIO(raw), media_type="application/json")
                values.update(query=canonical(spec)[0], count=len(members), digest=ref.digest,
                              size=ref.size_bytes, created=utc_now(), actor_kind=actor_kind, actor_id=actor_id)
                db.execute(text("""INSERT INTO selection_snapshots(project_id,selection_id,query_digest,query_spec,
                    member_count,manifest_digest,manifest_size,created_at,actor_kind,actor_principal_id)
                    VALUES(:project,:selection,:fingerprint,:query,:count,:digest,:size,:created,:actor_kind,:actor_id)"""), values)
                if members:
                    db.execute(text("""INSERT INTO selection_members(project_id,selection_id,position,run_id,revision,content_digest)
                        VALUES(:project,:selection,:position,:run_id,:revision,:content_digest)"""),
                        [{**member, "project": project_id, "selection": values["selection"]} for member in members])
            row = db.execute(text("SELECT * FROM selection_snapshots WHERE project_id=:project AND selection_id=:selection"),
                             values).mappings().one()
            descriptor = self._describe(row)
        return {"created": created, "selection": descriptor}

    def get(self, project_id, selection_id):
        identifier(project_id, "project_id")
        identifier(selection_id, "selection_id")
        rows = self.repository.rows("SELECT * FROM selection_snapshots WHERE project_id=:project AND selection_id=:selection",
                                    {"project": project_id, "selection": selection_id})
        if not rows:
            raise KeyError("Selection not found")
        return self._describe(rows[0])

    def members(self, project_id, selection_id, *, limit=100, after=None):
        descriptor = self.get(project_id, selection_id)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        if after is not None and (type(after) is not int or not 0 <= after < MAX_MEMBERS):
            raise ValueError("after must be a valid non-negative member position")
        start = 0 if after is None else after + 1
        params = {"project": project_id, "selection": selection_id, "start": start, "limit": limit}
        rows = self.repository.rows("""SELECT position,run_id,revision,content_digest FROM selection_members
            WHERE project_id=:project AND selection_id=:selection AND position>=:start
            ORDER BY position LIMIT :limit""", params)
        expected_count = min(limit, max(0, descriptor["member_count"] - start))
        if len(rows) != expected_count or [row["position"] for row in rows] != list(range(start, start + expected_count)):
            raise ContentCorruption("Selection member index does not match its manifest count")
        items = [dict(row) for row in rows]
        next_after = items[-1]["position"] if items and start + len(items) < descriptor["member_count"] else None
        return {"items": items, "next_after": next_after, "member_count": descriptor["member_count"],
                "manifest_digest": descriptor["manifest"]["digest"]}

    def read_manifest(self, project_id, selection_id):
        descriptor = self.get(project_id, selection_id)
        with self.content.open_verified(ContentRef(**descriptor["manifest"])) as stream:
            return stream.read()
