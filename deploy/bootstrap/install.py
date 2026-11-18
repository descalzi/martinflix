#!/usr/bin/env python3
"""Start or resume the first-run MartinFlix Compose project."""
import argparse
import errno
import hashlib
import ipaddress
import json
import os
import secrets
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from xml.etree import ElementTree

COMPOSE_FILE = Path(__file__).resolve().with_name('compose.yaml')
APPS = ('sonarr', 'radarr')
DOCKER_INSTALL = 'https://docs.docker.com/engine/install/'
COMPOSE_INSTALL = 'https://docs.docker.com/compose/install/linux/'


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


def docker_check(command):
    try:
        return subprocess.run(['docker', *command], capture_output=True, text=True, timeout=20)
    except subprocess.TimeoutExpired as exc:
        raise ValueError('Docker did not respond. Check that Docker is running, then try again.') from exc


def preflight_docker():
    if not shutil.which('docker'):
        raise ValueError(f'Docker is missing. Install Docker Engine and Compose for your Linux distribution: {DOCKER_INSTALL}')
    compose = docker_check(['compose', 'version'])
    if compose.returncode:
        raise ValueError(f'The Docker Compose plugin is missing or unavailable. Install it: {COMPOSE_INSTALL}\nThen run: docker compose version')
    help_result = docker_check(['compose', 'up', '--help'])
    if help_result.returncode or '--wait-timeout' not in help_result.stdout:
        raise ValueError(f'Your Docker Compose plugin needs an update. Follow: {COMPOSE_INSTALL}\nThen run: docker compose up --help')
    context = docker_check(['context', 'inspect', '--format', '{{.Endpoints.docker.Host}}'])
    host = context.stdout.strip()
    if os.environ.get('DOCKER_HOST') and not os.environ.get('DOCKER_CONTEXT'):
        host = os.environ['DOCKER_HOST']
    if context.returncode or not host.startswith('unix://'):
        raise ValueError('This installer needs Docker on this VM. Run: docker context use default\n'
                         'If DOCKER_HOST or DOCKER_CONTEXT points to a remote engine, unset it and try again.')
    info = docker_check(['info', '--format', '{{.DockerRootDir}}'])
    if not info.returncode:
        return Path(info.stdout.strip())
    if 'permission denied' in info.stderr.lower():
        raise ValueError('Your user cannot access Docker. Set up access for your account, then sign out and back in:\n'
                         'https://docs.docker.com/engine/install/linux-postinstall/\n'
                         'Run docker info to confirm access before rerunning the installer.')
    raise ValueError('Docker Engine is unavailable. On a systemd VM, try: sudo systemctl start docker\n'
                     'If you installed rootless Docker, try: systemctl --user start docker\n'
                     'Then run: docker info')


def check_directory(path):
    if path.is_symlink():
        raise ValueError(f'{path} is a symbolic link. Choose a private, writable --state-dir.')
    if path.exists():
        if not path.is_dir() or path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
            raise ValueError(f'{path} must be a private directory owned by your account (mode 0700). '
                             'Choose a new directory with --state-dir, or correct the permissions on your existing directory.')
    parent = path
    while not parent.exists():
        parent = parent.parent
    if not parent.is_dir() or not os.access(parent, os.W_OK | os.X_OK):
        raise ValueError(f'Cannot write to {path}. Choose a writable directory with --state-dir.')


def check_ports(compose, environment, bindings):
    result = subprocess.run(compose + ['ps', '--format', 'json'], env=environment,
                            capture_output=True, text=True, timeout=20, check=True)
    output = result.stdout.strip()
    containers = json.loads(output) if output.startswith('[') else [json.loads(line) for line in output.splitlines()]
    published = {
        (container['Service'], entry['URL'], entry['PublishedPort'])
        for container in containers for entry in (container.get('Publishers') or [])
        if entry.get('Protocol') == 'tcp'
    }
    for app, address, port in bindings:
        if (app, address, port) in published:
            continue
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                listener.bind((address, port))
        except OSError as exc:
            if exc.errno == errno.EADDRINUSE:
                raise ValueError(f'{app}: {address}:{port} is already in use. '
                                 f'Rerun with --{app}-port and a different port.') from exc
            if exc.errno == errno.EADDRNOTAVAIL:
                raise ValueError(f'{address} is not assigned to this VM. Use --dashboard-bind 127.0.0.1 '
                                 'or use the VM’s LAN IPv4 address.') from exc
            raise


def install(args):
    import fcntl

    if os.getuid() == 0:
        raise ValueError('Run the installer without sudo as the Linux account that will own the stack data.')
    print('Checking Docker Engine and Compose…', flush=True)
    docker_root = preflight_docker()

    state = args.state_dir.expanduser().resolve()
    if state.is_relative_to(COMPOSE_FILE.parents[2]):
        raise ValueError('Choose a state directory outside the repository for application data and secrets.')
    ports = (args.dashboard_port, args.sonarr_port, args.radarr_port)
    if len(set(ports)) != len(ports) or any(not 1024 <= port <= 65535 for port in ports):
        raise ValueError('Choose three distinct ports between 1024 and 65535.')
    try:
        bind = ipaddress.IPv4Address(args.dashboard_bind)
    except ipaddress.AddressValueError as exc:
        raise ValueError('Use --dashboard-bind 127.0.0.1 or a specific IPv4 address assigned to this VM.') from exc
    if bind.is_unspecified or bind.is_multicast:
        raise ValueError('Choose loopback or a specific host IPv4 address for the dashboard.')

    suffix = hashlib.sha256(os.fsencode(state)).hexdigest()[:16]
    project = f'martinflix-first-run-{suffix}'
    compose = ['docker', 'compose', '--env-file', '/dev/null', '-f', str(COMPOSE_FILE), '-p', project]
    environment = dict(os.environ, MARTINFLIX_STATE_DIR=str(state),
                       MARTINFLIX_UID=str(os.getuid()), MARTINFLIX_GID=str(os.getgid()),
                       MARTINFLIX_DASHBOARD_BIND=str(bind), MARTINFLIX_DASHBOARD_PORT=str(ports[0]),
                       MARTINFLIX_SONARR_PORT=str(ports[1]), MARTINFLIX_RADARR_PORT=str(ports[2]))

    print('Checking storage and service ports…', flush=True)
    for directory in (state, state / 'dashboard-secrets', *(state / app for app in APPS)):
        check_directory(directory)
    check_ports(compose, environment, [('dashboard', str(bind), ports[0]),
                                         ('sonarr', '127.0.0.1', ports[1]), ('radarr', '127.0.0.1', ports[2])])
    try:
        if shutil.disk_usage(docker_root).free < 2 * 1024**3:
            print('Docker storage has less than 2 GiB free. Free up space if the image download or build fails.', flush=True)
    except OSError:
        pass
    if args.check:
        print('Preflight passed. Run the same command without --check to start the installation.')
        return
    private_directory(state)

    def compose_step(label, command):
        print(label, flush=True)
        try:
            subprocess.run(compose + command, env=environment, check=True)
        except subprocess.CalledProcessError as exc:
            list_command = shlex.join(['docker', 'ps', '-a', '--filter', f'label=com.docker.compose.project={project}'])
            raise ValueError(f'{label} failed. Check the Docker error above. If an image could not be downloaded, '
                             'check the VM’s internet connection and Docker storage.\n'
                             f'To find the containers, run: {list_command}\n'
                             'For a failed container, run docker logs followed by its name. '
                             'Then rerun setup with the same options.') from exc

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
        compose_step('Starting Sonarr and Radarr', ['up', '-d', '--wait', '--wait-timeout', '120', *APPS])
        for app in APPS:
            write_secret(secret_dir / f'{app}-key', wait_for_key(state / app / 'config.xml'))
        compose_step('Building and starting the dashboard', ['up', '-d', '--build', '--wait', '--wait-timeout', '90', 'dashboard'])
        verify = '''
import server
for app in ('sonarr', 'radarr'):
    result = server.arr_data(app)
    if not result['configured'] or result.get('error'):
        raise SystemExit(f'{app} verification failed: {result.get("error", "key unavailable")}')
    print(f'{app} monitoring connected', flush=True)
'''
        compose_step('Verifying the Sonarr and Radarr connections', ['exec', '-T', 'dashboard', 'python', '-B', '-c', verify])
    print(f'Dashboard: http://{bind}:{ports[0]} (admin, password file: {password})')
    print(f'Sonarr: http://127.0.0.1:{ports[1]}  Radarr: http://127.0.0.1:{ports[2]}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', type=Path, default=Path.home() / '.local/state/martinflix-first-run')
    parser.add_argument('--dashboard-bind', default='127.0.0.1')
    parser.add_argument('--dashboard-port', type=int, default=8765)
    parser.add_argument('--sonarr-port', type=int, default=8989)
    parser.add_argument('--radarr-port', type=int, default=7878)
    parser.add_argument('--check', action='store_true', help='Check prerequisites without creating files or starting containers.')
    args = parser.parse_args()
    if sys.platform != 'linux':
        parser.exit(1, 'Run this installer on a Linux VM.\n')
    if sys.version_info < (3, 9):
        version = '.'.join(map(str, sys.version_info[:3]))
        parser.exit(1, f'Python {version} is too old for this installer. Python 3.9 or newer is required.\n'
                    'Run cat /etc/os-release to identify your Linux distribution. Install a newer Python 3 '
                    'through its package manager, or upgrade the Linux release if it offers only older Python.\n'
                    'Keep the system Python in place. If you installed a newer version alongside it, run it by name, '
                    'for example: python3.11 deploy/bootstrap/install.py --check\n')
    os.umask(0o077)
    try:
        install(args)
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        if isinstance(exc, subprocess.CalledProcessError):
            parser.exit(1, 'Could not check the Compose project. Run docker compose version and docker info, then try again.\n')
        if isinstance(exc, subprocess.TimeoutExpired):
            parser.exit(1, 'Docker did not respond. Check that Docker is running, then try again.\n')
        parser.exit(1, f'{exc}\n')


if __name__ == '__main__':
    main()
