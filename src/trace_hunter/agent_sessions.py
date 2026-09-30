"""Persistent, process-safe conversation journal for the built-in agent.

This is operational state beside the content store. Canonical Trace documents
remain in the existing immutable Trace repository.
"""

import json
import hmac
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def _now():
    return datetime.now(timezone.utc).isoformat()


class AgentSessions:
    def __init__(self, directory):
        self.root = Path(directory)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.root / "sessions.sqlite3"
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL,
                    project_id TEXT NOT NULL, trace_project_id TEXT NOT NULL,
                    claude_session_id TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS agent_sessions_owner
                    ON sessions(owner_id,updated_at DESC);
                CREATE TABLE IF NOT EXISTS turns (
                    turn_id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
                    ordinal INTEGER NOT NULL, prompt TEXT NOT NULL,
                    state TEXT NOT NULL, task_id TEXT, run_id TEXT,
                    error TEXT, raw_ref TEXT, created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(session_id,ordinal));
                CREATE INDEX IF NOT EXISTS agent_turns_state ON turns(state,created_at);
                CREATE TABLE IF NOT EXISTS events (
                    session_id TEXT NOT NULL, seq INTEGER NOT NULL,
                    turn_id TEXT NOT NULL, type TEXT NOT NULL,
                    payload TEXT NOT NULL, at TEXT NOT NULL,
                    PRIMARY KEY(session_id,seq));
                CREATE INDEX IF NOT EXISTS agent_events_turn_type
                    ON events(turn_id,type,seq);
                CREATE TABLE IF NOT EXISTS attachments (
                    attachment_id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
                    name TEXT NOT NULL, content_ref TEXT NOT NULL,
                    created_at TEXT NOT NULL);
            """)
        os.chmod(self.path, 0o600)

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=30000")
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def create(self, owner_id, project_id, trace_project_id, *, session_id=None):
        session_id, at = session_id or str(uuid.uuid4()), _now()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT owner_id,project_id,trace_project_id FROM sessions WHERE session_id=?",
                                  (session_id,)).fetchone()
            if previous is not None:
                if (previous["owner_id"], previous["project_id"], previous["trace_project_id"]) != (
                        owner_id, project_id, trace_project_id):
                    raise ValueError("Agent session identity already belongs to another import")
                return self.get(session_id, owner_id)
            db.execute("INSERT INTO sessions VALUES(?,?,?,?,?,?,?)",
                       (session_id, owner_id, project_id, trace_project_id, None, at, at))
        return self.get(session_id, owner_id)

    def get(self, session_id, owner_id):
        with self._db() as db:
            row = db.execute("SELECT * FROM sessions WHERE session_id=? AND owner_id=?",
                             (session_id, owner_id)).fetchone()
            if row is None:
                raise KeyError("Agent session not found")
            turns = db.execute("SELECT turn_id,ordinal,prompt,state,task_id,run_id,error,created_at,updated_at "
                               "FROM turns WHERE session_id=? ORDER BY ordinal", (session_id,)).fetchall()
            return {**dict(row), "turns": [dict(turn) for turn in turns]}

    def internal_get(self, session_id):
        with self._db() as db:
            row = db.execute("SELECT owner_id FROM sessions WHERE session_id=?", (session_id,)).fetchone()
        if row is None:
            raise KeyError("Agent session not found")
        return self.get(session_id, row["owner_id"])

    def internal_turn(self, turn_id):
        with self._db() as db:
            row = db.execute("SELECT t.*,s.owner_id,s.project_id,s.trace_project_id "
                             "FROM turns t JOIN sessions s ON s.session_id=t.session_id "
                             "WHERE t.turn_id=?", (turn_id,)).fetchone()
        if row is None:
            raise KeyError("Agent turn not found")
        return dict(row)

    def trace_labels(self, identities):
        """Return bounded user-query labels only for sealed turns in their actual projects."""
        requested = list(dict.fromkeys(identities))
        turn_ids = sorted({run_id.removeprefix("agent-") for _, run_id in requested
                           if run_id.startswith("agent-")})
        if not turn_ids:
            return []
        placeholders = ",".join("?" for _ in turn_ids)
        with self._db() as db:
            rows = db.execute(
                "SELECT t.turn_id,t.run_id,t.prompt,s.trace_project_id "
                "FROM turns t JOIN sessions s ON s.session_id=t.session_id "
                f"WHERE t.turn_id IN ({placeholders}) AND t.run_id IS NOT NULL",
                turn_ids).fetchall()
        labels = {}
        for row in rows:
            key = (row["trace_project_id"], row["run_id"])
            if row["run_id"] != "agent-" + row["turn_id"] or key not in requested:
                continue
            prompt = " ".join(row["prompt"].split())
            if prompt:
                labels[key] = prompt[:159] + "…" if len(prompt) > 160 else prompt
        return [{"project_id": project_id, "run_id": run_id, "title": labels[(project_id, run_id)]}
                for project_id, run_id in requested if (project_id, run_id) in labels]

    def list(self, owner_id, limit=50):
        with self._db() as db:
            rows = db.execute(
                "SELECT s.session_id,s.project_id,s.trace_project_id,s.claude_session_id,"
                "s.created_at,s.updated_at,"
                "t.turn_id,t.task_id,t.state,t.run_id,"
                "EXISTS(SELECT 1 FROM events e WHERE e.turn_id=t.turn_id "
                "AND e.type='native_terminal_ready') AS native_terminal,"
                "(SELECT name FROM attachments a WHERE a.session_id=s.session_id "
                "ORDER BY a.created_at,a.attachment_id LIMIT 1) AS source_name "
                "FROM sessions s LEFT JOIN turns t ON t.turn_id=("
                "SELECT turn_id FROM turns WHERE session_id=s.session_id "
                "ORDER BY ordinal DESC LIMIT 1) "
                "WHERE s.owner_id=? ORDER BY s.updated_at DESC,s.session_id DESC LIMIT ?",
                (owner_id, limit)).fetchall()
        return [{**dict(row), "kind": "auto_import" if row["task_id"] else "agent"}
                for row in rows]

    def task_links(self, owner_id, project_id, task_ids):
        """Resolve task identities to sessions visible to this browser owner."""
        return self._task_links(project_id, task_ids, owner_id=owner_id)

    def project_task_links(self, project_id, task_ids):
        """Resolve sessions for tasks already authorized through project access."""
        return self._task_links(project_id, task_ids)

    def _task_links(self, project_id, task_ids, *, owner_id=None):
        ids = list(dict.fromkeys(task_ids))
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        owner_filter = " AND s.owner_id=?" if owner_id is not None else ""
        parameters = (project_id, *((owner_id,) if owner_id is not None else ()), *ids)
        with self._db() as db:
            rows = db.execute(
                "SELECT t.task_id,t.session_id,s.claude_session_id,"
                "EXISTS(SELECT 1 FROM events e WHERE e.turn_id=t.turn_id "
                "AND e.type='native_terminal_ready') AS native_terminal "
                "FROM turns t JOIN sessions s ON s.session_id=t.session_id "
                f"WHERE s.project_id=?{owner_filter} AND t.task_id IN ({placeholders}) "
                "ORDER BY t.updated_at DESC",
                parameters).fetchall()
        links = {}
        for row in rows:
            if row["task_id"] in links:
                continue
            native = bool(row["native_terminal"] and row["claude_session_id"] == row["session_id"])
            links[row["task_id"]] = {"session_id": row["session_id"],
                                     "kind": "native_import" if native else "background"}
        return links

    def is_native_terminal(self, session_id, owner_id):
        """Only a successfully started PTY, never a legacy print-mode Claude ID."""
        with self._db() as db:
            return db.execute(
                "SELECT 1 FROM events e JOIN sessions s ON s.session_id=e.session_id "
                "WHERE e.session_id=? AND s.owner_id=? AND e.type='native_terminal_ready' LIMIT 1",
                (session_id, owner_id)).fetchone() is not None

    def attach_native_terminal(self, turn_id, terminal_session_id):
        """Bind a real Claude PTY once; never relabel a legacy print-mode turn."""
        if not isinstance(terminal_session_id, str) or len(terminal_session_id) != 36:
            raise ValueError("Invalid native terminal session id")
        with self._db() as db:
            row = db.execute("SELECT t.session_id,t.task_id,t.state,s.claude_session_id "
                             "FROM turns t JOIN sessions s ON s.session_id=t.session_id "
                             "WHERE t.turn_id=?", (turn_id,)).fetchone()
            if row is None or row["state"] not in ("queued", "running"):
                raise ValueError("Active Agent turn required for native terminal binding")
            if row["claude_session_id"] not in (None, terminal_session_id):
                raise ValueError("Agent turn is already bound to another terminal")
            db.execute("UPDATE sessions SET claude_session_id=?,updated_at=? WHERE session_id=?",
                       (terminal_session_id, _now(), row["session_id"]))
        return terminal_session_id

    def enqueue(self, session_id, owner_id, prompt, task_id=None):
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT session_id FROM sessions WHERE session_id=? AND owner_id=?",
                             (session_id, owner_id)).fetchone()
            if row is None:
                raise KeyError("Agent session not found")
            if task_id is not None:
                previous = db.execute("SELECT turn_id,ordinal,state,prompt FROM turns WHERE task_id=? AND session_id=?",
                                      (task_id, session_id)).fetchone()
                if previous is not None:
                    if previous["prompt"] != prompt:
                        raise ValueError("Agent import task prompt changed")
                    return {"turn_id": previous["turn_id"], "session_id": session_id,
                            "ordinal": previous["ordinal"], "state": previous["state"]}
            active = db.execute("SELECT 1 FROM turns WHERE session_id=? AND state IN ('queued','running')",
                                (session_id,)).fetchone()
            if active:
                raise ValueError("Agent session already has an active turn")
            ordinal = db.execute("SELECT COALESCE(MAX(ordinal),0)+1 FROM turns WHERE session_id=?",
                                 (session_id,)).fetchone()[0]
            turn_id, at = str(uuid.uuid4()), _now()
            db.execute("INSERT INTO turns VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                       (turn_id, session_id, ordinal, prompt, "queued", task_id,
                        None, None, None, at, at))
            db.execute("UPDATE sessions SET updated_at=? WHERE session_id=?", (at, session_id))
            self._append(db, session_id, turn_id, "queued", {"ordinal": ordinal})
        return {"turn_id": turn_id, "session_id": session_id, "ordinal": ordinal, "state": "queued"}

    def attach_task(self, turn_id, task_id):
        with self._db() as db:
            db.execute("UPDATE turns SET task_id=? WHERE turn_id=?", (task_id, turn_id))

    def claim(self):
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT t.*,s.owner_id,s.project_id,s.claude_session_id FROM turns t "
                             "JOIN sessions s ON s.session_id=t.session_id WHERE t.state='queued' "
                             "ORDER BY t.created_at LIMIT 1").fetchone()
            if row is None:
                return None
            at = _now()
            db.execute("UPDATE turns SET state='running',updated_at=? WHERE turn_id=?", (at, row["turn_id"]))
            self._append(db, row["session_id"], row["turn_id"], "running", {})
            return dict(row)

    @staticmethod
    def _append(db, session_id, turn_id, kind, payload):
        seq = db.execute("SELECT COALESCE(MAX(seq),0)+1 FROM events WHERE session_id=?",
                         (session_id,)).fetchone()[0]
        at = _now()
        db.execute("INSERT INTO events VALUES(?,?,?,?,?,?)",
                   (session_id, seq, turn_id, kind,
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")), at))
        return {"id": seq, "turn_id": turn_id, "type": kind, "payload": payload, "at": at}

    def append(self, session_id, turn_id, kind, payload):
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            return self._append(db, session_id, turn_id, kind, payload)

    def events(self, session_id, owner_id, after=0, limit=200):
        with self._db() as db:
            owned = db.execute("SELECT 1 FROM sessions WHERE session_id=? AND owner_id=?",
                               (session_id, owner_id)).fetchone()
            if owned is None:
                raise KeyError("Agent session not found")
            rows = db.execute("SELECT * FROM events WHERE session_id=? AND seq>? "
                              "ORDER BY seq LIMIT ?", (session_id, after, limit)).fetchall()
            return [{"id": row["seq"], "turn_id": row["turn_id"], "type": row["type"],
                     "payload": json.loads(row["payload"]), "at": row["at"]} for row in rows]

    def finish(self, turn_id, state, *, claude_session_id=None, run_id=None, raw_ref=None, error=None):
        if state not in ("succeeded", "failed", "cancelled"):
            raise ValueError("Terminal Agent state required")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM turns WHERE turn_id=?", (turn_id,)).fetchone()
            if row is None:
                raise KeyError("Agent turn not found")
            if row["state"] in ("succeeded", "failed", "cancelled"):
                if run_id and row["run_id"] is None and row["state"] == state:
                    db.execute("UPDATE turns SET run_id=?,raw_ref=?,updated_at=? WHERE turn_id=?",
                               (run_id, json.dumps(raw_ref) if raw_ref else None, _now(), turn_id))
                return
            at = _now()
            db.execute("UPDATE turns SET state=?,run_id=?,raw_ref=?,error=?,updated_at=? WHERE turn_id=?",
                       (state, run_id, json.dumps(raw_ref) if raw_ref else None, error, at, turn_id))
            if claude_session_id:
                db.execute("UPDATE sessions SET claude_session_id=?,updated_at=? WHERE session_id=?",
                           (claude_session_id, at, row["session_id"]))
            else:
                db.execute("UPDATE sessions SET updated_at=? WHERE session_id=?", (at, row["session_id"]))
            self._append(db, row["session_id"], turn_id, state,
                         {"run_id": run_id, "error": error})

    def cancel(self, session_id, owner_id):
        self.get(session_id, owner_id)
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT turn_id,state FROM turns WHERE session_id=? AND "
                             "state IN ('queued','running') ORDER BY ordinal DESC LIMIT 1",
                             (session_id,)).fetchone()
            if row is None:
                raise ValueError("No active Agent turn")
            if row["state"] == "queued":
                db.execute("UPDATE turns SET state='cancelled',updated_at=? WHERE turn_id=?",
                           (_now(), row["turn_id"]))
                self._append(db, session_id, row["turn_id"], "cancelled", {})
            else:
                self._append(db, session_id, row["turn_id"], "cancel_requested", {})
            return row["turn_id"]

    def cancel_requested(self, session_id, turn_id):
        with self._db() as db:
            return db.execute("SELECT 1 FROM events WHERE session_id=? AND turn_id=? "
                              "AND type='cancel_requested' LIMIT 1", (session_id, turn_id)).fetchone() is not None

    def recover(self):
        """An interrupted worker never replays commands automatically."""
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("SELECT t.*,s.owner_id,s.project_id,s.claude_session_id,"
                              "EXISTS(SELECT 1 FROM events e WHERE e.turn_id=t.turn_id "
                              "AND e.type='native_terminal_requested') AS native_requested FROM turns t "
                              "JOIN sessions s ON s.session_id=t.session_id "
                              "WHERE t.state='running'").fetchall()
            for row in rows:
                if row["native_requested"]:
                    continue  # The native Claude PTY may survive this worker restart.
                db.execute("UPDATE turns SET state='failed',error=?,updated_at=? WHERE turn_id=?",
                           ("Worker interrupted; resume the conversation explicitly", _now(), row["turn_id"]))
                self._append(db, row["session_id"], row["turn_id"], "failed",
                             {"error": "Worker interrupted; resume explicitly"})
            return [dict(row) for row in rows]

    def cancel_stale_import_turns(self, tasks):
        """A restarted API must not run an old queued Agent for a failed task."""
        with self._db() as db:
            rows = db.execute("SELECT t.turn_id,t.session_id,t.task_id,s.project_id FROM turns t "
                              "JOIN sessions s ON s.session_id=t.session_id "
                              "WHERE t.state='queued' AND t.task_id IS NOT NULL").fetchall()
        for row in rows:
            try:
                task = tasks.get(row["project_id"], row["task_id"])
            except KeyError:
                continue
            if task.get("source", {}).get("type") != "auto_import" or task["state"] not in (
                    "failed", "cancelled"):
                continue
            with self._db() as db:
                db.execute("BEGIN IMMEDIATE")
                changed = db.execute("UPDATE turns SET state='cancelled',error=?,updated_at=? "
                                     "WHERE turn_id=? AND state='queued'",
                                     ("Import task is no longer active", _now(), row["turn_id"]))
                if changed.rowcount:
                    self._append(db, row["session_id"], row["turn_id"], "cancelled",
                                 {"error": "Import task is no longer active"})

    def add_attachment(self, session_id, owner_id, name, ref, *, attachment_id=None):
        self.get(session_id, owner_id)
        item = {"attachment_id": attachment_id or str(uuid.uuid4()), "session_id": session_id,
                "name": name, "content_ref": ref, "created_at": _now()}
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT session_id,name,content_ref,created_at FROM attachments WHERE attachment_id=?",
                                  (item["attachment_id"],)).fetchone()
            if previous is not None:
                if (previous["session_id"] != session_id or previous["name"] != name or
                        json.loads(previous["content_ref"]) != ref):
                    raise ValueError("Agent attachment identity already belongs to another source")
                item["created_at"] = previous["created_at"]
                return item
            db.execute("INSERT INTO attachments VALUES(?,?,?,?,?)",
                       (item["attachment_id"], session_id, name,
                        json.dumps(ref, separators=(",", ":")), item["created_at"]))
        return item

    def attachments(self, session_id, owner_id):
        self.get(session_id, owner_id)
        with self._db() as db:
            rows = db.execute("SELECT * FROM attachments WHERE session_id=? ORDER BY created_at",
                              (session_id,)).fetchall()
            return [{**dict(row), "content_ref": json.loads(row["content_ref"])} for row in rows]

    def source(self, turn_id, source_id):
        """A worker-registered source is bound to one turn, not a guessed digest."""
        with self._db() as db:
            rows = db.execute("SELECT payload FROM events WHERE turn_id=? AND type='recorder_source'",
                              (turn_id,)).fetchall()
        for row in rows:
            value = json.loads(row["payload"])
            if value.get("source_id") == source_id:
                return value["content_ref"]
        return None

    def record_source(self, turn_id, source_id, content_ref):
        turn = self.internal_turn(turn_id)
        existing = self.source(turn_id, source_id)
        if existing is not None:
            if existing != content_ref:
                raise ValueError("Recorder source ID already binds different bytes")
            return existing
        self.append(turn["session_id"], turn_id, "recorder_source",
                    {"source_id": source_id, "content_ref": content_ref})
        return content_ref

    def authorize_telemetry(self, turn_id, token):
        """Bind a short-lived, high-entropy OTLP credential to one running turn."""
        import hashlib
        turn = self.internal_turn(turn_id)
        if turn["state"] != "running":
            raise ValueError("Recorder telemetry requires a running turn")
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT 1 FROM events WHERE turn_id=? AND type='recorder_telemetry_auth'",
                                  (turn_id,)).fetchone()
            if existing:
                raise ValueError("Recorder telemetry already authorized")
            self._append(db, turn["session_id"], turn_id, "recorder_telemetry_auth", {"sha256": digest})

    def telemetry_authorized(self, turn_id, token):
        import hashlib
        with self._db() as db:
            row = db.execute("SELECT payload FROM events WHERE turn_id=? AND type='recorder_telemetry_auth' "
                             "ORDER BY seq DESC LIMIT 1", (turn_id,)).fetchone()
        if row is None:
            return False
        return hmac.compare_digest(json.loads(row["payload"])["sha256"],
                                   hashlib.sha256(token.encode()).hexdigest())

    def record_telemetry(self, turn_id, signal, content_ref):
        turn = self.internal_turn(turn_id)
        if turn["state"] != "running":
            raise ValueError("Recorder telemetry requires a running turn")
        return self.append(turn["session_id"], turn_id, "recorder_telemetry",
                           {"signal": signal, "content_ref": content_ref})

    def telemetry_records(self, turn_id):
        self.internal_turn(turn_id)
        with self._db() as db:
            rows = db.execute("SELECT at,payload FROM events WHERE turn_id=? "
                              "AND type='recorder_telemetry' ORDER BY seq", (turn_id,)).fetchall()
        return [{"at": row["at"], **json.loads(row["payload"])} for row in rows]
