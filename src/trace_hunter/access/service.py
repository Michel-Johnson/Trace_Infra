"""Stateless operator identity and the project registry.

Credentials are deliberately not persisted by the eight-table core schema.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import re

from sqlalchemy import text

from ..database import Conflict

SCOPES = frozenset(("traces:read", "traces:write", "traces:index", "traces:search:analysis",
                    "traces:model_context"))
PROJECT_SLUG = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z", re.ASCII)


def identifier(value, field):
    if not isinstance(value, str) or not value.strip() or len(value) > 512 or "\x00" in value:
        raise ValueError(field + " must be a non-empty string of at most 512 characters")
    return value


class AuthenticationError(Exception):
    pass


@dataclass(frozen=True)
class Principal:
    project_id: str | None
    principal_id: str | None
    scopes: frozenset[str]
    is_operator: bool = False

    @classmethod
    def operator(cls):
        return cls(None, None, SCOPES, is_operator=True)

    @classmethod
    def evaluator(cls, project_id):
        identifier(project_id, "project_id")
        return cls(project_id, "evaluator", frozenset(("traces:model_context",)))

    def require(self, project_id, scope):
        identifier(project_id, "project_id")
        if scope not in SCOPES or not self.is_operator and (
                self.project_id != project_id or scope not in self.scopes):
            raise PermissionError("Project or scope access denied")


class Projects:
    def __init__(self, repository):
        self.repository = repository

    @staticmethod
    def _project(row):
        created = datetime.fromisoformat(row["created_at"])
        if created.tzinfo is None: created = created.replace(tzinfo=timezone.utc)
        return {"project_id": row["project_id"], "name": row["name"],
                "created_at": created.astimezone(timezone.utc).isoformat()}

    def get_project(self, project_id):
        identifier(project_id, "project_id")
        rows = self.repository.rows("SELECT * FROM projects WHERE project_id=:id", {"id": project_id})
        if not rows: raise KeyError("Project not found")
        return self._project(rows[0])

    def create_project(self, project_id, name):
        identifier(project_id, "project_id")
        if not isinstance(name, str) or not name.strip() or len(name) > 256 or "\x00" in name:
            raise ValueError("name must be a non-empty string of at most 256 characters")
        with self.repository.engine.begin() as db:
            previous = db.execute(text("SELECT * FROM projects WHERE project_id=:id"), {"id": project_id}).mappings().first()
            if previous is not None:
                if previous["name"] != name: raise Conflict("Project ID already binds a different name")
                return self._project(previous)
            if not PROJECT_SLUG.fullmatch(project_id):
                raise ValueError("New project_id must be an ASCII URL-safe slug of at most 128 characters")
            db.execute(text("INSERT INTO projects(project_id,name,created_at) VALUES(:id,:name,:at)"),
                       {"id": project_id, "name": name, "at": datetime.now(timezone.utc).isoformat()})
        return self.get_project(project_id)

    def list_projects(self, *, limit=50, after=None):
        if type(limit) is not int or not 1 <= limit <= 100: raise ValueError("limit must be between 1 and 100")
        if after is not None: identifier(after, "after")
        ordered = 'project_id COLLATE "C"' if self.repository.postgres else "project_id COLLATE BINARY"
        condition = " WHERE " + ordered + " > :after" if after is not None else ""
        rows = self.repository.rows("SELECT * FROM projects" + condition + " ORDER BY " + ordered + " LIMIT :limit",
                                    {"after": after, "limit": limit + 1})
        return {"items": [self._project(row) for row in rows[:limit]],
                "next_after": rows[limit - 1]["project_id"] if len(rows) > limit else None}


# Temporary source compatibility for core callers created before the table
# consolidation.  It provides project operations only, never credentials.
ServiceIdentities = Projects
