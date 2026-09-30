"""Copy a consistent SQLite snapshot into an empty PostgreSQL database atomically."""
import hashlib
import json
import sqlite3
from pathlib import Path
from sqlalchemy import text
from .database import Repository, Conflict

TABLES = {
    'runs': ('id', 'digest', 'imported_at', 'payload'),
    'collections': ('id', 'digest', 'payload'),
    'case_definitions': ('query_id', 'digest', 'payload'),
    'collection_cases': ('collection_id', 'query_id', 'position'),
    'analyses': ('digest', 'version', 'scope', 'payload'),
}


def fingerprint(rows):
    canonical = sorted(json.dumps(dict(row), ensure_ascii=False, sort_keys=True, separators=(',', ':')) for row in rows)
    return hashlib.sha256(('\n'.join(canonical)).encode()).hexdigest()


def migrate_sqlite(source, target):
    if not target.postgres:
        raise ValueError('Migration target must be PostgreSQL')
    with sqlite3.connect(Path(source).resolve().as_uri() + '?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        db.execute('BEGIN')
        original = {table: [dict(row) for row in db.execute('SELECT ' + ','.join(columns) + ' FROM ' + table + ' ORDER BY rowid')] for table, columns in TABLES.items()}
    for table in ('runs', 'collections', 'case_definitions'):
        for row in original[table]:
            if hashlib.sha256(row['payload'].encode()).hexdigest() != row['digest']:
                raise Conflict('Source payload/digest mismatch in ' + table)
    with target.engine.begin() as db:
        db.execute(text('SELECT pg_advisory_xact_lock(817603202)'))
        before = {table: [dict(row) for row in db.execute(text('SELECT ' + ','.join(columns) + ' FROM ' + table)).mappings()] for table, columns in TABLES.items()}
        if any(before.values()):
            if all(fingerprint(before[t]) == fingerprint(original[t]) for t in TABLES):
                return {'status': 'already_migrated', 'tables': {t: {'count': len(original[t]), 'sha256': fingerprint(original[t])} for t in TABLES}}
            raise Conflict('Target contains different data; migration never overwrites it')
        for row in original['runs']:
            target.insert_run(db, row)
        for table in ('collections', 'case_definitions', 'collection_cases', 'analyses'):
            columns = TABLES[table]
            statement = text('INSERT INTO ' + table + '(' + ','.join(columns) + ') VALUES(' + ','.join(':' + col for col in columns) + ')')
            for row in original[table]:
                db.execute(statement, row)
        report = {}
        for table, columns in TABLES.items():
            copied = list(db.execute(text('SELECT ' + ','.join(columns) + ' FROM ' + table)).mappings())
            if fingerprint(copied) != fingerprint(original[table]):
                raise Conflict('Migration verification failed: ' + table)
            report[table] = {'count': len(copied), 'sha256': fingerprint(copied)}
    return {'status': 'migrated', 'tables': report}
