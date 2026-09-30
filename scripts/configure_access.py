#!/usr/bin/env python3
"""Write private Caddy Basic Auth settings; never restart services or print secrets."""
import argparse
from datetime import datetime, timezone
import getpass
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid


def atomic(path, raw):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name('.' + path.name + '.pending-' + uuid.uuid4().hex)
    temporary.write_bytes(raw)
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def configure(username, password, data, caddy):
    if not re.fullmatch(r'[A-Za-z0-9_.@-]{1,64}', username):
        raise ValueError('Username must contain 1–64 letters, digits, _, ., @ or -')
    if not password or any(c in password for c in '\r\n') or len(password.encode()) > 72:
        raise ValueError('Password must contain 1–72 UTF-8 bytes without line breaks')
    web_path = data / 'web.env'
    previous_web = web_path.read_bytes()
    auth_path = (data / 'access.caddy').resolve()
    previous_auth = auth_path.read_bytes() if auth_path.exists() else None
    # No plaintext CLI argument, environment variable or logging.
    result = subprocess.run([str(caddy), 'hash-password', '--algorithm', 'bcrypt'],
        input=(password+'\n').encode(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
    digest = result.stdout.decode().strip()
    if not re.fullmatch(r'\$2[aby]\$\d{2}\$[./A-Za-z0-9]{53}', digest):
        raise RuntimeError('Caddy did not return a bcrypt hash')
    backup = data / 'backups' / ('access-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8])
    backup.mkdir(mode=0o700, parents=True)
    atomic(backup / 'web.env', previous_web)
    if previous_auth is not None:
        atomic(backup / 'access.caddy', previous_auth)
    lines = [line for line in previous_web.decode().splitlines() if not line.startswith('TRACE_HUNTER_AUTH_CONFIG=')]
    lines.append('TRACE_HUNTER_AUTH_CONFIG=' + str(auth_path))
    try:
        atomic(auth_path, ('basic_auth {\n    ' + username + ' ' + digest + '\n}\n').encode())
        atomic(web_path, ('\n'.join(lines) + '\n').encode())
    except Exception:
        if previous_auth is None:
            auth_path.unlink(missing_ok=True)
        else:
            atomic(auth_path, previous_auth)
        atomic(web_path, previous_web)
        raise
    return auth_path, backup


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('username')
    parser.add_argument('--data-dir', type=Path, default=Path.home() / 'apps/trace-hunter-data')
    parser.add_argument('--caddy', type=Path, default=Path.home() / 'apps/trace-hunter-tools/caddy')
    args = parser.parse_args()
    password = getpass.getpass('Password: ') if sys.stdin.isatty() else sys.stdin.readline().rstrip('\r\n')
    try:
        auth, backup = configure(args.username, password, args.data_dir, args.caddy)
    except Exception:
        raise SystemExit('Access configuration failed; private settings were not activated.') from None
    print('Private access configuration written:', auth)
    print('Previous settings backed up:', backup)
    print('Services have not been restarted. Activate the release Caddyfile to apply.')


if __name__ == '__main__':
    main()
