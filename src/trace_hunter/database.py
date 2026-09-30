"""Persistence boundary. Canonical payload text remains the hash authority."""
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool

class Conflict(ValueError):
    pass


SQLITE_SCHEMA = '''
CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY, checksum TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS projects (
    project_id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS traces (
    project_id TEXT NOT NULL, run_id TEXT NOT NULL, latest_revision INTEGER NOT NULL,
    query_id_hex TEXT, env_id_hex TEXT, harness_hex TEXT, model_hex TEXT, status_hex TEXT,
    title_hex TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    PRIMARY KEY(project_id,run_id), FOREIGN KEY(project_id) REFERENCES projects(project_id));
CREATE TABLE IF NOT EXISTS trace_revisions (
    project_id TEXT NOT NULL, run_id TEXT NOT NULL, revision INTEGER NOT NULL,
    request_key TEXT NOT NULL, request_fingerprint TEXT NOT NULL,
    content_digest TEXT NOT NULL, size_bytes INTEGER NOT NULL, media_type TEXT NOT NULL,
    format_version TEXT NOT NULL, previous_revision INTEGER, derivation TEXT NOT NULL,
    metadata TEXT NOT NULL, query_id_hex TEXT, env_id_hex TEXT, harness_hex TEXT, model_hex TEXT,
    status_hex TEXT, projector_version TEXT, projection_state TEXT NOT NULL DEFAULT 'unindexed',
    projection_error TEXT, projection_digest TEXT, indexed_at TEXT,
    record_count INTEGER, model_count INTEGER, model_batch_count INTEGER, tool_count INTEGER,
    agent_count INTEGER, wait_count INTEGER, other_count INTEGER, unknown_count INTEGER,
    capture_coverage TEXT, identity_basis TEXT NOT NULL DEFAULT 'source', created_at TEXT NOT NULL,
    attributes TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY(project_id,run_id,revision), UNIQUE(project_id,request_key),
    FOREIGN KEY(project_id,run_id) REFERENCES traces(project_id,run_id));
CREATE TABLE IF NOT EXISTS trace_objects (
    project_id TEXT NOT NULL, run_id TEXT NOT NULL, revision INTEGER NOT NULL,
    projector_version TEXT NOT NULL, object_ordinal INTEGER NOT NULL, object_kind TEXT NOT NULL,
    object_id TEXT NOT NULL, source_ordinal INTEGER, span_id TEXT, parent_id TEXT, kind TEXT,
    name TEXT, operation TEXT, status TEXT, agent_id TEXT, segment_id TEXT, turn_id TEXT,
    proposal_id TEXT, call_id TEXT, invocation_id TEXT, attempt INTEGER, context_id TEXT,
    visibility_status TEXT, visibility_issues TEXT, skill_name TEXT, skill_action TEXT,
    source_refs TEXT NOT NULL, stream_id TEXT, source_sequence INTEGER, clock_id TEXT,
    clock_kind TEXT, clock_origin_at TEXT, clock_uncertainty_ms REAL, start_ms REAL, end_ms REAL,
    duration_ms REAL, duration_basis TEXT, duration_scope TEXT, first_response_ms REAL,
    last_response_ms REAL, response_boundary TEXT, model_provider TEXT, requested_model TEXT,
    response_model TEXT, usage_completeness TEXT, input_tokens INTEGER, output_tokens INTEGER,
    cache_read_tokens INTEGER, cache_write_tokens INTEGER, reasoning_tokens INTEGER,
    attributes TEXT NOT NULL DEFAULT '{}', start_at TEXT, end_at TEXT,
    session_namespace TEXT, session_id TEXT,
    payload TEXT NOT NULL,
    PRIMARY KEY(project_id,run_id,revision,projector_version,object_ordinal),
    UNIQUE(project_id,run_id,revision,projector_version,object_kind,object_id));
CREATE TABLE IF NOT EXISTS trace_edges (
    project_id TEXT NOT NULL, run_id TEXT NOT NULL, revision INTEGER NOT NULL,
    projector_version TEXT NOT NULL, edge_ordinal INTEGER NOT NULL, source_kind TEXT NOT NULL,
    source_id TEXT NOT NULL, relation TEXT NOT NULL, target_kind TEXT NOT NULL,
    target_id TEXT NOT NULL, position INTEGER, payload TEXT NOT NULL,
    PRIMARY KEY(project_id,run_id,revision,projector_version,edge_ordinal));
CREATE TABLE IF NOT EXISTS trace_search_documents (
    project_id TEXT NOT NULL, run_id TEXT NOT NULL, revision INTEGER NOT NULL,
    projector_version TEXT NOT NULL, document_ordinal INTEGER NOT NULL, object_kind TEXT NOT NULL,
    object_id TEXT NOT NULL, span_id TEXT, field TEXT NOT NULL, text TEXT NOT NULL,
    text_state TEXT NOT NULL, source_refs TEXT NOT NULL,
    PRIMARY KEY(project_id,run_id,revision,projector_version,document_ordinal));
CREATE TABLE IF NOT EXISTS trace_search_hot_terms (
    project_id TEXT NOT NULL, term TEXT NOT NULL,
    search_count INTEGER NOT NULL DEFAULT 0, total_latency_ms REAL NOT NULL DEFAULT 0,
    last_total_count INTEGER, active INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, last_searched_at TEXT, indexed_at TEXT,
    PRIMARY KEY(project_id,term), FOREIGN KEY(project_id) REFERENCES projects(project_id));
CREATE TABLE IF NOT EXISTS trace_search_hot_postings (
    project_id TEXT NOT NULL, term TEXT NOT NULL, run_id TEXT NOT NULL,
    revision INTEGER NOT NULL, projector_version TEXT NOT NULL, document_ordinal INTEGER NOT NULL,
    object_kind TEXT NOT NULL, field TEXT NOT NULL, span_id TEXT,
    PRIMARY KEY(project_id,term,run_id,revision,projector_version,document_ordinal),
    FOREIGN KEY(project_id,term) REFERENCES trace_search_hot_terms(project_id,term));
CREATE TABLE IF NOT EXISTS analysis_results (
    project_id TEXT NOT NULL, run_id TEXT NOT NULL, revision INTEGER NOT NULL,
    analyzer TEXT NOT NULL, analyzer_version TEXT NOT NULL, scope_digest TEXT NOT NULL,
    input_digest TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL,
    PRIMARY KEY(project_id,run_id,revision,analyzer,analyzer_version,scope_digest));
CREATE INDEX IF NOT EXISTS traces_query ON traces(project_id,substr(query_id_hex,1,128));
CREATE INDEX IF NOT EXISTS trace_revisions_recent ON trace_revisions(project_id,created_at DESC,run_id,revision);
CREATE INDEX IF NOT EXISTS trace_objects_kind ON trace_objects(project_id,projector_version,kind,status);
CREATE INDEX IF NOT EXISTS trace_objects_skill ON trace_objects(project_id,projector_version,skill_name,skill_action,status);
CREATE INDEX IF NOT EXISTS trace_objects_duration ON trace_objects(project_id,projector_version,duration_ms);
CREATE INDEX IF NOT EXISTS trace_objects_visibility ON trace_objects(project_id,projector_version,visibility_status,context_id);
CREATE INDEX IF NOT EXISTS trace_objects_source_window ON trace_objects(project_id,run_id,revision,projector_version,source_ordinal) WHERE object_kind='span';
CREATE INDEX IF NOT EXISTS trace_objects_absolute_time ON trace_objects(project_id,projector_version,start_at,run_id,revision,object_ordinal) WHERE start_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS trace_objects_session ON trace_objects(project_id,projector_version,session_namespace,session_id,start_at,run_id,revision) WHERE session_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS trace_edges_lookup ON trace_edges(project_id,run_id,revision,projector_version,relation,source_id);
CREATE INDEX IF NOT EXISTS trace_edges_reverse_lookup ON trace_edges(project_id,run_id,revision,projector_version,relation,target_id);
CREATE INDEX IF NOT EXISTS trace_search_documents_identity ON trace_search_documents(project_id,projector_version,run_id,revision,object_kind,field);
CREATE INDEX IF NOT EXISTS trace_search_documents_text ON trace_search_documents(project_id,projector_version,text);
CREATE INDEX IF NOT EXISTS trace_search_hot_postings_page ON trace_search_hot_postings(project_id,term,projector_version,run_id,revision,document_ordinal);
CREATE INDEX IF NOT EXISTS trace_search_hot_postings_document ON trace_search_hot_postings(project_id,run_id,revision,projector_version);
INSERT OR IGNORE INTO projects(project_id,name,created_at) VALUES('default','Default',CURRENT_TIMESTAMP);
'''


class Repository:
    def __init__(self, location):
        self.postgres = str(location).startswith(('postgresql://', 'postgresql+psycopg://'))
        self.path = None if self.postgres else Path(location)
        if self.postgres:
            url = str(location).replace('postgresql://', 'postgresql+psycopg://', 1)
            self.engine = create_engine(url, pool_pre_ping=True, pool_size=5, max_overflow=5, hide_parameters=True, connect_args={'connect_timeout': 10})
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.connect() as db:
                db.executescript(SQLITE_SCHEMA)
            self.engine = create_engine('sqlite:///' + str(self.path.resolve()), poolclass=NullPool, connect_args={'timeout': 15}, hide_parameters=True)
        self.order_column = 'created_at'

    def connect(self):
        """Compatibility entry for SQLite scripts; platform uses transactions below."""
        if self.postgres:
            raise RuntimeError('Use the PostgreSQL repository transaction interface')
        return sqlite3.connect(str(self.path), timeout=15)

    def close(self):
        self.engine.dispose()

    def rows(self, sql, params=None):
        with self.engine.connect() as db:
            return list(db.execute(text(sql), params or {}).mappings())

    def migrate(self):
        if not self.postgres:
            return
        directory = Path(__file__).resolve().parents[2] / 'db/migrations'
        with self.engine.begin() as db:
            db.execute(text('SELECT pg_advisory_xact_lock(817603201)'))
            current_schema = db.execute(text('SELECT current_schema()')).scalar_one()
            extension_schema = db.execute(text("""SELECT n.nspname FROM pg_extension e
                JOIN pg_namespace n ON n.oid=e.extnamespace WHERE e.extname='pg_trgm'""")).scalar_one_or_none()
            if extension_schema and extension_schema != current_schema:
                quote = db.dialect.identifier_preparer.quote
                db.exec_driver_sql('SET LOCAL search_path = ' + quote(current_schema) + ',' + quote(extension_schema))
            db.execute(text('CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY, checksum TEXT NOT NULL)'))
            known = dict(db.execute(text('SELECT version,checksum FROM schema_migrations')).all())
            for path in sorted(directory.glob('*.sql')):
                raw = path.read_text()
                checksum = hashlib.sha256(raw.encode()).hexdigest()
                if path.name in known:
                    if known[path.name] != checksum:
                        raise Conflict('Applied database migration checksum changed: ' + path.name)
                    continue
                # These are repository-owned, transaction-safe migration files.
                db.exec_driver_sql(raw)
                db.execute(text('INSERT INTO schema_migrations VALUES(:version,:checksum)'), {'version': path.name, 'checksum': checksum})
            # 022 removes the legacy lookup table; all query metadata now lives
            # on the trace identity and revision rows.

    def cached(self, digest, version, scope):
        rows = self.rows('''SELECT payload FROM analysis_results
            WHERE input_digest=:digest AND analyzer='legacy-report'
              AND analyzer_version=:version AND scope_digest=:scope''',
            {'digest': digest, 'version': version, 'scope': scope})
        return json.loads(rows[0]['payload']) if rows else None

    def save_analysis(self, digest, version, scope, result):
        with self.engine.begin() as db:
            revision = db.execute(text('''SELECT project_id,run_id,revision FROM trace_revisions
                WHERE content_digest=:content ORDER BY created_at LIMIT 1'''),
                {'content': 'sha256:' + digest}).mappings().first()
            if revision is None:
                raise KeyError('Trace revision not found for analysis input')
            db.execute(text('''INSERT INTO analysis_results(project_id,run_id,revision,analyzer,
                analyzer_version,scope_digest,input_digest,payload,created_at)
                VALUES(:project_id,:run_id,:revision,'legacy-report',:version,:scope,:digest,:payload,:created_at)
                ON CONFLICT(project_id,run_id,revision,analyzer,analyzer_version,scope_digest) DO NOTHING'''),
                {**revision, 'digest': digest, 'version': version, 'scope': scope,
                 'payload': json.dumps(result, ensure_ascii=False, allow_nan=False),
                 'created_at': datetime.now(timezone.utc).isoformat()})
        return self.cached(digest, version, scope)
