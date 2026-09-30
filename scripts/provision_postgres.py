"""Provision this user's dedicated cluster. Never prints database credentials."""
import os
import secrets
import subprocess
import time
from pathlib import Path


def main():
    home = Path.home()
    binaries = home / 'apps/trace-hunter-postgres-18.6/bin'
    data = home / 'apps/trace-hunter-data'
    data.mkdir(parents=True, exist_ok=True, mode=0o700)
    socket = data / 'socket'
    socket.mkdir(exist_ok=True, mode=0o700)
    pgdata = data / 'postgres'
    if not (pgdata / 'PG_VERSION').exists():
        subprocess.run([str(binaries/'initdb'), '-D', str(pgdata), '--auth-local=peer', '--auth-host=scram-sha-256', '--encoding=UTF8', '--locale=C.UTF-8'], check=True, stdout=subprocess.DEVNULL)
        with (pgdata/'postgresql.conf').open('a') as f:
            f.write("\nlisten_addresses = '127.0.0.1'\nport = 55432\nunix_socket_directories = '" + str(socket) + "'\npassword_encryption = 'scram-sha-256'\n")
    unit = Path(__file__).resolve().parents[1] / 'deploy/services/trace-hunter-db.service'
    service = home / '.config/systemd/user/trace-hunter-db.service'
    service.parent.mkdir(parents=True, exist_ok=True)
    service.write_text(unit.read_text())
    subprocess.run(['systemctl','--user','daemon-reload'], check=True)
    subprocess.run(['systemctl','--user','enable','--now','trace-hunter-db'], check=True)
    for attempt in range(20):
        result = subprocess.run([str(binaries/'pg_isready'), '-h', str(socket), '-p', '55432', '-t', '1'], stdout=subprocess.DEVNULL)
        if result.returncode == 0:
            break
        time.sleep(0.5)
    else:
        raise RuntimeError('PostgreSQL did not become ready; inspect trace-hunter-db service logs')
    env = data / 'database.env'
    if not env.exists():
        password = secrets.token_urlsafe(36)
        # The generated password contains only URL-safe ASCII, never SQL quotes.
        sql = "CREATE ROLE trace_hunter LOGIN PASSWORD '" + password + "';\nCREATE DATABASE trace_hunter OWNER trace_hunter;\nCREATE DATABASE trace_hunter_test OWNER trace_hunter;\n"
        subprocess.run([str(binaries/'psql'), '-h', str(socket), '-p','55432','-d','postgres','-v','ON_ERROR_STOP=1'], input=sql, text=True, check=True, stdout=subprocess.DEVNULL)
        fd = os.open(env, os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600)
        with os.fdopen(fd,'w') as f:
            f.write('DATABASE_URL=postgresql://trace_hunter:' + password + '@127.0.0.1:55432/trace_hunter\n')
    print('Dedicated PostgreSQL cluster ready; credentials are in the private database.env file.')

if __name__ == '__main__':
    main()
