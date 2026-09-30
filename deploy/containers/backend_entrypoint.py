"""Run the existing API, worker or schema migration inside the backend image."""
import os
from pathlib import Path
import runpy
import sys

from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError

ROOT = Path(__file__).resolve().parents[2]
COMMANDS = {
    'api': ROOT / 'apps/api/run.py',
    'worker': ROOT / 'scripts/evaluation_worker.py',
    'invocation-worker': ROOT / 'scripts/invocation_worker.py',
    'migrate': ROOT / 'scripts/migrate_database.py',
    'rebuild-indexes': ROOT / 'scripts/rebuild_span_indexes.py',
}


class ConfigurationError(ValueError):
    """Only static, credential-free configuration diagnostics belong here."""


def port(value, name):
    try:
        number = int(value)
        if not 1 <= number <= 65535:
            raise ValueError()
    except (ValueError, TypeError):
        raise ConfigurationError(name + ' must be an integer from 1 to 65535') from None
    return number


def database_url(environ):
    """Resolve PostgreSQL credentials without printing or shell-interpolating them."""
    configured = environ.get('DATABASE_URL')
    if configured:
        try:
            parsed = make_url(configured)
            if parsed.drivername not in ('postgresql', 'postgresql+psycopg'):
                raise ValueError()
            return parsed.set(drivername='postgresql+psycopg').render_as_string(hide_password=False)
        except (ValueError, TypeError, ArgumentError):
            # URL parser exceptions can include the supplied connection string.
            raise ConfigurationError('DATABASE_URL must be a valid PostgreSQL URL') from None

    required = ('DB_HOST', 'DB_NAME', 'DB_USER', 'DB_PASSWORD_FILE')
    if any(not environ.get(name) for name in required):
        raise ConfigurationError('Set DATABASE_URL or all of DB_HOST, DB_NAME, DB_USER and DB_PASSWORD_FILE')
    try:
        password = Path(environ['DB_PASSWORD_FILE']).read_text(encoding='utf-8').rstrip('\r\n')
    except (OSError, UnicodeError):
        raise ConfigurationError('DB_PASSWORD_FILE must be a readable UTF-8 secret file') from None
    if not password:
        raise ConfigurationError('DB_PASSWORD_FILE must not be empty')
    return URL.create('postgresql+psycopg', username=environ['DB_USER'], password=password,
                      host=environ['DB_HOST'], port=port(environ.get('DB_PORT', '5432'), 'DB_PORT'),
                      database=environ['DB_NAME']).render_as_string(hide_password=False)


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    try:
        if len(args) != 1 or args[0] not in COMMANDS:
            raise ConfigurationError('Choose one backend command: api, worker, invocation-worker, migrate or rebuild-indexes')
        command = args[0]
        resolved = database_url(os.environ)
        if command == 'api':
            os.environ.setdefault('API_HOST', '0.0.0.0')
            os.environ['API_PORT'] = str(port(os.environ.get('API_PORT', '8767'), 'API_PORT'))
        os.environ['DATABASE_URL'] = resolved
        os.chdir(ROOT)
        script = COMMANDS[command]
        # Reuse the existing entrypoints in this process so they receive signals.
        sys.argv = [str(script)]
        if command == 'invocation-worker':
            project = os.environ.get('TRACE_HUNTER_WORKER_PROJECT')
            if not project:
                raise ConfigurationError('TRACE_HUNTER_WORKER_PROJECT is required')
            sys.argv += ['run', '--project', project]
        runpy.run_path(str(script), run_name='__main__')
    except ConfigurationError as error:
        print('Backend configuration error: ' + str(error), file=sys.stderr)
        return 2
    except Exception as error:
        # Driver errors may include connection strings; never echo the exception.
        print('Backend command failed (' + type(error).__name__ + ').', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
