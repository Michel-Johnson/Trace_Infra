"""PostgreSQL prepare/cutover migrations preserve core facts."""

import hashlib
import json
import os
from pathlib import Path
import unittest
import uuid

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "Dedicated PostgreSQL test database required")
class CoreSchemaMigrationTests(unittest.TestCase):
    def setUp(self):
        base = make_url(os.environ["TEST_DATABASE_URL"]).set(drivername="postgresql+psycopg")
        self.admin = create_engine(base, hide_parameters=True)
        self.schema = "test_core_migration_" + uuid.uuid4().hex
        with self.admin.begin() as db: db.execute(text("CREATE SCHEMA " + self.schema))
        location = base.update_query_dict({"options": "-csearch_path=" + self.schema + ",public"})
        self.engine = create_engine(location, hide_parameters=True)
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.engine.dispose()
        with self.admin.begin() as db: db.execute(text("DROP SCHEMA " + self.schema + " CASCADE"))
        self.admin.dispose()

    def test_legacy_rows_are_copied_before_schema_consolidation(self):
        migrations = ROOT / "db/migrations"
        with self.engine.begin() as db:
            for path in sorted(migrations.glob("*.sql")):
                if path.name >= "021_core_schema_consolidation.sql": break
                db.exec_driver_sql(path.read_text())
            payload = json.dumps({"schema_version": "trace-hunter/1.1", "run": {"id": "run-1"}})
            digest = hashlib.sha256(payload.encode()).hexdigest()
            metadata = json.dumps({"run_id": "run-1", "query_id": "q", "env_id": "e",
                                   "harness": "h", "model": "m", "status": "completed",
                                   "title": "title", "format_version": "trace-hunter/1.1"})
            db.execute(text("""INSERT INTO runs(id,digest,imported_at,payload,query_id,env_id,metadata)
                VALUES('run-1',:digest,'2026-01-01T00:00:00Z',:payload,'q','e','{}')"""),
                       {"digest": digest, "payload": payload})
            db.execute(text("INSERT INTO trace_heads VALUES('default','run-1',1)"))
            db.execute(text("""INSERT INTO trace_revisions VALUES(
                'default','run-1',1,:content,:size,'application/json','trace-hunter/1.1',NULL,
                'legacy_import',:metadata,'2026-01-01T00:00:00Z')"""),
                       {"content": "sha256:" + digest, "size": len(payload), "metadata": metadata})
            db.execute(text("INSERT INTO trace_revision_requests VALUES('default','legacy:run-1','fingerprint','run-1',1)"))
            db.execute(text("INSERT INTO trace_revision_lookup VALUES('default','run-1',1,'71','65','68','6d','636f6d706c65746564')"))
            db.execute(text("""INSERT INTO trace_indexes(project_id,run_id,revision,projector_version,
                content_digest,format_version,state,record_count,model_count,model_batch_count,tool_count,
                agent_count,wait_count,other_count,unknown_count,identity_basis,projection_digest,indexed_at)
                VALUES('default','run-1',1,'trace-index/3',:content,'trace-hunter/1.1','complete',1,0,0,1,0,0,0,0,
                'source','projection','2026-01-01T00:00:01Z')"""), {"content": "sha256:" + digest})
            db.execute(text("""INSERT INTO trace_index_records(project_id,run_id,revision,projector_version,
                source_ordinal,span_id,kind,name,status,source_refs)
                VALUES('default','run-1',1,'trace-index/3',0,'span-1','tool','Read','ok','[]')"""))
            db.execute(text("""INSERT INTO trace_search_documents VALUES(
                'default','run-1',1,'trace-index/3',0,'span','span-1','span-1','name','Read','exact','[]')"""))
            db.execute(text("INSERT INTO analyses VALUES(:digest,'v1','[]','{}')"), {"digest": digest})
            db.exec_driver_sql((migrations / "021_core_schema_consolidation.sql").read_text())
            shadow_names = {row[0] for row in db.execute(text("""SELECT table_name FROM information_schema.tables
                WHERE table_schema=:schema AND table_type='BASE TABLE'"""), {"schema": self.schema})}
            self.assertIn("trace_revisions", shadow_names)
            self.assertIn("core_trace_revisions", shadow_names)
            self.assertEqual(db.execute(text("SELECT count(*) FROM core_trace_revisions")).scalar_one(), 1)
            self.assertEqual(db.execute(text("SELECT content_digest FROM core_trace_revisions")).scalar_one(),
                             "sha256:" + digest)
            self.assertEqual(db.execute(text("SELECT count(*) FROM core_trace_objects")).scalar_one(), 1)
            self.assertEqual(db.execute(text("SELECT count(*) FROM core_trace_search_documents")).scalar_one(), 1)

            db.exec_driver_sql((migrations / "022_core_schema_cutover.sql").read_text())
            db.exec_driver_sql((migrations / "023_trace_analysis_fields.sql").read_text())
            names = {row[0] for row in db.execute(text("""SELECT table_name FROM information_schema.tables
                WHERE table_schema=:schema AND table_type='BASE TABLE'"""), {"schema": self.schema})}
            self.assertEqual(names, {"projects", "traces", "trace_revisions", "trace_objects",
                "trace_edges", "trace_search_documents", "analysis_results"})
            self.assertEqual(db.execute(text("SELECT count(*) FROM trace_objects")).scalar_one(), 1)
            self.assertEqual(db.execute(text("SELECT count(*) FROM trace_search_documents")).scalar_one(), 1)
            self.assertEqual(db.execute(text("SELECT count(*) FROM analysis_results")).scalar_one(), 1)
            columns = {row[0] for row in db.execute(text("""SELECT column_name
                FROM information_schema.columns WHERE table_schema=:schema
                  AND table_name='trace_objects'"""), {"schema": self.schema})}
            self.assertIn("input_tokens", columns)


if __name__ == "__main__": unittest.main()
