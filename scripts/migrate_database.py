"""Version schema or import SQLite; DATABASE_URL comes from the environment."""
import argparse
import json
import os
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from trace_hunter.database import Repository
from trace_hunter.migration import migrate_sqlite


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--sqlite', type=Path, help='Read-only source snapshot; target must be empty or identical')
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    if not os.environ.get('DATABASE_URL', '').startswith(('postgresql://', 'postgresql+psycopg://')):
        parser.error('Set DATABASE_URL to the PostgreSQL target')
    repo = Repository(os.environ['DATABASE_URL'])
    try:
        repo.migrate()
        result = migrate_sqlite(args.sqlite, repo) if args.sqlite else {'status': 'schema_ready'}
        output = json.dumps(result, ensure_ascii=False, indent=2) + '\n'
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(output)
        print(output, end='')
    finally:
        repo.close()

if __name__ == '__main__':
    main()
