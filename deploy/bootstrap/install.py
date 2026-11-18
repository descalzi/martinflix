#!/usr/bin/env python3
"""Start or resume the first-run MartinFlix Compose project."""
import argparse
import fcntl
import hashlib
import ipaddress
import os
import secrets
import subprocess
import tempfile
import time
from pathlib import Path
from xml.etree import ElementTree

COMPOSE_FILE = Path(__file__).resolve().with_name('compose.yaml')
APPS = ('sonarr', 'radarr')


def private_directory(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
        raise ValueError(f'{path} must be a private directory owned by the current account (mode 0700).')


def write_secret(path, value):
    if path.is_symlink():
        raise ValueError(f'{path} must be a regular file.')
    if path.exists():
        if not path.is_file() or path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
            raise ValueError(f'{path} must be private and owned by the current account.')
        if path.read_text().strip() == value:
            return
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as output:
        temporary = Path(output.name)
        output.write(value + '\n')
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def wait_for_key(path):
    for _ in range(30):
        try:
            key = ElementTree.parse(path).getroot().findtext('ApiKey', '').strip()
            if key:
                return key
        except (FileNotFoundError, ElementTree.ParseError):
            pass
        time.sleep(1)
    raise ValueError(f'{path.parent.name} did not write its API key in time. Check its Compose logs and rerun.')


def install(args):
    if os.getuid() == 0:
        raise ValueError('Run as the Linux account that will own the stack data and can already use Docker.')
    try:
        subprocess.run(['docker', 'compose', 'version'], check=True, stdout=subprocess.DEVNULL)
        subprocess.run(['docker', 'info', '--format', '{{.ServerVersion}}'], check=True, stdout=subprocess.DEVNULL)
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        raise ValueError('Install Docker Engine and the Compose plugin, then verify Docker works for this account.') from exc

    state = args.state_dir.expanduser().resolve()
    if state.is_relative_to(COMPOSE_FILE.parents[2]):
        raise ValueError('Choose a state directory outside the repository for application data and secrets.')
    ports = (args.dashboard_port, args.sonarr_port, args.radarr_port)
    if len(set(ports)) != len(ports) or any(not 1024 <= port <= 65535 for port in ports):
        raise ValueError('Choose three distinct ports between 1024 and 65535.')
    bind = ipaddress.IPv4Address(args.dashboard_bind)
    if bind.is_unspecified or bind.is_multicast:
        raise ValueError('Choose loopback or a specific host IPv4 address for the dashboard.')

    private_directory(state)
    suffix = hashlib.sha256(os.fsencode(state)).hexdigest()[:16]
    project = f'martinflix-first-run-{suffix}'
    compose = ['docker', 'compose', '--env-file', '/dev/null', '-f', str(COMPOSE_FILE), '-p', project]
    environment = dict(os.environ, MARTINFLIX_STATE_DIR=str(state),
                       MARTINFLIX_UID=str(os.getuid()), MARTINFLIX_GID=str(os.getgid()),
                       MARTINFLIX_DASHBOARD_BIND=str(bind), MARTINFLIX_DASHBOARD_PORT=str(ports[0]),
                       MARTINFLIX_SONARR_PORT=str(ports[1]), MARTINFLIX_RADARR_PORT=str(ports[2]))

    with (state / 'install.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        secret_dir = state / 'dashboard-secrets'
        private_directory(secret_dir)
        for app in APPS:
            private_directory(state / app)
        password = secret_dir / 'admin-password'
        if password.exists() or password.is_symlink():
            if password.is_symlink() or not password.is_file() or password.stat().st_uid != os.getuid() or password.stat().st_mode & 0o077:
                raise ValueError('The existing dashboard password file must be private and owned by the current account.')
            if len(password.read_text().rstrip('\r\n')) < 16:
                raise ValueError('The existing dashboard password must be at least 16 characters.')
        else:
            write_secret(password, secrets.token_urlsafe(32))

        print(f'Setting up Compose project {project}', flush=True)
        subprocess.run(compose + ['up', '-d', '--wait', '--wait-timeout', '120', *APPS],
                       env=environment, check=True)
        for app in APPS:
            write_secret(secret_dir / f'{app}-key', wait_for_key(state / app / 'config.xml'))
        subprocess.run(compose + ['up', '-d', '--build', '--wait', '--wait-timeout', '90', 'dashboard'],
                       env=environment, check=True)
        verify = '''
import server
for app in ('sonarr', 'radarr'):
    result = server.arr_data(app)
    if not result['configured'] or result.get('error'):
        raise SystemExit(f'{app} verification failed: {result.get("error", "key unavailable")}')
    print(f'{app} monitoring connected', flush=True)
'''
        subprocess.run(compose + ['exec', '-T', 'dashboard', 'python', '-B', '-c', verify],
                       env=environment, check=True)
    print(f'Dashboard: http://{bind}:{ports[0]} (admin, password file: {password})')
    print(f'Sonarr: http://127.0.0.1:{ports[1]}  Radarr: http://127.0.0.1:{ports[2]}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', type=Path, default=Path.home() / '.local/state/martinflix-first-run')
    parser.add_argument('--dashboard-bind', default='127.0.0.1')
    parser.add_argument('--dashboard-port', type=int, default=8765)
    parser.add_argument('--sonarr-port', type=int, default=8989)
    parser.add_argument('--radarr-port', type=int, default=7878)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        install(args)
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        if isinstance(exc, subprocess.CalledProcessError):
            parser.exit(1, 'Compose setup or verification failed. Check the output above and rerun with the same options.\n')
        parser.exit(1, f'{exc}\n')


if __name__ == '__main__':
    main()
