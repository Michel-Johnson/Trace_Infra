"""Curated exact-substring postings and opt-in evaluation search statistics."""

from datetime import datetime, timezone

from sqlalchemy import text

from ..traces.index import PROJECTOR_VERSION
from ..traces.service import identifier

MAX_ACTIVE_TERMS = 16
MIN_TERM_LENGTH = 2
MAX_TERM_LENGTH = 128


def valid_term(value):
    if (not isinstance(value, str) or not MIN_TERM_LENGTH <= len(value) <= MAX_TERM_LENGTH
            or "\x00" in value or not value.strip()):
        raise ValueError("Search term must be 2 to 128 non-blank characters")
    return value


def _like(value):
    return "%" + value.replace("!", "!!").replace("%", "!%").replace("_", "!_") + "%"


class HotSearchTerms:
    def __init__(self, repository):
        self.repository = repository

    def active(self, project_id, term):
        return bool(self.repository.rows(
            "SELECT 1 FROM trace_search_hot_terms WHERE project_id=:project AND term=:term AND active",
            {"project": project_id, "term": term}))

    def list(self, project_id):
        identifier(project_id, "project_id")
        rows = self.repository.rows("""
            SELECT t.term,t.search_count,t.total_latency_ms,t.last_total_count,t.active,
                   CAST(t.last_searched_at AS TEXT) AS last_searched_at,
                   CAST(t.indexed_at AS TEXT) AS indexed_at,
                   (SELECT count(*) FROM trace_search_hot_postings p
                    WHERE p.project_id=t.project_id AND p.term=t.term
                      AND p.projector_version=:projector) AS posting_count
            FROM trace_search_hot_terms t WHERE t.project_id=:project
            ORDER BY t.active DESC,t.search_count DESC,t.term
        """, {"project": project_id, "projector": PROJECTOR_VERSION})
        return {"items": [{"term": row["term"], "search_count": row["search_count"],
                           "average_latency_ms": (row["total_latency_ms"] / row["search_count"]
                                                  if row["search_count"] else None),
                           "last_total_count": row["last_total_count"],
                           "active": bool(row["active"]), "posting_count": row["posting_count"],
                           "last_searched_at": row["last_searched_at"], "indexed_at": row["indexed_at"]}
                          for row in rows], "max_active_terms": MAX_ACTIVE_TERMS,
                "observation_scope": "evaluation_literal_only"}

    def observe(self, project_id, term, latency_ms, total_count):
        identifier(project_id, "project_id")
        if not isinstance(term, str) or not MIN_TERM_LENGTH <= len(term) <= MAX_TERM_LENGTH:
            return
        valid_term(term)
        if latency_ms < 0 or total_count < 0:
            raise ValueError("Invalid search observation")
        now = datetime.now(timezone.utc).isoformat()
        with self.repository.engine.begin() as db:
            db.execute(text("""
                INSERT INTO trace_search_hot_terms(project_id,term,search_count,total_latency_ms,
                                                  last_total_count,last_searched_at)
                VALUES(:project,:term,1,:latency,:total,:now)
                ON CONFLICT(project_id,term) DO UPDATE SET
                    search_count=trace_search_hot_terms.search_count+1,
                    total_latency_ms=trace_search_hot_terms.total_latency_ms+:latency,
                    last_total_count=:total,last_searched_at=:now
            """), {"project": project_id, "term": term, "latency": latency_ms,
                   "total": total_count, "now": now})

    def promote(self, project_id, term):
        identifier(project_id, "project_id")
        valid_term(term)
        with self.repository.engine.begin() as db:
            if self.repository.postgres:
                db.execute(text("SET LOCAL statement_timeout = '30000ms'"))
                # Serialize publication with projector writes so no document falls between
                # the backfill snapshot and incremental maintenance.
                db.execute(text("LOCK TABLE trace_search_documents IN SHARE ROW EXCLUSIVE MODE"))
            active = db.execute(text("""SELECT count(*) FROM trace_search_hot_terms
                WHERE project_id=:project AND active AND term<>:term"""),
                {"project": project_id, "term": term}).scalar_one()
            if active >= MAX_ACTIVE_TERMS:
                raise ValueError("Search term vocabulary limit reached")
            db.execute(text("""INSERT INTO trace_search_hot_terms(project_id,term)
                VALUES(:project,:term) ON CONFLICT(project_id,term) DO NOTHING"""),
                {"project": project_id, "term": term})
            db.execute(text("""DELETE FROM trace_search_hot_postings
                WHERE project_id=:project AND term=:term"""),
                {"project": project_id, "term": term})
            condition = "d.text LIKE :needle ESCAPE '!'" if self.repository.postgres else "instr(d.text,:term)>0"
            db.execute(text("""INSERT INTO trace_search_hot_postings(
                project_id,term,run_id,revision,projector_version,document_ordinal,
                object_kind,field,span_id)
                SELECT d.project_id,:term,d.run_id,d.revision,d.projector_version,d.document_ordinal,
                       d.object_kind,d.field,d.span_id
                FROM trace_search_documents d
                WHERE d.project_id=:project AND d.projector_version=:projector AND """ + condition),
                {"project": project_id, "term": term, "projector": PROJECTOR_VERSION,
                 "needle": _like(term)})
            now = datetime.now(timezone.utc).isoformat()
            db.execute(text("""UPDATE trace_search_hot_terms SET active=TRUE,indexed_at=:now
                WHERE project_id=:project AND term=:term"""),
                {"project": project_id, "term": term, "now": now})
        return self.get(project_id, term)

    def demote(self, project_id, term):
        identifier(project_id, "project_id")
        valid_term(term)
        with self.repository.engine.begin() as db:
            changed = db.execute(text("""UPDATE trace_search_hot_terms
                SET active=FALSE,indexed_at=NULL WHERE project_id=:project AND term=:term"""),
                {"project": project_id, "term": term}).rowcount
            if not changed:
                raise KeyError("Search term not found")
            db.execute(text("""DELETE FROM trace_search_hot_postings
                WHERE project_id=:project AND term=:term"""),
                {"project": project_id, "term": term})
        return self.get(project_id, term)

    def get(self, project_id, term):
        for item in self.list(project_id)["items"]:
            if item["term"] == term:
                return item
        raise KeyError("Search term not found")
