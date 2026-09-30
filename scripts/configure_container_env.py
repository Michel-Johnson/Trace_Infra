"""Create private Compose settings; password hashes never enter source or CLI args."""
import argparse
import getpass
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def configure(directory, username, password, port=8766, version='local'):
    if not re.fullmatch(r'[A-Za-z0-9_.@-]{1,64}', username):
        raise ValueError('Invalid username')
    if not password or len(password.encode()) > 72 or any(c in password for c in '\r\n'):
        raise ValueError('Password must contain 1–72 UTF-8 bytes without line breaks')
    if not 1 <= port <= 65535 or not re.fullmatch(r'[A-Za-z0-9_.-]+', version):
        raise ValueError('Invalid port or image version')
    directory = directory.resolve()
    if directory.exists():
        raise ValueError('Choose a new private directory; existing credentials are not overwritten')
    lock = json.loads((ROOT/'deploy/containers/images.lock.json').read_text())
    caddy = next(image['reference'] for image in lock['images'] if image['image'] == 'caddy')
    result = subprocess.run(['docker','run','--rm','-i',caddy,'caddy','hash-password','--algorithm','bcrypt'],
        input=(password+'\n').encode(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
    digest = result.stdout.decode().strip()
    if not re.fullmatch(r'\$2[aby]\$\d{2}\$[./A-Za-z0-9]{53}', digest):
        raise ValueError('Password hashing did not return the expected format')
    directory.mkdir(mode=0o700, parents=True)
    # Local Compose uses read-only bind mounts for file secrets. The parent is
    # private; each container user must be able to read the mounted file itself.
    for name, content in {'db_password':secrets.token_urlsafe(36)+'\n',
                          'access.caddy':f'basic_auth {{\n    {username} {digest}\n}}\n'}.items():
        path=directory/name;path.write_text(content);path.chmod(0o644)
    env=directory/'deployment.env'
    env.write_text(f'TRACE_HUNTER_VERSION={version}\nWEB_BIND=127.0.0.1\nWEB_PORT={port}\nPUBLIC_ORIGIN=http://127.0.0.1:{port}\n'
                   f'DB_PASSWORD_FILE={json.dumps(str(directory/"db_password"))}\nWEB_AUTH_FILE={json.dumps(str(directory/"access.caddy"))}\n')
    env.chmod(0o600)
    return env


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory',type=Path,required=True)
    parser.add_argument('--username',default='admin')
    parser.add_argument('--port',type=int,default=8766)
    parser.add_argument('--version',default='local')
    args=parser.parse_args()
    password=getpass.getpass('Web password: ') if sys.stdin.isatty() else sys.stdin.readline().rstrip('\r\n')
    try:env=configure(args.directory,args.username,password,args.port,args.version)
    except ValueError as error:raise SystemExit(str(error)) from None
    except Exception:raise SystemExit('Container settings could not be created; check Docker and the private directory.') from None
    print('Private Compose settings:',env)
    print('No containers have been started except the temporary password hasher.')

if __name__=='__main__':main()
