#!/usr/bin/env python3
"""Start or resume the first-run MartinFlix Compose project."""
import argparse
import errno
import getpass
import hashlib
import ipaddress
import json
import os
import re
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

from configure import (AppApi, BazarrApi, TransmissionRpc, configure_apps, configure_bazarr, ensure_arr_login,
                       ensure_bazarr_login, seed_transmission_defaults)

COMPOSE_FILE = Path(__file__).resolve().with_name('compose.yaml')
APPS = ('sonarr', 'radarr')
DOCKER_INSTALL = 'https://docs.docker.com/engine/install/'
COMPOSE_INSTALL = 'https://docs.docker.com/compose/install/linux/'

BANNER = r"""
     /\        __  __            _   _       _____ _ _
    /  \      |  \/  | __ _ _ __| |_(_)_ __ |  ___| (_)_  __
   /____\     | |\/| |/ _` | '__| __| | '_ \| |_  | | \ \/ /
   | >  |     | |  | | (_| | |  | |_| | | | |  _| | | |>  <
   |____|     |_|  |_|\__,_|_|   \__|_|_| |_|_|   |_|_/_/\_\

              Home media setup
"""
MEDIA_FOLDERS = ('TV Shows', 'Movies', 'downloads')
# Transmission files finished downloads under its download-dir plus each app's category. It creates a category
# folder only for the first torrent, and until then Sonarr and Radarr report the mapped path as missing.
DOWNLOAD_FOLDERS = ('downloads/complete', 'downloads/complete/sonarr', 'downloads/complete/radarr', 'downloads/incomplete')
DOWNLOAD_APPS = ('transmission', 'prowlarr', 'flaresolverr', 'bazarr')
# --enable-downloads and installs made before Bazarr joined mean exactly these three.
LEGACY_DOWNLOADS = ('transmission', 'prowlarr', 'flaresolverr')
DOWNLOAD_PORTS = {'transmission': 9091, 'prowlarr': 9696, 'bazarr': 6767}
CONTAINER_PORTS = {'dashboard': 8765, 'sonarr': 8989, 'radarr': 7878, **DOWNLOAD_PORTS}
DEFAULT_PORTS = {**CONTAINER_PORTS, 'transmission_peer': 51155}
TRANSMISSION_SETTINGS = Path(__file__).resolve().with_name('transmission-settings.json')


def saved_options(state):
    try:
        options = json.loads((state / 'install-options.json').read_text())
    except FileNotFoundError:
        return {}
    if not isinstance(options, dict):
        raise ValueError('The saved installation options are invalid.')
    return options


def has_installation(path):
    return (path / 'install-options.json').exists() or (path / 'dashboard-secrets/admin-password').exists()


def resolve_state_directory(requested):
    if requested is not None:
        return requested.expanduser().resolve()
    state_home = Path.home() / '.local/state'
    current = state_home / 'martinflix'
    legacy = state_home / 'martinflix-first-run'
    if has_installation(legacy):
        if has_installation(current):
            raise ValueError('Two MartinFlix installations were found. Choose one with --state-dir.')
        return legacy.resolve()
    return current.resolve()


def compose_project(state, previous, fresh=False):
    suffix = hashlib.sha256(os.fsencode(state)).hexdigest()[:16]
    current = f'martinflix-{suffix}'
    legacy = f'martinflix-first-run-{suffix}'
    if fresh:
        return current
    project = previous.get('project')
    if project is None:
        return legacy if has_installation(state) else current
    if project not in (current, legacy):
        raise ValueError('The saved Docker project does not match this state directory. Use the original --state-dir.')
    return project


def media_root(value, state):
    path = Path(value).expanduser().resolve()
    repo = COMPOSE_FILE.parents[2]
    if path.is_relative_to(state) or state.is_relative_to(path) or path.is_relative_to(repo) or repo.is_relative_to(path):
        raise ValueError('Choose a dedicated media directory separate from the checkout and installer state.')
    if not path.is_dir() or not os.access(path, os.W_OK | os.X_OK):
        raise ValueError(f'The media directory must already exist and be writable: {path}')
    for name in (*MEDIA_FOLDERS, *DOWNLOAD_FOLDERS):
        folder = path / name
        if folder.is_symlink() or (folder.exists() and (
                not folder.is_dir() or not os.access(folder, os.W_OK | os.X_OK))):
            raise ValueError(f'The media subdirectory must be writable and cannot be a link: {folder}')
    return path


def private_secret(path):
    if not path.exists() and not path.is_symlink():
        return None
    if path.is_symlink() or not path.is_file() or path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
        raise ValueError(f'{path} must be a private file owned by your account.')
    return path.read_text().strip() or None


def arr_needs_login(path):
    try:
        method = ElementTree.parse(path).getroot().findtext('AuthenticationMethod', 'none')
        return method.casefold() == 'none'
    except (FileNotFoundError, ElementTree.ParseError):
        return True


def prepare_vpn_secrets(path):
    private_directory(path)
    credentials = {name: private_secret(path / name) for name in ('openvpn_user', 'openvpn_password')}
    if not all(credentials.values()):
        if not sys.stdin.isatty():
            raise ValueError('Run setup in a terminal to enter your NordVPN OpenVPN service credentials.')
        print('Enter the manual OpenVPN service credentials from your Nord Account.', flush=True)
        for name, prompt in [('openvpn_user', 'NordVPN service username: '), ('openvpn_password', 'NordVPN service password: ')]:
            if not credentials[name]:
                credentials[name] = getpass.getpass(prompt).strip()
            if not credentials[name]:
                raise ValueError('Both NordVPN service credentials are needed.')
    for name, value in credentials.items():
        write_secret(path / name, value, trailing_newline=False)


def prepare_transmission_password(path, temporary_password, personal_password_set):
    """Transmission signs in with the dashboard password, like every other app in the stack.

    Until the admin chooses a personal password, every app uses the temporary dashboard password. After that the
    dashboard keeps this file in step whenever the password changes, so setup leaves it alone.
    """
    private_directory(path)
    target = path / 'transmission_password'
    current = private_secret(target)
    password = current if current and personal_password_set else temporary_password
    # LinuxServer loads every byte from FILE__PASS, including any trailing newline. Running containers bind-mount
    # this file, so it is rewritten in place rather than replaced.
    write_secret(target, password, trailing_newline=False, in_place=True)
    return password


def private_directory(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
        raise ValueError(f'{path} must be a private directory owned by the current account (mode 0700).')


def write_secret(path, value, trailing_newline=True, in_place=False):
    content = value + '\n' if trailing_newline else value
    if path.is_symlink():
        raise ValueError(f'{path} must be a regular file.')
    if path.exists():
        if not path.is_file() or path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
            raise ValueError(f'{path} must be private and owned by the current account.')
        if path.read_text() == content:
            return
        if in_place:
            with path.open('r+') as output:
                output.write(content)
                output.truncate()
            return
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as output:
        temporary = Path(output.name)
        output.write(content)
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def bazarr_setting(state_dir, section, key):
    """Read one top-level section value from Bazarr's config.yaml without a YAML library."""
    try:
        lines = (state_dir / 'config/config.yaml').read_text().splitlines()
    except FileNotFoundError:
        return None
    inside = False
    for line in lines:
        if not line.startswith((' ', '\t')):
            inside = line.rstrip() == f'{section}:'
            continue
        name, _, value = line.strip().partition(':')
        if inside and name == key:
            value = value.strip().strip('\'"')
            return None if value in ('', 'null') else value
    return None


def bazarr_needs_login(state_dir):
    return bazarr_setting(state_dir, 'auth', 'type') not in ('form', 'basic')


def wait_for_bazarr_key(state_dir):
    for _ in range(60):
        key = bazarr_setting(state_dir, 'auth', 'apikey')
        if key:
            return key
        time.sleep(1)
    raise ValueError('Bazarr did not write its API key in time. Check its Compose logs and rerun.')


def opensubtitles_login():
    """OpenSubtitles.com is the one enabled subtitle provider that needs an account. Blank skips it."""
    if not sys.stdin.isatty():
        return None
    print('Bazarr can search OpenSubtitles.com with your account. Leave the username blank to skip it.', flush=True)
    username = input('OpenSubtitles.com username: ').strip()
    if not username:
        return None
    password = getpass.getpass('OpenSubtitles.com password: ')
    return (username, password) if password else None


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


PORT_PATTERN = re.compile(r'(\d{1,3}(?:\.\d{1,3}){3}):(\d+)->(\d+)/tcp')


def published_ports(projects):
    """Return (service, address, host port, container port) for the given Compose projects."""
    result = docker_check(['ps', '-a', '--format',
                           '{{.Label "com.docker.compose.project"}}\t{{.Label "com.docker.compose.service"}}\t{{.Ports}}'])
    if result.returncode:
        raise ValueError('Could not list Docker containers. Run docker ps, then try again.')
    published = []
    for line in result.stdout.splitlines():
        project, _, rest = line.partition('\t')
        service, _, ports = rest.partition('\t')
        if project not in projects:
            continue
        for address, port, target in PORT_PATTERN.findall(ports):
            published.append((service, address, int(port), int(target)))
    return published


def check_ports(bindings, published):
    owned = {(address, port) for _, address, port, _ in published}
    for app, address, port in bindings:
        if (address, port) in owned:
            continue
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                listener.bind((address, port))
        except OSError as exc:
            if exc.errno == errno.EADDRINUSE:
                raise ValueError(f'{app}: {address}:{port} is already in use. '
                                 f'Rerun with --{app.replace("_", "-")}-port and a different port.') from exc
            if exc.errno == errno.EADDRNOTAVAIL:
                raise ValueError(f'{address} is not assigned to this VM. Use --dashboard-bind 127.0.0.1 '
                                 'or use the VM’s LAN IPv4 address.') from exc
            raise


def running_settings(published):
    """Recover the address and ports of installs created before setup saved them."""
    bind = None
    ports = {}
    for service, address, port, target in published:
        if service == 'dashboard' and target == 8765:
            bind = address
        for name, container_port in CONTAINER_PORTS.items():
            if target == container_port and (service == name or (service == 'gluetun' and name in DOWNLOAD_PORTS)):
                ports[name] = port
    return bind, ports


def download_overlay(apps, ports, behind_vpn):
    services = {'dashboard': {'environment': {
        'MARTINFLIX_DOWNLOAD_APPS': ','.join(apps),
        'MARTINFLIX_DOWNLOAD_GATEWAY': 'gluetun' if behind_vpn else '',
        'MARTINFLIX_DOWNLOAD_BIND': '${MARTINFLIX_DOWNLOAD_BIND}',
    }}}
    if 'transmission' in apps:
        # The dashboard writes password changes here; Transmission's sidecar applies them.
        services['dashboard']['environment']['MARTINFLIX_TRANSMISSION_PASSWORD_FILE'] = '/run/transmission-password'
        services['dashboard']['volumes'] = [{
            'type': 'bind', 'source': '${MARTINFLIX_STATE_DIR}/download-secrets/transmission_password',
            'target': '/run/transmission-password', 'bind': {'create_host_path': False},
        }]
    for app in apps:
        configuration = services.setdefault(app, {})
        if behind_vpn:
            configuration.update(network_mode='service:gluetun', depends_on={
                'gluetun': {'condition': 'service_healthy', 'restart': True},
            })
        if app == 'transmission' and not behind_vpn:
            # Lets other peers connect in. NordVPN forwards no ports, so behind the VPN this would do nothing.
            peer = ports['transmission_peer']
            configuration.setdefault('ports', []).extend([f'{peer}:{peer}/tcp', f'{peer}:{peer}/udp'])
        if app in DOWNLOAD_PORTS:
            services['dashboard']['environment'][f'MARTINFLIX_{app.upper()}_WEB_PORT'] = str(ports[app])
            owner = services.setdefault('gluetun', {}) if behind_vpn else configuration
            # Same address as Sonarr and Radarr; setup keeps it on loopback until Prowlarr has a login.
            owner.setdefault('ports', []).append(f'${{MARTINFLIX_DOWNLOAD_BIND}}:{ports[app]}:{DOWNLOAD_PORTS[app]}')
    return {'services': services}


def reset_installation(state, project):
    import fcntl

    print(f'\nReset MartinFlix at {state}?')
    print('Setup will stop this stack and archive its app databases, configuration, and credentials. '
          'Media files stay in place.')
    if not sys.stdin.isatty() or input('Type RESET to continue: ') != 'RESET':
        raise ValueError('Reset cancelled. Nothing was changed.')
    with (state / 'install.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not has_installation(state):
            raise ValueError('The installation has already been reset.')
        environment = dict(os.environ, MARTINFLIX_STATE_DIR=str(state), MARTINFLIX_UID=str(os.getuid()),
                           MARTINFLIX_GID=str(os.getgid()), MARTINFLIX_DASHBOARD_BIND='127.0.0.1',
                           MARTINFLIX_DASHBOARD_PORT='8765', MARTINFLIX_SONARR_PORT='8989', MARTINFLIX_RADARR_PORT='7878')
        subprocess.run(['docker', 'compose', '--env-file', '/dev/null', '-p', project, '-f', str(COMPOSE_FILE),
                        'down', '--remove-orphans'], env=environment, timeout=120, check=True)
        archive = Path(tempfile.mkdtemp(prefix='reset-backup-', dir=state))
        managed = ('install-options.json', 'dashboard-auth', 'dashboard-secrets', 'download-secrets',
                   'downloads-compose.json', 'sonarr', 'radarr', 'prowlarr', 'transmission', 'bazarr')
        moved = []
        try:
            for name in managed:
                source = state / name
                if source.exists() or source.is_symlink():
                    source.rename(archive / name)
                    moved.append(name)
        except OSError:
            for name in reversed(moved):
                (archive / name).rename(state / name)
            archive.rmdir()
            raise
    print(f'Previous app data and credentials archived at {archive}', flush=True)


def plan_installation(args, state, previous, project, overlay_file, fresh_arr=False):
    """Validate every option and work out the Compose command. Changes nothing on disk except the overlay."""
    old_media = previous.get('media_path')
    media = media_root(args.media_path or old_media, state) if args.media_path or old_media else None
    if old_media and str(media) != old_media:
        raise ValueError('This installation already uses a media directory. Move or remount it at the same path, '
                         'or use a separate --state-dir for a different library.')
    enabled = set(previous.get('download_apps', LEGACY_DOWNLOADS if previous.get('downloads') else ()))
    if args.enable_downloads:
        enabled.update(LEGACY_DOWNLOADS)
    if args.download_client:
        enabled.add(args.download_client)
    if args.enable_prowlarr:
        enabled.add('prowlarr')
    if args.enable_flaresolverr:
        enabled.add('flaresolverr')
    if args.enable_bazarr:
        enabled.add('bazarr')
    downloads = [app for app in DOWNLOAD_APPS if app in enabled]
    if 'flaresolverr' in enabled and 'prowlarr' not in enabled:
        raise ValueError('Choose --enable-prowlarr to install FlareSolverr alongside Prowlarr.')
    if 'transmission' in enabled and not media:
        raise ValueError('To install Transmission, specify your dedicated media directory with --media-path.')
    if 'bazarr' in enabled and not media:
        raise ValueError('Bazarr saves subtitles beside your videos. Specify your media directory with --media-path.')
    if args.download_network and not downloads:
        raise ValueError('Select a download app before choosing its network route.')
    previous_network = previous.get('download_network') or ('vpn' if previous.get('downloads') else None)
    default_network = 'vpn' if args.enable_downloads or args.enable_vpn or args.vpn_country or args.vpn_city or previous.get('vpn') else 'direct'
    download_network = (args.download_network or previous_network or default_network) if downloads else None
    if previous_network and download_network != previous_network:
        raise ValueError('This download stack already has a network route. Changing it requires migrating its app connections. '
                         'To choose a different route, start from scratch with --reset.')
    vpn = bool(args.enable_vpn or args.vpn_country or args.vpn_city or previous.get('vpn') or download_network == 'vpn')
    vpn_country = args.vpn_country or ('' if args.vpn_city else previous.get('vpn_country') or '')
    vpn_city = args.vpn_city or ('' if args.vpn_country else previous.get('vpn_city'))
    if vpn_city is None:
        vpn_city = '' if vpn_country else os.environ.get('MARTINFLIX_VPN_CITY', 'Prague')
    if vpn and not Path('/dev/net/tun').is_char_device():
        raise ValueError('The VPN needs /dev/net/tun on this VM. Try sudo modprobe tun. If it is still unavailable, '
                         'enable TUN access in your VM settings and rerun setup.')

    saved_ports = previous.get('ports') or {}
    ports = {}
    for name in ('dashboard', 'sonarr', 'radarr', *(app for app in downloads if app in DOWNLOAD_PORTS),
                 *(['transmission_peer'] if 'transmission' in downloads else [])):
        requested = getattr(args, name + '_port')
        ports[name] = requested if requested is not None else saved_ports.get(name, DEFAULT_PORTS[name])
    if len(set(ports.values())) != len(ports) or any(not 1024 <= port <= 65535 for port in ports.values()):
        raise ValueError('Choose distinct ports between 1024 and 65535 for the selected services.')
    try:
        bind = ipaddress.IPv4Address(args.dashboard_bind or previous.get('dashboard_bind') or '127.0.0.1')
    except ipaddress.AddressValueError as exc:
        raise ValueError('Use --dashboard-bind 127.0.0.1 or a specific IPv4 address assigned to this VM.') from exc
    if bind.is_unspecified or bind.is_multicast:
        raise ValueError('Choose loopback or a specific host IPv4 address for the dashboard.')

    options = {**previous, 'media_path': str(media) if media else None, 'downloads': bool(downloads),
               'download_apps': downloads, 'project': project, 'dashboard_bind': str(bind), 'ports': ports}
    options.update(vpn=vpn, vpn_country=vpn_country, vpn_city=vpn_city, download_network=download_network)

    compose = ['docker', 'compose', '--env-file', '/dev/null', '-p', project, '-f', str(COMPOSE_FILE)]
    if media:
        compose += ['-f', str(COMPOSE_FILE.with_name('compose.media.yaml'))]
    if vpn:
        compose += ['-f', str(COMPOSE_FILE.with_name('compose.vpn.yaml'))]
    if downloads:
        overlay_file.write_text(json.dumps(download_overlay(downloads, ports, download_network == 'vpn')))
        compose += ['-f', str(COMPOSE_FILE.with_name('compose.downloads.yaml')), '-f', str(overlay_file)]
        for app in downloads:
            compose += ['--profile', app]
    environment = dict(os.environ, MARTINFLIX_STATE_DIR=str(state),
                       MARTINFLIX_UID=str(os.getuid()), MARTINFLIX_GID=str(os.getgid()),
                       MARTINFLIX_DASHBOARD_BIND=str(bind), MARTINFLIX_MEDIA_PATH=str(media) if media else '')
    environment.update(MARTINFLIX_VPN_PROVIDER=args.vpn_provider, MARTINFLIX_VPN_COUNTRY=vpn_country, MARTINFLIX_VPN_CITY=vpn_city)
    environment.update({f'MARTINFLIX_{service.upper()}_PORT': str(port) for service, port in ports.items()})
    app_dirs = (*APPS, *(app for app in downloads if app in DOWNLOAD_PORTS))

    for name in ('dashboard-secrets', 'dashboard-auth', *app_dirs):
        check_directory(state / name)
    check_directory(state)
    if vpn or downloads:
        check_directory(state / 'download-secrets')
    bootstrapping_arr = fresh_arr or any(arr_needs_login(state / app / 'config.xml') for app in APPS)
    # Prowlarr and Bazarr start without a login, so they stay on loopback until setup has given them one.
    bootstrapping_prowlarr = (('prowlarr' in downloads and (fresh_arr or arr_needs_login(state / 'prowlarr/config.xml')))
                              or ('bazarr' in downloads and (fresh_arr or bazarr_needs_login(state / 'bazarr'))))
    environment['MARTINFLIX_DOWNLOAD_BIND'] = str(bind)
    bindings = [(service, str(bind), port) for service, port in ports.items() if service != 'transmission_peer']
    if 'transmission_peer' in ports and download_network != 'vpn':
        bindings.append(('transmission_peer', '0.0.0.0', ports['transmission_peer']))
    if bootstrapping_arr and str(bind) != '127.0.0.1':
        bindings.extend((app, '127.0.0.1', ports[app]) for app in APPS)
    if bootstrapping_prowlarr and str(bind) != '127.0.0.1':
        bindings.extend((app, '127.0.0.1', ports[app]) for app in downloads if app in DOWNLOAD_PORTS)
    return {
        'media': media, 'enabled': enabled, 'downloads': downloads, 'download_network': download_network,
        'vpn': vpn, 'vpn_country': vpn_country, 'vpn_city': vpn_city, 'options': options, 'ports': ports,
        'bind': bind, 'compose': compose, 'environment': environment, 'app_dirs': app_dirs,
        'bootstrapping_arr': bootstrapping_arr, 'bootstrapping_prowlarr': bootstrapping_prowlarr, 'bindings': bindings,
    }


def refuse_root():
    if os.getuid() == 0:
        raise ValueError('Run the installer without sudo as the Linux account that will own the stack data.')


def install(args, overlay_file):
    refuse_root()
    print(BANNER, flush=True)
    print('1. Checking Docker, storage, and network ports…', flush=True)
    docker_root = preflight_docker()

    state = resolve_state_directory(args.state_dir)
    if state.is_relative_to(COMPOSE_FILE.parents[2]):
        raise ValueError('Choose a state directory outside the repository for application data and secrets.')
    check_directory(state)
    existing = has_installation(state)
    if (args.reset or args.update) and not existing:
        flag = '--reset' if args.reset else '--update'
        raise ValueError(f'No installer-managed MartinFlix installation was found at {state}. '
                         f'Check --state-dir, or run setup without {flag} for a new installation.')
    saved = saved_options(state)
    old_project = compose_project(state, saved) if existing else None
    project = compose_project(state, {} if args.reset else saved, fresh=args.reset)
    published = published_ports({project, old_project} - {None})
    current = dict(saved)
    if existing and 'ports' not in current:
        running_bind, running_ports = running_settings(published)
        current['ports'] = running_ports
        if running_bind:
            current['dashboard_bind'] = running_bind
        elif args.dashboard_bind is None and not args.reset:
            print('This installation was created before setup remembered its address, and its dashboard is not running. '
                  'If you open MartinFlix from another device, add --dashboard-bind with the VM’s LAN address.', flush=True)
    # A reset discards the saved options, so validate the new installation as if it were fresh.
    previous = {} if args.reset else current
    plan = plan_installation(args, state, previous, project, overlay_file, fresh_arr=args.reset)
    check_ports(plan['bindings'], published)
    try:
        if shutil.disk_usage(docker_root).free < 2 * 1024**3:
            print('Docker storage has less than 2 GiB free. Free up space if the image download or build fails.', flush=True)
    except OSError:
        pass
    describe_plan(plan, state, args, current if args.reset else None)
    if args.check:
        if args.reset:
            print('Preflight passed. Run the same command without --check. Setup will ask you to type RESET '
                  'before it stops and archives the current installation.')
        else:
            print('Preflight passed. Run the same command without --check to start the installation.')
        return
    if args.reset:
        reset_installation(state, old_project)
        previous = {}
    run_installation(plan, state, previous, mode='reset' if args.reset else 'update' if existing else 'install')


def current_settings(options):
    """Describe a saved installation the same way as a plan, for comparison."""
    vpn = bool(options.get('vpn'))
    apps = list(options.get('download_apps', LEGACY_DOWNLOADS if options.get('downloads') else ()))
    return {
        'Address': options.get('dashboard_bind') or '127.0.0.1',
        'Media directory': options.get('media_path') or 'Not set',
        'VPN': f'NordVPN · {options.get("vpn_city") or options.get("vpn_country") or "Automatic"}' if vpn else 'Not installed',
        'Download apps': ', '.join(apps) or 'None',
        'Download route': (options.get('download_network') or ('vpn' if options.get('downloads') else 'direct')) if apps else 'None',
        **{f'{name.replace("_", " ").capitalize()} port': str(port) for name, port in (options.get('ports') or {}).items()},
    }


def keep_flags(options):
    """The options that recreate a saved installation's settings after a reset."""
    flags = []
    if options.get('dashboard_bind') and options['dashboard_bind'] != '127.0.0.1':
        flags += ['--dashboard-bind', options['dashboard_bind']]
    if options.get('media_path'):
        flags += ['--media-path', options['media_path']]
    if options.get('vpn'):
        flags.append('--enable-vpn')
        if options.get('vpn_city'):
            flags += ['--vpn-city', options['vpn_city']]
        elif options.get('vpn_country'):
            flags += ['--vpn-country', options['vpn_country']]
    apps = options.get('download_apps', LEGACY_DOWNLOADS if options.get('downloads') else ())
    if 'transmission' in apps:
        flags += ['--download-client', 'transmission']
    if 'prowlarr' in apps:
        flags.append('--enable-prowlarr')
    if 'flaresolverr' in apps:
        flags.append('--enable-flaresolverr')
    if apps:
        flags += ['--download-network', options.get('download_network') or ('vpn' if options.get('downloads') else 'direct')]
    for name, port in (options.get('ports') or {}).items():
        if port != DEFAULT_PORTS.get(name):
            flags += [f'--{name.replace("_", "-")}-port', str(port)]
    return flags


def describe_plan(plan, state, args, current=None):
    mode = 'Start from scratch' if args.reset else 'Update' if args.update else 'Install'
    if current is not None:
        before = current_settings(current)
        after = current_settings(plan['options'])
        # Ports and routes of apps that are going away are covered by the Download apps line.
        changes = [(label, before.get(label, 'Not set'), after.get(label, 'Not set'))
                   for label in dict.fromkeys([*before, *after]) if before.get(label) != after.get(label)
                   and label in after and not (label == 'Download route' and after[label] == 'None')]
        if changes:
            width = max(len(label) for label, _, _ in changes) + 2
            print('\nChanges from the current installation')
            for label, old, new in changes:
                print(f'{label:<{width}}{old} → {new}')
            flags = keep_flags(current)
            if flags:
                print('To keep the current settings instead, rerun with:')
                print('  ' + shlex.join(flags))
    print(f'\n{mode} plan')
    print(f'State directory   {state}')
    print(f'Address           {plan["bind"]}')
    print(f'Media directory   {plan["media"] or "Not set"}')
    if plan['vpn']:
        print(f'VPN               NordVPN · {plan["vpn_city"] or plan["vpn_country"] or "Automatic"}')
    if plan['downloads']:
        route = 'VPN' if plan['download_network'] == 'vpn' else 'normal internet connection'
        print(f'Download apps     {", ".join(plan["downloads"])} via {route}')
    print('Ports             ' + ', '.join(f'{name} {port}' for name, port in plan['ports'].items()))
    if plan['vpn']:
        print('VPN selected. Setup will use your NordVPN OpenVPN service credentials.')
    print()


def run_installation(plan, state, previous, mode='install'):
    import fcntl

    media, enabled, downloads = plan['media'], plan['enabled'], plan['downloads']
    vpn, download_network, options = plan['vpn'], plan['download_network'], plan['options']
    ports, bind, compose, environment = plan['ports'], plan['bind'], plan['compose'], plan['environment']
    app_dirs, bootstrapping_arr = plan['app_dirs'], plan['bootstrapping_arr']
    bootstrapping_prowlarr = plan['bootstrapping_prowlarr']
    project = options['project']
    private_directory(state)

    def compose_step(label, command):
        print(label, flush=True)
        with tempfile.TemporaryFile() as log:
            result = subprocess.run(compose + command, env=environment, stdout=log, stderr=subprocess.STDOUT)
            if result.returncode == 0:
                return
            log.seek(max(0, log.tell() - 12000))
            details = log.read().decode(errors='replace').strip()
        if details:
            print(f'\nDocker details\n{details}\n', file=sys.stderr)
        list_command = shlex.join(['docker', 'ps', '-a', '--filter', f'label=com.docker.compose.project={project}'])
        raise ValueError('This step could not finish. Check the Docker details above. '
                         'If an image could not be downloaded, check this VM’s internet connection and Docker storage.\n'
                         f'To find the containers, run: {list_command}\n'
                         'For a failed container, run docker logs followed by its name. '
                         'Then rerun setup with the same options.')

    with (state / 'install.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if {key: value for key, value in saved_options(state).items() if key not in ('ports', 'dashboard_bind')} != \
                {key: value for key, value in previous.items() if key not in ('ports', 'dashboard_bind')}:
            raise ValueError('Setup options changed while this command was waiting. Run the installer again.')
        secret_dir = state / 'dashboard-secrets'
        private_directory(secret_dir)
        auth_dir = state / 'dashboard-auth'
        private_directory(auth_dir)
        password = secret_dir / 'admin-password'
        if password.exists() or password.is_symlink():
            if password.is_symlink() or not password.is_file() or password.stat().st_uid != os.getuid() or password.stat().st_mode & 0o077:
                raise ValueError('The existing dashboard password file must be private and owned by the current account.')
            if len(password.read_text().rstrip('\r\n')) < 16:
                raise ValueError('The existing dashboard password must be at least 16 characters.')
        else:
            write_secret(password, secrets.token_urlsafe(32))
        temporary_password = password.read_text().rstrip('\r\n')
        personal_password_set = (auth_dir / 'password-hash').is_file()
        if vpn:
            prepare_vpn_secrets(state / 'download-secrets')
        seed_bazarr = 'bazarr' in downloads and not options.get('bazarr_defaults_applied')
        opensubtitles = opensubtitles_login() if seed_bazarr else None
        transmission_password = (prepare_transmission_password(state / 'download-secrets', temporary_password,
                                                               personal_password_set)
                                 if 'transmission' in enabled else None)
        if media:
            for folder in MEDIA_FOLDERS:
                (media / folder).mkdir(mode=0o700, exist_ok=True)
            if 'transmission' in downloads:
                for folder in DOWNLOAD_FOLDERS:
                    (media / folder).mkdir(mode=0o700, exist_ok=True)
        if downloads:
            runtime_overlay = state / 'downloads-compose.json'
            overlay_index = next(index for index, item in enumerate(compose) if item.endswith('downloads.json'))
            write_secret(runtime_overlay, Path(compose[overlay_index]).read_text())
            compose[overlay_index] = str(runtime_overlay)
        options.setdefault('radarr_indexer_defaults_pending', not (state / 'radarr/config.xml').exists())
        for app in app_dirs:
            private_directory(state / app)
        if 'transmission' in downloads and not (state / 'transmission/settings.json').exists():
            # The image copies its own defaults only when this file is missing, so ours win on a new install.
            write_secret(state / 'transmission/settings.json', TRANSMISSION_SETTINGS.read_text(), trailing_newline=False)
            options['transmission_defaults_applied'] = True
        if options != saved_options(state):
            write_secret(state / 'install-options.json', json.dumps(options))

        arr_address = str(bind)
        if bootstrapping_arr:
            arr_address = '127.0.0.1'
            environment['MARTINFLIX_DASHBOARD_BIND'] = arr_address
        sidecars = ['transmission-auth'] if 'transmission' in downloads else []
        images = [*APPS, *(['gluetun'] if vpn else []), *downloads, *sidecars]
        compose_step('2. Downloading the latest app images. The first download may take a few minutes…',
                     ['pull', '--quiet', *images])
        compose_step('3. Starting Sonarr and Radarr…', ['up', '-d', '--wait', '--wait-timeout', '120', *APPS])
        arr_keys = {app: wait_for_key(state / app / 'config.xml') for app in APPS}
        for app, key in arr_keys.items():
            write_secret(secret_dir / f'{app}-key', key)
            api = AppApi(app.capitalize(), ports[app], key, address=arr_address)
            ensure_arr_login(api, lambda: temporary_password)
        if bootstrapping_arr and arr_address != str(bind):
            arr_address = str(bind)
            environment['MARTINFLIX_DASHBOARD_BIND'] = arr_address
            compose_step('   Opening the protected Sonarr and Radarr logins on your LAN…',
                         ['up', '-d', '--wait', '--wait-timeout', '120', *APPS])
        step = 4
        prowlarr_key = None
        services = (['gluetun'] if vpn else []) + downloads + sidecars
        download_address = '127.0.0.1' if bootstrapping_prowlarr else str(bind)
        environment['MARTINFLIX_DOWNLOAD_BIND'] = download_address
        if services:
            label = 'Starting the VPN and selected services' if vpn else 'Starting selected download and tracker apps'
            compose_step(f'{step}. {label}…', ['up', '-d', '--wait', '--wait-timeout', '210', *services])
            step += 1
        if 'prowlarr' in enabled:
            prowlarr_key = wait_for_key(state / 'prowlarr/config.xml')
            write_secret(secret_dir / 'prowlarr-key', prowlarr_key)
            ensure_arr_login(AppApi('Prowlarr', ports['prowlarr'], prowlarr_key, version=1, address=download_address),
                             lambda: temporary_password)
        bazarr = None
        if 'bazarr' in enabled:
            bazarr_key = wait_for_bazarr_key(state / 'bazarr')
            write_secret(secret_dir / 'bazarr-key', bazarr_key)
            bazarr = BazarrApi(download_address, ports['bazarr'], bazarr_key)
            ensure_bazarr_login(bazarr, lambda: temporary_password)
        if download_address != str(bind):
            download_address = str(bind)
            environment['MARTINFLIX_DOWNLOAD_BIND'] = download_address
            compose_step('   Opening the protected download app logins on your LAN…',
                         ['up', '-d', '--wait', '--wait-timeout', '210', *services])
            if bazarr:
                bazarr = BazarrApi(download_address, ports['bazarr'], bazarr.key)
        print(f'{step}. Configuring library defaults and checking app connections…', flush=True)
        if bazarr:
            configure_bazarr(bazarr, arr_keys, seed=seed_bazarr, opensubtitles=opensubtitles)
            if seed_bazarr:
                options['bazarr_defaults_applied'] = True
                write_secret(state / 'install-options.json', json.dumps(options))
        if 'transmission' in downloads and not options.get('transmission_defaults_applied'):
            seed_transmission_defaults(TransmissionRpc(download_address, ports['transmission'], 'admin', transmission_password))
            options['transmission_defaults_applied'] = True
            write_secret(state / 'install-options.json', json.dumps(options))
        seed_indexers = not options.get('sonarr_indexer_defaults_applied')
        seed_radarr_indexers = options['radarr_indexer_defaults_pending']
        seed_prowlarr = bool(prowlarr_key) and not options.get('prowlarr_indexer_defaults_applied')
        configure_apps(arr_keys, ports, media, prowlarr_key, transmission_password, seed_indexers=seed_indexers,
                       seed_radarr_indexers=seed_radarr_indexers, seed_prowlarr=seed_prowlarr, arr_address=arr_address,
                       downloads_behind_vpn=download_network == 'vpn', prowlarr_address=download_address)
        if seed_prowlarr:
            options['prowlarr_indexer_defaults_applied'] = True
        if seed_indexers or seed_radarr_indexers or seed_prowlarr:
            options.update(sonarr_indexer_defaults_applied=True, radarr_indexer_defaults_pending=False)
            write_secret(state / 'install-options.json', json.dumps(options))
        compose_step(f'{step + 1}. Building and starting your dashboard…', ['build', '--pull', '--quiet', 'dashboard'])
        compose_step('   Starting the dashboard…', ['up', '-d', '--wait', '--wait-timeout', '90', 'dashboard'])
        verify = '''
import server
for app in ('sonarr', 'radarr'):
    result = server.arr_data(app)
    if not result['configured'] or result.get('error'):
        raise SystemExit(f'{app} verification failed: {result.get("error", "key unavailable")}')
    print(f'{app} monitoring connected', flush=True)
'''
        compose_step(f'{step + 2}. Checking that the dashboard can read both libraries…', ['exec', '-T', 'dashboard', 'python', '-B', '-c', verify])
    done = {'update': 'Update complete', 'reset': 'Fresh installation complete'}.get(mode, 'Setup complete')
    print(f'\n{done} · Sonarr and Radarr are connected.\n')
    print('Open your dashboard')
    print('───────────────────')
    print(f'Address   http://{bind}:{ports["dashboard"]}')
    print('Username  admin')
    if (auth_dir / 'password-hash').is_file():
        print('Password  Use the password you set on first sign-in.')
    else:
        print('Password  Run this on the VM to show your temporary password:')
        print(f'          cat {shlex.quote(str(password))}')
        print('          Sign in with it, then choose your own password on the setup screen.')
    names = ['Sonarr', 'Radarr', *(name for app, name in (('prowlarr', 'Prowlarr'), ('transmission', 'Transmission'),
                                                          ('bazarr', 'Bazarr')) if app in enabled)]
    listed = ', '.join(names[:-1]) + ' and ' + names[-1]
    print('\nApp sign-in')
    if personal_password_set:
        print(f'{listed} use admin and your dashboard password. Changing it in the dashboard updates every app.')
        if mode == 'update':
            print('Apps added before this version keep their old password until you next change your dashboard password.')
    else:
        print(f'{listed} use admin and the same temporary password. Setting your own password updates every app.')
    if vpn:
        print(f'NordVPN exit: {plan["vpn_city"] or plan["vpn_country"] or "Automatic"}')
    if downloads:
        print(f'Download network: {"VPN" if download_network == "vpn" else "normal internet connection"}')
    for app in app_dirs:
        address = bind
        print(f'{app.capitalize():13} http://{address}:{ports[app]}')
    if 'prowlarr' in enabled:
        print('Manage indexers in Prowlarr. The installer connected both library apps.')
    if 'flaresolverr' in enabled:
        flare_host = '127.0.0.1' if download_network == 'vpn' else 'flaresolverr'
        print(f'FlareSolverr proxy address inside Prowlarr: http://{flare_host}:8191')


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', type=Path,
                        help='Where app data is stored. Defaults to ~/.local/state/martinflix for new installs.')
    parser.add_argument('--dashboard-bind',
                        help='IPv4 address for the dashboard, Sonarr, and Radarr. Defaults to the saved address, '
                             'or 127.0.0.1 for a new installation.')
    parser.add_argument('--dashboard-port', type=int)
    parser.add_argument('--media-path', help='Existing writable media directory containing or receiving TV Shows, Movies, and downloads.')
    parser.add_argument('--enable-downloads', action='store_true',
                        help='Install Gluetun, Transmission, and Prowlarr. Requires --media-path. Defaults to VPN routing.')
    parser.add_argument('--enable-vpn', action='store_true', help='Install the NordVPN gateway.')
    parser.add_argument('--vpn-provider', choices=['nordvpn'], default='nordvpn')
    parser.add_argument('--vpn-country', choices=['Czech Republic', 'United Kingdom', 'Netherlands', 'Germany', 'United States'])
    parser.add_argument('--vpn-city', choices=['Prague'])
    parser.add_argument('--download-network', choices=['vpn', 'direct'])
    parser.add_argument('--download-client', choices=['transmission'])
    parser.add_argument('--enable-prowlarr', action='store_true')
    parser.add_argument('--enable-flaresolverr', action='store_true')
    parser.add_argument('--enable-bazarr', action='store_true',
                        help='Install Bazarr for subtitles. Requires --media-path. Follows the download network route.')
    parser.add_argument('--bazarr-port', type=int)
    parser.add_argument('--prowlarr-port', type=int)
    parser.add_argument('--transmission-port', type=int)
    parser.add_argument('--transmission-peer-port', type=int,
                        help='Port Transmission accepts peers on. Defaults to 51155. Published only for direct downloads.')
    parser.add_argument('--sonarr-port', type=int)
    parser.add_argument('--radarr-port', type=int)
    parser.add_argument('--check', action='store_true',
                        help='Check prerequisites and options without creating files, stopping, or starting containers.')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--update', action='store_true',
                      help='Update an existing installation using its saved settings. Fails if no installation is found.')
    mode.add_argument('--reset', action='store_true',
                      help='Archive this installation’s app data and credentials, then start fresh. Requires confirmation.')
    return parser


def main():
    parser = build_parser()
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
        with tempfile.TemporaryDirectory(prefix='martinflix-compose-') as directory:
            install(args, Path(directory) / 'downloads.json')
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        if isinstance(exc, subprocess.CalledProcessError):
            parser.exit(1, 'Could not check the Compose project. Run docker compose version and docker info, then try again.\n')
        if isinstance(exc, subprocess.TimeoutExpired):
            parser.exit(1, 'Docker did not respond. Check that Docker is running, then try again.\n')
        parser.exit(1, f'{exc}\n')


if __name__ == '__main__':
    main()
