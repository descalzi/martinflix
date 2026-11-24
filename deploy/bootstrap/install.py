#!/usr/bin/env python3
"""Set up, update or reset MartinFlix on this machine.

This runs inside the MartinFlix image, started by its launcher (launch.py), which gives it this machine's folders at
the same paths, this machine's network, and the Docker socket. Start it with docker run, as the launcher explains.
"""
import argparse
import datetime
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
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'controller'))
from controller import hash_pin, valid_pin  # noqa: E402
import restore  # noqa: E402
from configure import (AppApi, BazarrApi, TransmissionRpc, adopt_legacy, configure_apps, configure_bazarr, ensure_arr_login,
                       ensure_bazarr_login, seed_transmission_defaults)

COMPOSE_FILE = Path(__file__).resolve().with_name('compose.yaml')
APP_DIR = COMPOSE_FILE.parents[2]
# This machine's /dev as setup sees it. The launcher mounts it read-only at this path.
HOST_DEV = Path(os.environ.get('MARTINFLIX_HOST_DEV', '/dev'))
IMAGE_REPO = os.environ.get('MARTINFLIX_IMAGE_REPO', 'ghcr.io/descalzi/martinflix')
VERSION = os.environ.get('MARTINFLIX_VERSION', 'dev')
REVISION = os.environ.get('MARTINFLIX_REVISION', '')
# The image setup runs from, by its version. The dashboard and controller run from the same image.
OWN_IMAGE = f'{IMAGE_REPO}:{VERSION}'
CHANNELS = ('edge', 'stable')
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


def default_state_directories():
    state_home = Path.home() / '.local/state'
    return state_home / 'martinflix', state_home / 'martinflix-first-run'


def resolve_state_directory(requested):
    if requested is not None:
        return requested.expanduser().resolve()
    current, legacy = default_state_directories()
    if has_installation(legacy):
        if has_installation(current):
            raise ValueError('Two MartinFlix installations were found. Choose one with --state-dir.')
        return legacy.resolve()
    return current.resolve()


CONTAINER_PREFIX = 'martinflix'


def container_prefix(project, previous):
    """Containers are named <prefix>-<app>: plain martinflix, unless another installation on this machine already
    has those names, when this one uses its project name so the two never clash. Decided once, then kept."""
    saved = previous.get('container_prefix')
    if saved in (CONTAINER_PREFIX, project):
        return saved
    try:
        listed = subprocess.run(['docker', 'ps', '-a', '--filter', f'name=^{CONTAINER_PREFIX}-(dashboard|controller|sonarr)$',
                                 '--format', '{{.Label "com.docker.compose.project"}}'],
                                capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return project
    if listed.returncode:
        return project
    owners = {line.strip() for line in listed.stdout.splitlines() if line.strip()}
    return CONTAINER_PREFIX if owners <= {project} else project


def project_exists(project):
    try:
        listed = subprocess.run(['docker', 'ps', '-aq', '--filter', f'label=com.docker.compose.project={project}'],
                                capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return True
    return listed.returncode != 0 or bool(listed.stdout.strip())


def compose_project(state, previous, fresh=False):
    suffix = hashlib.sha256(os.fsencode(state)).hexdigest()[:16]
    current = f'martinflix-{suffix}'
    legacy = f'martinflix-first-run-{suffix}'
    if fresh:
        return current
    project = previous.get('project')
    if project is None:
        # Installs from before setup saved its options ran under the older name. Anything else, such as a first run
        # that stopped part-way, takes the current one.
        return legacy if has_installation(state) and project_exists(legacy) else current
    if project not in (current, legacy):
        raise ValueError('The saved Docker project does not match this state directory. Use the original --state-dir.')
    return project


def media_root(value, state):
    path = Path(value).expanduser().resolve()
    if path.is_relative_to(state) or state.is_relative_to(path):
        raise ValueError('Choose a dedicated media directory separate from the installer state.')
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


NORDVPN_CREDENTIALS_PAGE = 'https://my.nordaccount.com/dashboard/nordvpn/manual-configuration/'
NORDVPN_CREDENTIALS_HELP = f'''
NordVPN login
─────────────
Gluetun signs in with your NordVPN service credentials. These are not your Nord Account email and password.
To find them:
  1. Sign in at {NORDVPN_CREDENTIALS_PAGE}
     (or Nord Account → NordVPN → Advanced settings → Set up NordVPN manually).
  2. Enter the verification code NordVPN emails you.
  3. Open the Service credentials tab and copy the username and password.
'''


def prepare_vpn_secrets(path):
    """Ask for any missing NordVPN credentials. Returns True when they changed.

    Gluetun reads its credentials once, from files bound into its container when the container is created. New
    files are not seen by a container that already exists, so callers recreate Gluetun when this returns True.
    """
    private_directory(path)
    credentials = {name: private_secret(path / name) for name in ('openvpn_user', 'openvpn_password')}
    changed = not all(credentials.values())
    if changed:
        if not sys.stdin.isatty():
            raise ValueError('Run setup in a terminal to enter your NordVPN OpenVPN service credentials.')
        print(NORDVPN_CREDENTIALS_HELP, flush=True)
        # The username shows as you type so typos are visible; the password stays hidden, as with sudo.
        if not credentials['openvpn_user']:
            credentials['openvpn_user'] = input('NordVPN service username: ').strip()
        if not credentials['openvpn_password']:
            credentials['openvpn_password'] = getpass.getpass(
                'NordVPN service password (hidden as you type or paste): ').strip()
        if not all(credentials.values()):
            raise ValueError('Both NordVPN service credentials are needed.')
    for name, value in credentials.items():
        write_secret(path / name, value, trailing_newline=False)
    return changed


CONTROL_PIN_HELP = """
The dashboard can restart and update apps. Choose a PIN of 4 to 8 digits: the dashboard asks for it before each
restart or update, and too many wrong tries lock these actions for five minutes. Only a hash of the PIN is stored."""


def prepare_control_pin(path, change=False):
    """Ask for the dashboard PIN when none is set, or when the admin asked to change it."""
    private_directory(path)
    pin_file = path / 'pin-hash'
    if pin_file.is_file() and not change:
        return
    if not sys.stdin.isatty():
        raise ValueError('Run setup in a terminal to choose the dashboard PIN.')
    print(CONTROL_PIN_HELP, flush=True)
    for _ in range(3):
        pin = getpass.getpass('Dashboard PIN (4 to 8 digits, hidden as you type): ').strip()
        if not valid_pin(pin):
            print('Use 4 to 8 digits, with no spaces or letters.', flush=True)
            continue
        if getpass.getpass('Type the PIN again: ').strip() != pin:
            print('The two PINs did not match.', flush=True)
            continue
        write_secret(pin_file, hash_pin(pin))
        return
    raise ValueError('No PIN was set. Rerun setup to choose one.')


TMDB_KEY = re.compile(r'[A-Za-z0-9._-]{20,1024}')
PLEX_TOKEN = re.compile(r'[A-Za-z0-9_-]{10,200}')
VPN_CREDENTIAL = re.compile(r'[^\s]{1,200}')


def read_settings_file(path):
    """Secrets the dashboard sent along with changed settings, through the controller. The file is read once and
    removed straight away, whatever is in it."""
    if path is None:
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise ValueError('The settings from the dashboard could not be read. Save them again.') from exc
    finally:
        path.unlink(missing_ok=True)
    if not isinstance(data, dict) or not all(isinstance(value, str) for value in data.values()):
        raise ValueError('The settings from the dashboard are invalid. Save them again.')
    for name, pattern, problem in (('tmdbKey', TMDB_KEY, 'That doesn’t look like a TMDB API key.'),
                                   ('plexToken', PLEX_TOKEN, 'That doesn’t look like a Plex token.'),
                                   ('vpnUser', VPN_CREDENTIAL, 'Enter the NordVPN service username.'),
                                   ('vpnPassword', VPN_CREDENTIAL, 'Enter the NordVPN service password.')):
        value = data.get(name)
        if value and not pattern.fullmatch(value):
            raise ValueError(problem)
    if bool(data.get('vpnUser')) != bool(data.get('vpnPassword')):
        raise ValueError('Enter both the NordVPN service username and password.')
    return {key: value for key, value in data.items() if key in ('tmdbKey', 'plexToken', 'vpnUser', 'vpnPassword')}


def apply_settings_secrets(given, secret_dir, vpn_dir):
    """Store the secrets sent from the dashboard. Returns True when the VPN credentials changed."""
    for name, path, kept, removed in (('tmdbKey', secret_dir / 'tmdb-key', 'Discover will use your TMDB key.',
                                       'Discover will use MartinFlix’s TMDB key.'),
                                      ('plexToken', secret_dir / 'plex-token', 'Plex token saved.', 'Plex disconnected.')):
        if name in given:
            if given[name]:
                write_secret(path, given[name])
            else:
                path.unlink(missing_ok=True)
            print(f'   {kept if given[name] else removed}', flush=True)
    if given.get('vpnUser'):
        private_directory(vpn_dir)
        write_secret(vpn_dir / 'openvpn_user', given['vpnUser'], trailing_newline=False)
        write_secret(vpn_dir / 'openvpn_password', given['vpnPassword'], trailing_newline=False)
        print('   New NordVPN service credentials saved.', flush=True)
        return True
    return False


TMDB_KEY_HELP = """
Discover uses MartinFlix's own TMDB API key unless you give it yours. To get one, sign in at
https://www.themoviedb.org, open Settings → API, request a Developer key, and copy the "API Key" (or the longer
"API Read Access Token"). Leave it blank to go back to MartinFlix's key."""


def prepare_tmdb_key(path):
    """Ask for the admin's own TMDB key. A blank answer removes it, so Discover goes back to MartinFlix's key."""
    if not sys.stdin.isatty():
        raise ValueError('Run setup in a terminal to enter the TMDB API key.')
    print(TMDB_KEY_HELP, flush=True)
    key = getpass.getpass('TMDB API key or read access token (hidden as you type or paste): ').strip()
    if key and not TMDB_KEY.fullmatch(key):
        raise ValueError('That doesn’t look like a TMDB API key. Copy it again from TMDB’s API settings.')
    if key:
        write_secret(path, key)
    else:
        path.unlink(missing_ok=True)
    print('Discover will use your TMDB key.' if key else 'Discover will use MartinFlix’s TMDB key.', flush=True)


PLEX_TOKEN_HELP = """
Live reads what's playing and the watch history straight from Plex, with a Plex token. To find yours, open Plex Web,
choose any film or episode, open ⋯ → Get Info → View XML, and copy the value after "X-Plex-Token=" in the address
bar. Leave it blank to disconnect Plex."""


def prepare_plex_token(path):
    if not sys.stdin.isatty():
        raise ValueError('Run setup in a terminal to enter the Plex token.')
    print(PLEX_TOKEN_HELP, flush=True)
    token = getpass.getpass('Plex token (hidden as you type or paste): ').strip()
    if token and not PLEX_TOKEN.fullmatch(token):
        raise ValueError('That doesn’t look like a Plex token. Copy it again from the X-Plex-Token part of the address.')
    if token:
        write_secret(path, token)
    else:
        path.unlink(missing_ok=True)
    print('Plex is connected.' if token else 'Plex is disconnected.', flush=True)


# Registry answers that mean "try again shortly", including GitHub's shared limit for every LinuxServer image.
TRANSIENT_PULL = re.compile(r'toomanyrequests|too many requests|i/o timeout|TLS handshake timeout|connection reset|'
                            r'unexpected EOF|context deadline exceeded|50[234] (?:Bad Gateway|Service Unavailable|Gateway)',
                            re.IGNORECASE)
PULL_PAUSES = (10, 30, 60)
PULL_WORKERS = 4
APP_LABELS = {'gluetun': 'Gluetun (VPN)', 'flaresolverr': 'FlareSolverr'}
# A layer line from docker pull, such as "3f4ca61aafcd: Pull complete".
LAYER_LINE = re.compile(r'^([0-9a-f]{12,64}): (.+)$')


class ImagePull:
    """One image's progress, shown as a line while setup downloads."""

    def __init__(self, service, image):
        self.service = service
        self.image = image
        self.label = APP_LABELS.get(service, service.replace('-', ' ').title())
        self.state = 'waiting'
        self.note = ''
        self.layers = {}
        self.output = ''

    def line(self, width=72):
        done = sum(1 for status in self.layers.values() if status in ('Pull complete', 'Already exists'))
        text = {
            'waiting': 'waiting',
            'checking': 'checking for updates…',
            'downloading': f'downloading ({done} of {len(self.layers)} layers)',
            'updated': 'updated',
            'downloaded': 'downloaded',
            'current': 'up to date',
            'kept': 'kept the version already here (registry busy)',
            'failed': 'could not download',
        }.get(self.state, self.note)
        return f'   {self.label:<20} {text}'[:width]

    @property
    def finished(self):
        return self.state in ('updated', 'downloaded', 'current', 'kept', 'failed')


def image_id(image, environment, run=subprocess.run):
    result = run(['docker', 'image', 'inspect', '--format', '{{.Id}}', image], env=environment, capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else None


def pull_one(pull, environment, sleep=time.sleep, popen=subprocess.Popen, run=subprocess.run):
    """Download one image, retrying when the registry is busy, and record whether it changed."""
    before = image_id(pull.image, environment, run)
    for pause in (*PULL_PAUSES, None):
        pull.state, pull.layers = 'checking', {}
        lines = []
        process = popen(['docker', 'pull', pull.image], env=environment, stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT, text=True)
        for line in process.stdout:
            lines.append(line)
            if match := LAYER_LINE.match(line.strip()):
                pull.layers[match.group(1)] = match.group(2)
                if match.group(2) not in ('Already exists',):
                    pull.state = 'downloading'
        if process.wait() == 0:
            after = image_id(pull.image, environment, run)
            pull.state = 'downloaded' if before is None else 'current' if after == before else 'updated'
            return
        pull.output = ''.join(lines).strip()[-4000:]
        if pause is None or not TRANSIENT_PULL.search(pull.output):
            break
        pull.state, pull.note = 'retrying', f'registry busy, trying again in {pause} seconds'
        sleep(pause)
    # An update can carry on with the version already here; a first download cannot.
    pull.state = 'kept' if before else 'failed'


def pull_images(compose, environment, services, sleep=time.sleep, popen=subprocess.Popen, run=subprocess.run,
                output=None):
    """Download the newest image for each service, showing each app's progress. Returns None when setup can carry on,
    or the Docker output to show when it must stop."""
    output = output or sys.stdout
    listed = run(compose + ['config', '--format', 'json'], env=environment, capture_output=True, text=True)
    if listed.returncode != 0:
        return (listed.stderr or listed.stdout or '').strip()[-4000:] or 'Compose could not read the stack.'
    specs = json.loads(listed.stdout).get('services', {})
    pulls, seen = [], set()
    for service in services:
        image = (specs.get(service) or {}).get('image')
        if image and image not in seen:
            seen.add(image)
            pulls.append(ImagePull(service, image))
    # Downloads run in parallel and nothing starts until all are done, so the list is alphabetical, for reading.
    pulls.sort(key=lambda pull: pull.label.casefold())
    live = output.isatty()
    width = shutil.get_terminal_size((80, 20)).columns - 1
    shown = 0
    reported = set()

    def draw(final=False):
        nonlocal shown
        if live:
            if shown:
                output.write(f'\x1b[{shown}F')
            for pull in pulls:
                output.write('\x1b[2K' + pull.line(width) + '\n')
            shown = len(pulls)
        else:
            for pull in pulls:
                if pull.finished and pull.service not in reported:
                    reported.add(pull.service)
                    output.write(pull.line(width) + '\n')
        output.flush()

    with ThreadPoolExecutor(max_workers=PULL_WORKERS) as pool:
        jobs = [pool.submit(pull_one, pull, environment, sleep, popen, run) for pull in pulls]
        while not all(job.done() for job in jobs):
            draw()
            time.sleep(0.25 if live else 1)
        for job in jobs:
            job.result()
    draw(final=True)
    if kept := [pull.label for pull in pulls if pull.state == 'kept']:
        output.write(f'   The image registry stayed busy, so setup is using the versions of {", ".join(kept)} already on '
                     'this VM. Rerun setup later to update them.\n')
    failed = [pull for pull in pulls if pull.state == 'failed']
    pull_images.changed = {pull.service for pull in pulls if pull.state in ('updated', 'downloaded')}
    return '\n\n'.join(f'{pull.label}:\n{pull.output}' for pull in failed) if failed else None


def app_label(service):
    return APP_LABELS.get(service, service.replace('-', ' ').title())


def container_states(project, environment, run=subprocess.run):
    """Each service's container ID, whether it is running, and when it started."""
    listed = run(['docker', 'ps', '-aq', '--filter', f'label=com.docker.compose.project={project}'],
                 env=environment, capture_output=True, text=True)
    ids = listed.stdout.split() if listed.returncode == 0 else []
    if not ids:
        return {}
    inspected = run(['docker', 'inspect', *ids], env=environment, capture_output=True, text=True)
    try:
        items = json.loads(inspected.stdout) if inspected.returncode == 0 else []
    except ValueError:
        items = []
    return {item['Config']['Labels'].get('com.docker.compose.service'):
            (item.get('Id'), bool(item['State'].get('Running')), item['State'].get('StartedAt')) for item in items}


HEALTH = re.compile(r'\((healthy|unhealthy|health: starting)\)')


def live_states(project, names, environment, run=subprocess.run):
    """Each named service's container state and Docker's status line, such as "Up 9 seconds (health: starting)"."""
    try:
        listed = run(['docker', 'ps', '-a', '--filter', f'label=com.docker.compose.project={project}', '--format',
                      '{{.Label "com.docker.compose.service"}}\t{{.State}}\t{{.Status}}\t{{.ID}}'],
                     env=environment, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return {}
    states = {}
    for line in listed.stdout.splitlines() if listed.returncode == 0 else []:
        service, state, status, container = (line.split('\t') + ['', '', ''])[:4]
        if service in names:
            states[service] = (state, status, container)
    return states


def describe_state(found, follows=None):
    """What an app is doing while Compose waits for it, in words. Returns (text, finished)."""
    if found is None:
        return 'waiting to be created', False
    state, status = found[:2]
    if state == 'created':
        return (f'waiting for {app_label(follows)}' if follows else 'waiting to start'), False
    if state == 'restarting':
        return 'restarting after a problem…', False
    if state in ('exited', 'dead'):
        code = re.search(r'Exited \((\d+)\)', status)
        return f'stopped{f" (exit code {code.group(1)})" if code else ""}', True
    health = HEALTH.search(status)
    if not health:
        return 'running', True
    return {'healthy': ('ready', True), 'unhealthy': ('running, but its health check is failing', False),
            'health: starting': ('starting…', False)}[health.group(1)]


class StepProgress:
    """Each app's progress while a step waits for it to be healthy, so a long wait never looks frozen. On a
    terminal the lines update in place with the time waited. Otherwise, as in the dashboard's updates, only an app
    that is taking a while gets a line, now and then: the step's outcome for each app follows anyway."""

    # Without a terminal: quiet for this long, then a line per waiting app at most this often, or when it changes.
    QUIET_SECONDS = 15
    REPEAT_SECONDS = 30

    def __init__(self, project, names, environment, follows=None, output=None, run=subprocess.run,
                 clock=time.monotonic):
        self.project, self.names, self.environment = project, sorted(names, key=lambda item: app_label(item).casefold()), environment
        self.follows, self.output, self.run, self.clock = follows, output or sys.stdout, run, clock
        self.live = self.output.isatty()
        self.started = clock()
        self.shown = 0
        self.reported = {}
        # Containers as they were before the step. One still there may be about to be replaced, so it only "runs".
        self.before = {name: found[2] for name, found in live_states(project, self.names, environment, run).items()
                       if len(found) > 2}

    def update(self):
        waited = int(self.clock() - self.started)
        # A container being replaced still looks ready for a moment, so the first few seconds show nothing.
        if waited < 3 and not self.shown and not self.reported:
            return
        states = live_states(self.project, self.names, self.environment, self.run)
        lines = []
        for name in self.names:
            found = states.get(name)
            text, finished = describe_state(found, self.follows if name != self.follows else None)
            if found and len(found) > 2 and found[2] == self.before.get(name) and text == 'ready':
                text = 'running'
            if self.live:
                lines.append(f'   {app_label(name):<20} {text}' + ('' if finished else f' {waited // 60}:{waited % 60:02d}'))
            elif not finished and waited >= self.QUIET_SECONDS:
                last = self.reported.get(name)
                if last is None or last[0] != text or waited - last[1] >= self.REPEAT_SECONDS:
                    self.reported[name] = (text, waited)
                    lines.append(f'   {app_label(name):<20} {text} {waited // 60}:{waited % 60:02d}')
        if self.live:
            self.clear()
            width = shutil.get_terminal_size((80, 20)).columns - 1
            self.output.write(''.join('\x1b[2K' + line[:width] + '\n' for line in lines))
            self.shown = len(lines)
        else:
            self.output.write(''.join(line + '\n' for line in lines))
        self.output.flush()

    def clear(self):
        """Remove the live lines, so the step's outcome for each app is printed in their place."""
        if self.live and self.shown:
            self.output.write(f'\x1b[{self.shown}F\x1b[0J')
            self.output.flush()
            self.shown = 0


def restart_outcome(before, after, new_image):
    """What a step did to one app, in words, from its container before and after."""
    if after is None:
        return None
    if before is None or not before[1]:
        return 'started'
    if before[0] != after[0]:
        return 'restarted with the new image' if new_image else 'restarted with new settings'
    if before[2] != after[2]:
        return 'restarted'
    return 'running, unchanged'


DEFAULT_BACKUP_DIR = Path.home() / '.local/share/martinflix/backups'


def backup_folder(requested, previous):
    """Where backups go. A chosen folder must already exist, as a mounted NAS share would; setup never creates a
    mount point. Each machine writes into its own subfolder, so machines sharing a folder never mix backups."""
    folder = Path(requested or previous or DEFAULT_BACKUP_DIR).expanduser()
    if not folder.is_absolute():
        raise ValueError('Use an absolute path for --backup-dir, such as /mnt/martinflix/Backups.')
    if folder != DEFAULT_BACKUP_DIR and not folder.is_dir():
        raise ValueError(f'The backup folder {folder} does not exist. If it is on a NAS, check that the share is '
                         'mounted, then rerun setup.')
    return folder


PLEX_PORT = 32400
APP_IMAGES = {'sonarr': 'lscr.io/linuxserver/sonarr', 'radarr': 'lscr.io/linuxserver/radarr',
              'prowlarr': 'lscr.io/linuxserver/prowlarr', 'bazarr': 'lscr.io/linuxserver/bazarr'}
IMAGE_TAG = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}')
PLEX_CLAIM_HELP = """
Link the new Plex server to your Plex account with a claim code: sign in at https://plex.tv/claim and copy the
code it shows. It only lasts four minutes, so get it now. Leave it blank to claim the server later in Plex Web."""
PLEX_PREFERENCES = Path('Library/Application Support/Plex Media Server/Preferences.xml')
# A new Plex server's settings, taken from the reference server. Its identity, account and remote-access port are
# deliberately absent: each server has its own.
PLEX_SETTINGS = {
    # Maintenance runs overnight: local media, periodic metadata and TV guide refreshes.
    'ButlerStartHour': '2', 'ButlerEndHour': '9', 'ButlerTaskRefreshLocalMedia': '1',
    'ButlerTaskRefreshPeriodicMetadata': '1', 'ButlerTaskRefreshEpgGuides': '1',
    # New files appear as soon as Sonarr and Radarr move them in; a low-priority full scan runs once a day too.
    'FSEventLibraryUpdatesEnabled': '1', 'FSEventLibraryPartialScanEnabled': '1', 'ScannerLowPriority': '1',
    'ScheduledLibraryUpdatesEnabled': '1', 'ScheduledLibraryUpdateInterval': '86400',
    # Intro, credits and voice-activity markers are found during maintenance, not on import.
    'GenerateIntroMarkerBehavior': 'scheduled', 'GenerateCreditsMarkerBehavior': 'scheduled',
    'GenerateVADBehavior': 'scheduled',
    'LibraryVideoPlayedAtBehaviour': '2', 'autoEmptyTrash': '1',
    # Cinema trailers before films (a Plex Pass feature).
    'CinemaTrailersFromLibrary': '1', 'CinemaTrailersFromTheater': '1', 'CinemaTrailersFromBluRay': '1',
    'CinemaTrailersType': '0',
    'DlnaEnabled': '0', 'PushNotificationsEnabled': '0', 'sendCrashReports': '0', 'logDebug': '0', 'LogVerbose': '0',
    # Remote access with automatic port mapping (UPnP on the router), over IPv4 and IPv6 where the network has it.
    'PublishServerOnPlexOnlineKey': '1', 'ManualPortMappingMode': '0', 'IPNetworkType': 'dualstack',
}
PLEX_DEFAULT_NAME = 'martinflix'
PREROLL_DIR = APP_DIR / 'assets' / 'preroll'


def sync_prerolls(target, source=PREROLL_DIR):
    """Plex plays pre-rolls from the state folder: the clips in this image, added, replaced or removed to match."""
    private_directory(target)
    wanted = {clip.name: clip for clip in source.glob('*.mp4')}
    for clip in target.glob('*.mp4'):
        if clip.name not in wanted:
            clip.unlink()
    for name, clip in wanted.items():
        copy = target / name
        if not copy.is_file() or copy.stat().st_size != clip.stat().st_size or \
                hashlib.sha256(copy.read_bytes()).digest() != hashlib.sha256(clip.read_bytes()).digest():
            shutil.copyfile(clip, copy)


def preroll_list(folder=PREROLL_DIR):
    """Plex's pre-roll setting: every clip in the folder, as Plex sees it. ';' plays one at random per film."""
    return ';'.join(f'/preroll/{clip.name}' for clip in sorted(folder.glob('*.mp4')))


def plex_tracked_settings(name, folder=PREROLL_DIR):
    """Settings setup keeps in step on every run: the server's name and the pre-roll clips."""
    return {'FriendlyName': name, 'CinemaTrailersPrerollID': preroll_list(folder)}
# Libraries: name, type, agent, scanner, folder, and the advanced settings that differ from Plex's defaults.
PLEX_LIBRARIES = (
    ('Films', 'movie', 'tv.plex.agents.movie', 'Plex Movie', '/data/Movies',
     {'country': 'GB', 'useLocalAssets': 'false', 'includeAdultContent': 'true', 'autoCollectionThreshold': '2'}),
    ('TV programmes', 'show', 'tv.plex.agents.series', 'Plex TV Series', '/data/TV Shows',
     {'showOrdering': 'aired', 'useLocalAssets': 'false', 'includeExtrasWithLocalizedSubtitles': 'true'}),
)


def plex_token(config):
    """The account token Plex saves once the server is claimed. Empty until then."""
    try:
        return ElementTree.parse(config / PLEX_PREFERENCES).getroot().get('PlexOnlineToken', '').strip()
    except (FileNotFoundError, ElementTree.ParseError):
        return ''


def plex_claim_code():
    if not sys.stdin.isatty():
        return ''
    print(PLEX_CLAIM_HELP, flush=True)
    code = input('Plex claim code (starts with claim-): ').strip()
    if code and not re.fullmatch(r'claim-[A-Za-z0-9_-]{10,60}', code):
        raise ValueError('That doesn’t look like a Plex claim code. Get a new one at https://plex.tv/claim.')
    return code


def wait_for_plex_token(config, seconds=60):
    for _ in range(seconds):
        if token := plex_token(config):
            return token
        time.sleep(1)
    return ''


# Plex is on this VM, so its requests never go through a proxy.
LOCAL_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def wait_for_plex_ready(token, seconds=180, opener=LOCAL_OPENER.open, sleep=time.sleep):
    """Plex answers its identity check within seconds of starting, but other requests get 503 until it has finished
    setting up (its database, and linking to the account on first start). Returns True once it really answers."""
    waited, told = 0, False
    while True:
        try:
            plex_request(token, 'GET', '/library/sections', opener=opener)
            return True
        except urllib.error.HTTPError as exc:
            exc.close()
            if exc.code != 503 or waited >= seconds:
                return False
        except OSError:
            if waited >= seconds:
                return False
        if not told:
            print('   Plex is still starting up; waiting for it to finish…', flush=True)
            told = True
        sleep(5)
        waited += 5


def plex_request(token, method, path, params=None, opener=LOCAL_OPENER.open):
    query = f'?{urllib.parse.urlencode(params)}' if params else ''
    request = urllib.request.Request(f'http://127.0.0.1:{PLEX_PORT}{path}{query}', method=method,
                                     headers={'X-Plex-Token': token, 'Accept': 'application/json'})
    with opener(request, timeout=20) as response:
        body = response.read()
    return json.loads(body) if body.strip().startswith(b'{') else {}


def apply_plex_settings(token, opener=LOCAL_OPENER.open):
    plex_request(token, 'PUT', '/:/prefs', PLEX_SETTINGS, opener=opener)
    print(f'   Plex: {len(PLEX_SETTINGS)} server settings applied.', flush=True)


def seed_plex_libraries(token, language='en-US', opener=LOCAL_OPENER.open):
    """Add the Films and TV programmes libraries over the media folders, unless Plex already has libraries."""
    sections = plex_request(token, 'GET', '/library/sections', opener=opener).get('MediaContainer', {}).get('Directory') or []
    if sections:
        print('   Plex: libraries already exist; left as they are.', flush=True)
        return
    for name, kind, agent, scanner, location, settings in PLEX_LIBRARIES:
        plex_request(token, 'POST', '/library/sections', {
            'name': name, 'type': kind, 'agent': agent, 'scanner': scanner, 'language': language, 'location': location,
            **{f'prefs[{key}]': value for key, value in settings.items()}}, opener=opener)
        print(f'   Plex: {name} library added.', flush=True)


def docker_socket():
    """The Docker socket the controller mounts, with its group, or None when Docker is reached over TCP or SSH.
    The launcher gives setup this machine's socket at the usual path and says where it is on this machine."""
    host = os.environ.get('DOCKER_HOST', '')
    if not host:
        try:
            host = subprocess.run(['docker', 'context', 'inspect', '--format', '{{.Endpoints.docker.Host}}'],
                                  capture_output=True, text=True, timeout=20).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return None
    if not host.startswith('unix://'):
        return None
    path = Path(host[len('unix://'):])
    try:
        return Path(os.environ.get('MARTINFLIX_DOCKER_SOCKET') or path), path.stat().st_gid
    except OSError:
        return None


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
    password = getpass.getpass('OpenSubtitles.com password (hidden as you type or paste): ')
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


VPN_PROBLEMS = (
    ('no server found', 'No NordVPN server matches the chosen exit location. Rerun with a different --vpn-country '
                        'or --vpn-city.'),
    ('TLS key negotiation failed', 'Gluetun reached no NordVPN server. If the server-list refresh above failed, the '
                                   'server may be out of service; rerun setup to try again. If it keeps failing, your '
                                   'network may block VPN traffic over UDP: rerun with --vpn-protocol tcp.'),
)


def refresh_vpn_servers(compose, environment, data_dir):
    """Fetch the provider's current server list, so Gluetun never picks a server its image only remembers."""
    private_directory(data_dir)
    print('   Refreshing the NordVPN server list…', flush=True)
    try:
        result = subprocess.run(compose + ['run', '--rm', '--no-deps', 'gluetun', 'update', '-enduser', '-providers',
                                           'nordvpn'], env=environment, capture_output=True, text=True, timeout=180)
    except (OSError, subprocess.SubprocessError):
        result = None
    if result is None or result.returncode:
        print('   Could not refresh the server list. Gluetun will use the list built into its image.', flush=True)


def diagnose_vpn(compose, environment, secrets_dir):
    """Show Gluetun's own log after a failed start, and act on the problems it can name."""
    try:
        result = subprocess.run(compose + ['logs', '--no-color', '--tail', '40', 'gluetun'], env=environment,
                                capture_output=True, text=True, timeout=30)
        log = result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return
    if log:
        print(f'Gluetun log\n{log}\n', file=sys.stderr)
    rejected = sorted(set(re.findall(r'\[([a-z]{2}\d+\.nordvpn\.com)\][^\n]*\n(?:[^\n]*\n)*?[^\n]*AUTH_FAILED', log)))
    if 'AUTH_FAILED' in log:
        # A rejection does not prove the credentials are wrong: NordVPN also refuses logins over its device limit,
        # and some servers refuse logins others accept. Keep the credentials and explain the options.
        servers = f' ({", ".join(rejected)})' if rejected else ''
        print(f'NordVPN rejected the login{servers}. Setup tried other servers too. Check that:\n'
              f'  • these are the service credentials from {NORDVPN_CREDENTIALS_PAGE}\n'
              '    (Set up NordVPN manually → Service credentials), not your Nord Account email and password;\n'
              '  • the account is not at its device limit. Stop test containers and other VPN connections you no longer use.\n'
              'To enter different credentials, run:\n'
              f'  rm {shlex.quote(str(secrets_dir / "openvpn_user"))} {shlex.quote(str(secrets_dir / "openvpn_password"))}\n'
              'then rerun setup. Otherwise rerun setup as it is to try again.\n', file=sys.stderr)
        return
    for marker, advice in VPN_PROBLEMS:
        if marker.casefold() in log.casefold():
            print(advice + '\n', file=sys.stderr)
            return
    print('Gluetun did not report a connection within two minutes. Check the log above, and that this VM can '
          'reach the internet, then rerun setup.\n', file=sys.stderr)


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
                           MARTINFLIX_DASHBOARD_PORT='8765', MARTINFLIX_SONARR_PORT='8989', MARTINFLIX_RADARR_PORT='7878',
                           MARTINFLIX_IMAGE=OWN_IMAGE, MARTINFLIX_BACKUP_DIR='/nonexistent',
                           MARTINFLIX_BACKUP_HOST_DIR='/nonexistent', MARTINFLIX_NAMES=container_prefix(project, saved_options(state)))
        subprocess.run(['docker', 'compose', '--env-file', '/dev/null', '-p', project, '-f', str(COMPOSE_FILE),
                        'down', '--remove-orphans'], env=environment, timeout=120, check=True)
        archive = Path(tempfile.mkdtemp(prefix='reset-backup-', dir=state))
        managed = ('install-options.json', 'dashboard-auth', 'dashboard-secrets', 'download-secrets',
                   'downloads-compose.json', 'sonarr', 'radarr', 'prowlarr', 'transmission', 'bazarr', 'gluetun',
                   'controller', 'plex', 'plex-gpu-compose.json')
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
    plex = bool(args.enable_plex or previous.get('plex'))
    plex_name = (args.plex_name if args.plex_name is not None else previous.get('plex_name') or PLEX_DEFAULT_NAME).strip()
    if not re.fullmatch(r'[\w][\w .()\'-]{0,49}', plex_name):
        raise ValueError('Use up to 50 letters, numbers, spaces, dots, dashes or brackets for --plex-name.')
    if plex and not media:
        raise ValueError('Plex shows your media directory. Specify it with --media-path.')
    if args.download_network and not downloads:
        raise ValueError('Select a download app before choosing its network route.')
    previous_network = previous.get('download_network') or ('vpn' if previous.get('downloads') else None)
    default_network = 'vpn' if args.enable_downloads or args.enable_vpn or args.vpn_country or args.vpn_city or previous.get('vpn') else 'direct'
    download_network = (args.download_network or previous_network or default_network) if downloads else None
    vpn = bool(args.enable_vpn or args.vpn_country or args.vpn_city or previous.get('vpn') or download_network == 'vpn')
    vpn_protocol = args.vpn_protocol or previous.get('vpn_protocol') or 'udp'
    vpn_country = args.vpn_country or ('' if args.vpn_city else previous.get('vpn_country') or '')
    vpn_city = args.vpn_city or ('' if args.vpn_country else previous.get('vpn_city'))
    if vpn_city is None:
        vpn_city = '' if vpn_country else os.environ.get('MARTINFLIX_VPN_CITY', 'Prague')
    if vpn and not (HOST_DEV / 'net/tun').is_char_device():
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
    options.update(vpn=vpn, vpn_country=vpn_country, vpn_city=vpn_city, vpn_protocol=vpn_protocol,
                   download_network=download_network, plex=plex, plex_name=plex_name)

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
    image_tags = previous.get('image_tags') or {}
    if not isinstance(image_tags, dict) or any(app not in APP_IMAGES or not isinstance(tag, str) or not IMAGE_TAG.fullmatch(tag)
                                               for app, tag in image_tags.items()):
        raise ValueError('The saved image tags are not valid. Remove image_tags from install-options.json and rerun.')
    running = {*APPS, *downloads}
    if tagged := {app: tag for app, tag in image_tags.items() if app in running}:
        # Apps that came from another stack on another tag of the same image (Radarr's nightly, say) stay on it: their
        # databases may be newer than the latest release understands.
        tag_overlay = overlay_file.with_name('image-tags-compose.json')
        tag_overlay.write_text(json.dumps({'services': {app: {'image': f'{APP_IMAGES[app]}:{tag}'}
                                                        for app, tag in tagged.items()}}))
        compose += ['-f', str(tag_overlay)]
    if plex:
        compose += ['-f', str(COMPOSE_FILE.with_name('compose.plex.yaml')), '--profile', 'plex']
        alias = previous.get('plex_media_alias')
        if alias:
            if not isinstance(alias, str) or not re.fullmatch(r'/[A-Za-z0-9._ /-]{1,200}', alias) or '..' in alias \
                    or PurePosixPath(alias).parts[1] in ('config', 'data', 'preroll', 'dev', 'proc', 'sys', 'etc', 'usr'):
                raise ValueError('The saved Plex media folder is not valid. Remove plex_media_alias from '
                                 'install-options.json and rerun.')
            # A library adopted from another stack keeps Plex's folders as they were there, so Plex sees the media at
            # that path too. Its own libraries use /data.
            alias_overlay = overlay_file.with_name('plex-media-compose.json')
            alias_overlay.write_text(json.dumps({'services': {'plex': {'volumes': [{
                'type': 'bind', 'source': str(media), 'target': alias.rstrip('/'), 'read_only': True,
                'bind': {'create_host_path': False}}]}}}))
            compose += ['-f', str(alias_overlay)]
        if (HOST_DEV / 'dri').is_dir():
            # A GPU lets Plex transcode in hardware (with Plex Pass). The image gives Plex access to it.
            gpu_overlay = overlay_file.with_name('plex-gpu-compose.json')
            gpu_overlay.write_text(json.dumps({'services': {'plex': {'devices': ['/dev/dri:/dev/dri']}}}))
            compose += ['-f', str(gpu_overlay)]
    environment = dict(os.environ, MARTINFLIX_STATE_DIR=str(state),
                       MARTINFLIX_UID=str(os.getuid()), MARTINFLIX_GID=str(os.getgid()),
                       MARTINFLIX_DASHBOARD_BIND=str(bind), MARTINFLIX_MEDIA_PATH=str(media) if media else '')
    environment.update(MARTINFLIX_VPN_PROVIDER=args.vpn_provider, MARTINFLIX_VPN_COUNTRY=vpn_country, MARTINFLIX_VPN_CITY=vpn_city,
                       MARTINFLIX_VPN_PROTOCOL=vpn_protocol)
    environment.update({f'MARTINFLIX_{service.upper()}_PORT': str(port) for service, port in ports.items()})
    environment['MARTINFLIX_IMAGE'] = OWN_IMAGE
    options['container_prefix'] = environment['MARTINFLIX_NAMES'] = container_prefix(project, previous)
    channel = args.channel or previous.get('channel') or (os.environ.get('MARTINFLIX_CHANNEL') if
                                                          os.environ.get('MARTINFLIX_CHANNEL') in CHANNELS else 'edge')
    options['channel'] = channel
    region = (args.discover_region or previous.get('discover_region') or 'GB').upper()
    if not re.fullmatch(r'[A-Z]{2}', region):
        raise ValueError('Use a two-letter country code for --discover-region, such as GB or US.')
    backups = backup_folder(args.backup_dir, previous.get('backup_dir'))
    options['backup_dir'] = str(backups)
    environment.update(MARTINFLIX_BACKUP_DIR=str(backups),
                       MARTINFLIX_BACKUP_HOST_DIR=str(backups / socket.gethostname()))
    options['discover_region'] = region
    environment['MARTINFLIX_DISCOVER_REGION'] = region
    options['discover'] = args.discover or previous.get('discover') or 'on'
    environment['MARTINFLIX_DISCOVER'] = options['discover']
    plex_url = args.plex_url if args.plex_url is not None else previous.get('plex_url', '')
    if plex and args.plex_url:
        raise ValueError('This installation runs its own Plex, which the dashboard finds by itself. Leave out --plex-url.')
    if plex_url and not re.fullmatch(r'https?://[A-Za-z0-9.\[\]:-]+(:[0-9]{1,5})?/?', plex_url):
        raise ValueError('Use the Plex server’s address for --plex-url, such as http://192.168.1.10:32400.')
    options['plex_url'] = plex_url.rstrip('/')
    environment['MARTINFLIX_PLEX_URL'] = options['plex_url']
    control_socket = docker_socket()
    if not control_socket:
        raise ValueError('Setup needs Docker on this machine’s local socket. Start it with the docker run command in '
                         'the MartinFlix setup guide, which passes /var/run/docker.sock.')
    environment.update(MARTINFLIX_DOCKER_SOCKET=str(control_socket[0]), MARTINFLIX_DOCKER_GID=str(control_socket[1]))
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
    if plex and not (state / 'plex').exists():
        # Plex uses the host's network, so its port must be free on every address. Ours already holds it on updates.
        bindings.append(('plex', '0.0.0.0', PLEX_PORT))
    if bootstrapping_arr and str(bind) != '127.0.0.1':
        bindings.extend((app, '127.0.0.1', ports[app]) for app in APPS)
    if bootstrapping_prowlarr and str(bind) != '127.0.0.1':
        bindings.extend((app, '127.0.0.1', ports[app]) for app in downloads if app in DOWNLOAD_PORTS)
    return {
        'media': media, 'enabled': enabled, 'downloads': downloads, 'download_network': download_network,
        'vpn': vpn, 'vpn_country': vpn_country, 'vpn_city': vpn_city, 'options': options, 'ports': ports,
        'bind': bind, 'compose': compose, 'environment': environment, 'app_dirs': app_dirs,
        'bootstrapping_arr': bootstrapping_arr, 'bootstrapping_prowlarr': bootstrapping_prowlarr, 'bindings': bindings,
        'control': True, 'change_pin': bool(getattr(args, 'change_pin', False)),
        'set_tmdb_key': bool(getattr(args, 'set_tmdb_key', False)),
        'set_plex_token': bool(getattr(args, 'set_plex_token', False)),
        'plex': plex, 'overlay_dir': overlay_file.parent,
    }


def keep_overlays(compose, state, temporary):
    """Move the Compose files setup generated (downloads, Plex's GPU) from its temporary folder into the state
    folder, and point the command at them there. The controller runs the same command long after setup ends."""
    names = {'downloads.json': 'downloads-compose.json', 'plex-gpu-compose.json': 'plex-gpu-compose.json'}
    for index, item in enumerate(compose):
        path = Path(item)
        if index and compose[index - 1] == '-f' and path.parent == temporary:
            kept = state / names.get(path.name, path.name)
            write_secret(kept, path.read_text())
            compose[index] = str(kept)


def refuse_root():
    if os.getuid() == 0:
        raise ValueError('Run the installer without sudo as the Linux account that will own the stack data.')


def install(args, overlay_file):
    refuse_root()
    # Secrets from the dashboard are read, and their file removed, before anything else can stop setup.
    given = read_settings_file(args.settings_file)
    print(BANNER, flush=True)
    print('1. Checking Docker, storage, and network ports…', flush=True)
    docker_root = preflight_docker()

    state = resolve_state_directory(args.state_dir)
    if state.is_relative_to(COMPOSE_FILE.parents[2]):
        raise ValueError('Choose a state directory outside the repository for application data and secrets.')
    check_directory(state)
    existing = has_installation(state)
    if args.restore and args.reset:
        raise ValueError('A restore replaces the app data anyway. Leave out --reset.')
    if args.keep_plex_identity and not args.restore:
        raise ValueError('--keep-plex-identity only applies with --restore.')
    if args.restore and not args.backup_dir and not saved_options(state).get('backup_dir'):
        # Rebuilding a machine from its backups: keep backing up to the folder they came from.
        args.backup_dir = str(Path(args.restore).resolve().parent.parent)
    if (args.reset or args.update) and not existing:
        flag = '--reset' if args.reset else '--update'
        raise ValueError(f'No installer-managed MartinFlix installation was found at {state}. '
                         f'Check --state-dir, or run setup without {flag} for a new installation.')
    saved = saved_options(state)
    old_project = compose_project(state, saved) if existing else None
    # A reset of an install in the old default folder starts the fresh one under the current default name; the
    # archive of the old installation stays behind in the old folder.
    target = state
    current_default, legacy_default = (path.resolve() for path in default_state_directories())
    if args.reset and args.state_dir is None and state == legacy_default:
        target = current_default
        check_directory(target)
    project = compose_project(target, {} if args.reset else saved, fresh=args.reset)
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
    if (args.reset or not existing) and not args.media_path:
        raise ValueError('Sonarr and Radarr need a media directory to file series and films in. Add --media-path with '
                         'an existing, writable folder, such as a mounted NAS share. Setup creates TV Shows, Movies '
                         'and downloads inside it.')
    plan = plan_installation(args, target, previous, project, overlay_file, fresh_arr=args.reset)
    plan['secrets'] = given
    check_ports(plan['bindings'], published)
    try:
        if shutil.disk_usage(docker_root).free < 2 * 1024**3:
            print('Docker storage has less than 2 GiB free. Free up space if the image download or build fails.', flush=True)
    except OSError:
        pass
    describe_plan(plan, target, args, current if existing else None)
    if target != state:
        print(f'The fresh installation moves to {target}. The archive of the current one stays in {state}.\n', flush=True)
    backup = None
    if args.restore:
        print(f'Checking the backup in {args.restore}: every archive is read against its checksum'
              + (f' ({size})' if (size := restore.backup_size(args.restore)) else '') + '. A large one takes a few minutes…',
              flush=True)
        backup = restore.Backup(args.restore)
        print('Restore\n' + backup.describe() + '\n', flush=True)
        if args.keep_plex_identity and 'plex' in backup.apps:
            # Checked now, before anything is stopped or replaced.
            restore.plex_identity(target / 'plex' / restore.PLEX_PREFERENCES)
        if backup.exported:
            describe_export(backup, plan)
    if args.check:
        if backup:
            print('Preflight passed, and every archive in the backup matches its checksum. Run the same command '
                  'without --check to restore it.')
        elif args.reset:
            print('Preflight passed. Run the same command without --check. Setup will ask you to type RESET '
                  'before it stops and archives the current installation.')
        else:
            print('Preflight passed. Run the same command without --check to start the installation.')
        return
    if args.reset:
        reset_installation(state, old_project)
        previous = {}
    if backup:
        previous, plan = restore_backup(args, backup, target, previous, plan, project, overlay_file)
    run_installation(plan, target, previous,
                     mode='restore' if backup else 'reset' if args.reset else 'update' if existing else 'install')


# Kept running through a restore, to report it.
KEEP_RUNNING = ('dashboard', 'controller')


def describe_export(backup, plan):
    """What restoring an export of another stack does beyond a usual restore, and anything that won't line up."""
    source = backup.manifest.get('export') or {}
    old, host = source.get('media'), source.get('mediaHost')
    media = str(plan['media']) if plan['media'] else None
    print('Adopting the old stack', flush=True)
    if old:
        print(f'   Sonarr and Radarr: series and films move from {old} to this installation’s media folder, '
              f'{media}. Files stay where they are.')
    if host and media and host != media:
        print(f'   Note: the old stack kept its media in {host} on its machine, but this installation’s media folder is '
              f'{media}. Sonarr and Radarr will look for every series and film there.')
    if source.get('plexMedia') and plan['plex']:
        print(f'   Plex: also sees your media at {source["plexMedia"]}, as before, so its libraries need no changes.')
    names = [label for app, label in (('transmission', 'download clients'), ('prowlarr', 'Prowlarr'), ('bazarr', 'Bazarr'))
             if app in plan['downloads']]
    if names:
        listed = ', '.join(names[:-1]) + (' and ' if len(names) > 1 else '') + names[-1]
        print(f'   {listed[0].upper() + listed[1:]}: pointed at this installation’s apps.', flush=True)
    print(flush=True)


def restore_backup(args, backup, state, previous, plan, project, overlay_file):
    """Stop the stack, put the backup's data in place, and plan the run that starts the apps on it. Returns the
    options and plan to carry on with."""
    import fcntl

    if sys.stdin.isatty():
        print('Restoring replaces every app’s settings and data listed above with the backup’s. Anything changed '
              'since it was taken is set aside, not deleted. Media files stay as they are.')
        if input('Type RESTORE to continue: ') != 'RESTORE':
            raise ValueError('Restore cancelled. Nothing was changed.')
    print('Restoring the backup. The apps stop while their data is replaced…', flush=True)
    private_directory(state)
    with (state / 'install.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        listed = subprocess.run(plan['compose'] + ['config', '--services'], env=plan['environment'],
                                capture_output=True, text=True, timeout=120)
        if listed.returncode:
            raise ValueError('Setup could not read the stack before restoring: '
                             + (listed.stderr or listed.stdout).strip()[-600:])
        # The dashboard and controller keep running, so the dashboard can show the restore as it goes; setup restarts
        # them on the restored data at the end. Nothing they hold open is replaced.
        apps = [name for name in listed.stdout.split() if name not in KEEP_RUNNING]
        stopped = subprocess.run(plan['compose'] + ['stop', *apps], env=plan['environment'], capture_output=True,
                                 text=True, timeout=300)
        if stopped.returncode:
            raise ValueError('Setup could not stop the apps before restoring: '
                             + (stopped.stderr or stopped.stdout).strip()[-600:])
        aside = state / datetime.datetime.now().strftime('before-restore-%Y-%m-%d_%H%M%S')
        private_directory(aside)
        restore.Restorer(state, aside, keep_plex_identity=args.keep_plex_identity).restore(backup)
        restore.prune_aside(state, aside)
        merge = restore.merge_exported if backup.exported else restore.merge_options
        options = merge(saved_options(state), previous)
        write_secret(state / 'install-options.json', json.dumps(options))
    print(f'   What the restore replaced is kept in {aside}', flush=True)
    return options, plan_installation(args, state, options, project, overlay_file)


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
        'Release channel': options.get('channel') or 'Not set',
        'Discover': ('Off' if options.get('discover') == 'off' else f'On · {options.get("discover_region") or "GB"}'),
        'Backup folder': options.get('backup_dir') or 'Not set',
        **({'Plex server name': options.get('plex_name') or PLEX_DEFAULT_NAME} if options.get('plex') else {}),
        **({'Plex address': options['plex_url']} if options.get('plex_url') else {}),
        **{f'{name.replace("_", " ").capitalize()} port': str(port) for name, port in (options.get('ports') or {}).items()},
    }


def keep_flags(options):
    """The options that recreate a saved installation's settings after a reset."""
    flags = []
    if options.get('dashboard_bind') and options['dashboard_bind'] != '127.0.0.1':
        flags += ['--dashboard-bind', options['dashboard_bind']]
    if options.get('media_path'):
        flags += ['--media-path', options['media_path']]
    if options.get('backup_dir') and Path(options['backup_dir']) != DEFAULT_BACKUP_DIR:
        flags += ['--backup-dir', options['backup_dir']]
    if options.get('discover_region', 'GB') != 'GB':
        flags += ['--discover-region', options['discover_region']]
    if options.get('discover') == 'off':
        flags += ['--discover', 'off']
    if options.get('plex_url'):
        flags += ['--plex-url', options['plex_url']]
    if options.get('channel') in CHANNELS:
        flags += ['--channel', options['channel']]
    if options.get('vpn'):
        flags.append('--enable-vpn')
        if options.get('vpn_city'):
            flags += ['--vpn-city', options['vpn_city']]
        elif options.get('vpn_country'):
            flags += ['--vpn-country', options['vpn_country']]
        if options.get('vpn_protocol') == 'tcp':
            flags += ['--vpn-protocol', 'tcp']
    apps = options.get('download_apps', LEGACY_DOWNLOADS if options.get('downloads') else ())
    if 'transmission' in apps:
        flags += ['--download-client', 'transmission']
    if 'prowlarr' in apps:
        flags.append('--enable-prowlarr')
    if 'flaresolverr' in apps:
        flags.append('--enable-flaresolverr')
    if 'bazarr' in apps:
        flags.append('--enable-bazarr')
    if options.get('plex'):
        flags.append('--enable-plex')
        if options.get('plex_name', PLEX_DEFAULT_NAME) != PLEX_DEFAULT_NAME:
            flags += ['--plex-name', options['plex_name']]
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
            flags = keep_flags(current) if args.reset else None
            if flags:
                print('To keep the current settings instead, rerun with:')
                print('  ' + shlex.join(flags))
    print(f'\n{mode} plan')
    print(f'State directory   {state}')
    print(f'Address           {plan["bind"]}')
    print(f'Media directory   {plan["media"] or "Not set"}')
    if plan['vpn']:
        print(f'VPN               NordVPN · {plan["vpn_city"] or plan["vpn_country"] or "Automatic"} · '
              f'{plan["options"]["vpn_protocol"].upper()}')
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

    def started_step(label, command, names, diagnose=None, before=None, follows=None):
        """A step that starts apps, followed by what it did to each: started, restarted (and why), or unchanged.
        Apps that follow another, like the download apps on Gluetun's network, say so when that is why."""
        before = container_states(project, environment) if before is None else before
        compose_step(label, command, diagnose, watch=names, follows=follows)
        after = container_states(project, environment)
        changed = getattr(pull_images, 'changed', set())
        leader_restarted = bool(follows) and before.get(follows) is not None and before.get(follows) != after.get(follows)
        for name in sorted(names, key=lambda item: app_label(item).casefold()):
            outcome = restart_outcome(before.get(name), after.get(name), name in changed)
            if leader_restarted and outcome in ('restarted', 'restarted with new settings'):
                outcome = f'restarted with {app_label(follows)}'
            if outcome:
                print(f'   {app_label(name):<20} {outcome}', flush=True)

    def compose_step(label, command, diagnose=None, watch=(), follows=None):
        print(label, flush=True)
        with tempfile.TemporaryFile() as log:
            process = subprocess.Popen(compose + command, env=environment, stdout=log, stderr=subprocess.STDOUT)
            progress = StepProgress(project, watch, environment, follows=follows) if watch else None
            # Quick steps finish without a progress line; slower ones show each app's state every second.
            while True:
                try:
                    process.wait(timeout=1)
                    break
                except subprocess.TimeoutExpired:
                    if progress:
                        progress.update()
            if progress:
                progress.clear()
            if process.returncode == 0:
                return
            log.seek(max(0, log.tell() - 12000))
            details = log.read().decode(errors='replace').strip()
        if details:
            print(f'\nDocker details\n{details}\n', file=sys.stderr)
        if watch:
            # Where each app had got to when the step gave up.
            for name, found in live_states(project, watch, environment).items():
                print(f'   {app_label(name):<20} {describe_state(found)[0]}', file=sys.stderr)
        if diagnose:
            diagnose()
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
        backup_host_dir = Path(environment['MARTINFLIX_BACKUP_HOST_DIR'])
        try:
            if backup_host_dir.parent == DEFAULT_BACKUP_DIR:
                DEFAULT_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
            backup_host_dir.mkdir(mode=0o700, exist_ok=True)
        except OSError as exc:
            raise ValueError(f'Setup cannot write to the backup folder {backup_host_dir.parent} ({exc.strerror}). '
                             'Check its permissions, or choose another with --backup-dir.') from None
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
        given = plan.get('secrets') or {}
        forced = apply_settings_secrets(given, secret_dir, state / 'download-secrets') if given else False
        vpn_credentials_changed = (prepare_vpn_secrets(state / 'download-secrets') if vpn else False) or (forced and vpn)
        if plan['set_tmdb_key']:
            prepare_tmdb_key(secret_dir / 'tmdb-key')
        if plan['set_plex_token']:
            prepare_plex_token(secret_dir / 'plex-token')
        if plan['control']:
            prepare_control_pin(state / 'controller', change=plan['change_pin'])
            if not (secret_dir / 'controller-token').is_file():
                write_secret(secret_dir / 'controller-token', secrets.token_urlsafe(32))
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
        keep_overlays(compose, state, plan['overlay_dir'])
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
        # MartinFlix's own image is the one setup runs from, so it is already here.
        images = [*APPS, *(['gluetun'] if vpn else []), *downloads, *(['plex'] if plan['plex'] else [])]
        print('2. Downloading the latest app images. The first download may take a few minutes…', flush=True)
        if (details := pull_images(compose, environment, images)) is not None:
            if details:
                print(f'\nDocker details\n{details}\n', file=sys.stderr)
            raise ValueError('Setup could not download the app images. If the details mention "toomanyrequests", the '
                             'image registry is busy: wait a few minutes, then rerun setup with the same options. '
                             'Otherwise check this VM’s internet connection and Docker storage.')
        started_step('3. Sonarr and Radarr…', ['up', '-d', '--wait', '--wait-timeout', '360', *APPS], APPS)
        arr_keys = {app: wait_for_key(state / app / 'config.xml') for app in APPS}
        for app, key in arr_keys.items():
            write_secret(secret_dir / f'{app}-key', key)
            api = AppApi(app.capitalize(), ports[app], key, address=arr_address)
            ensure_arr_login(api, lambda: temporary_password)
        if bootstrapping_arr and arr_address != str(bind):
            arr_address = str(bind)
            environment['MARTINFLIX_DASHBOARD_BIND'] = arr_address
            compose_step('   Opening the protected Sonarr and Radarr logins on your LAN…',
                         ['up', '-d', '--wait', '--wait-timeout', '360', *APPS], watch=APPS)
        step = 4
        prowlarr_key = None
        services = (['gluetun'] if vpn else []) + downloads
        download_address = '127.0.0.1' if bootstrapping_prowlarr else str(bind)
        environment['MARTINFLIX_DOWNLOAD_BIND'] = download_address
        # New credentials only reach a new Gluetun container, and apps sharing its network must follow it.
        recreate = ['--force-recreate'] if vpn_credentials_changed else []
        # Restarting Gluetun restarts the apps on its network too, so they are compared with the state before it.
        before_downloads = container_states(project, environment)
        if vpn:
            refresh_vpn_servers(compose, environment, state / 'gluetun')
            started_step(f'{step}. VPN. Connecting can take a minute or two…',
                         ['up', '-d', '--wait', '--wait-timeout', '180', *recreate, 'gluetun'], ['gluetun'],
                         before=before_downloads,
                         diagnose=lambda: diagnose_vpn(compose, environment, state / 'download-secrets'))
        if services:
            label = 'Download apps' if vpn else 'Download and tracker apps'
            started_step(f'{step}. {label}…' if not vpn else f'   {label}…',
                         ['up', '-d', '--wait', '--wait-timeout', '360', *recreate, *services],
                         [service for service in services if service != 'gluetun'], before=before_downloads,
                         follows='gluetun' if vpn and download_network == 'vpn' else None)
            step += 1
        elif vpn:
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
                         ['up', '-d', '--wait', '--wait-timeout', '360', *services], watch=services,
                         follows='gluetun' if vpn and download_network == 'vpn' else None)
            if bazarr:
                bazarr = BazarrApi(download_address, ports['bazarr'], bazarr.key)
        print(f'{step}. Configuring library defaults and checking app connections…', flush=True)
        if bazarr:
            configure_bazarr(bazarr, arr_keys, seed=seed_bazarr, opensubtitles=opensubtitles)
            if seed_bazarr:
                options['bazarr_defaults_applied'] = True
                write_secret(state / 'install-options.json', json.dumps(options))
        if options.get('legacy_media') and not options.get('legacy_adopted'):
            # Once, after restoring an export of a stack setup didn't install.
            adopt_legacy(arr_keys, ports, options['legacy_media'], media, arr_address=arr_address,
                         prowlarr_key=prowlarr_key, prowlarr_address=download_address,
                         downloads_behind_vpn=download_network == 'vpn', transmission=bool(transmission_password),
                         plex_alias=options.get('plex_media_alias') if plan['plex'] else None, bazarr=bazarr)
            options['legacy_adopted'] = True
            write_secret(state / 'install-options.json', json.dumps(options))
        if 'transmission' in downloads and not options.get('transmission_defaults_applied'):
            seed_transmission_defaults(TransmissionRpc(download_address, ports['transmission'], 'admin', transmission_password))
            options['transmission_defaults_applied'] = True
            write_secret(state / 'install-options.json', json.dumps(options))
        seed_indexers = not options.get('sonarr_indexer_defaults_applied')
        seed_radarr_indexers = options['radarr_indexer_defaults_pending']
        seed_prowlarr = bool(prowlarr_key) and not options.get('prowlarr_indexer_defaults_applied')
        # Earlier setups added indexers disabled, or with no site address; put each right once.
        enable_prowlarr_indexers = bool(prowlarr_key) and not seed_prowlarr and not options.get('prowlarr_indexers_enabled')
        address_prowlarr_indexers = bool(prowlarr_key) and not seed_prowlarr and not options.get('prowlarr_indexers_addressed')
        # Prowlarr's tags decide which indexers Sonarr and Radarr get, and which FlareSolverr serves. Applied once, so
        # tags changed by hand afterwards stay; earlier setups used a single "flaresolverr" tag instead.
        tag_prowlarr = bool(prowlarr_key) and not seed_prowlarr and not options.get('prowlarr_tags_applied')
        configure_apps(arr_keys, ports, media, prowlarr_key, transmission_password, seed_indexers=seed_indexers,
                       seed_radarr_indexers=seed_radarr_indexers, seed_prowlarr=seed_prowlarr, arr_address=arr_address,
                       downloads_behind_vpn=download_network == 'vpn', prowlarr_address=download_address,
                       flaresolverr=bool(prowlarr_key) and 'flaresolverr' in downloads,
                       enable_prowlarr_indexers=enable_prowlarr_indexers, address_prowlarr_indexers=address_prowlarr_indexers,
                       tag_prowlarr=tag_prowlarr)
        if seed_prowlarr or enable_prowlarr_indexers or address_prowlarr_indexers:
            options.update(prowlarr_indexer_defaults_applied=True, prowlarr_indexers_enabled=True,
                           prowlarr_indexers_addressed=True)
        if prowlarr_key:
            options.update(prowlarr_tags_applied=True)
        if seed_indexers or seed_radarr_indexers or seed_prowlarr or enable_prowlarr_indexers or address_prowlarr_indexers \
                or tag_prowlarr:
            options.update(sonarr_indexer_defaults_applied=True, radarr_indexer_defaults_pending=False)
            write_secret(state / 'install-options.json', json.dumps(options))
        if plan['plex']:
            step += 1
            plex_config = state / 'plex'
            private_directory(plex_config)
            sync_prerolls(state / 'preroll')
            # Asked just before Plex starts, because a claim code expires after four minutes.
            claim = '' if plex_token(plex_config) else plex_claim_code()
            if claim:
                environment['MARTINFLIX_PLEX_CLAIM'] = claim
            started_step(f'{step}. Plex…', ['up', '-d', '--wait', '--wait-timeout', '360', 'plex'], ['plex'])
            environment.pop('MARTINFLIX_PLEX_CLAIM', None)
            token = wait_for_plex_token(plex_config, 60 if claim else 1)
            if token and not wait_for_plex_ready(token):
                print('   Plex is taking longer than usual to start, so setup will configure it next time. '
                      'Rerun setup with --update in a few minutes.', flush=True)
                write_secret(secret_dir / 'plex-token', token)
                token = ''
            if token:
                # Live reads now playing and watch history with the server owner's token.
                write_secret(secret_dir / 'plex-token', token)
                # The name and pre-rolls follow setup's options and the clips in the image, whenever they change.
                tracked = plex_tracked_settings(options.get('plex_name') or PLEX_DEFAULT_NAME)
                if options.get('plex_tracked_settings') != tracked:
                    try:
                        plex_request(token, 'PUT', '/:/prefs', tracked)
                        clips = tracked['CinemaTrailersPrerollID'].count('/preroll/')
                        print(f'   Plex: named {tracked["FriendlyName"]}, with {clips} pre-roll clip{"s" if clips != 1 else ""}.',
                              flush=True)
                        options['plex_tracked_settings'] = tracked
                        write_secret(state / 'install-options.json', json.dumps(options))
                    except (OSError, ValueError) as exc:
                        print(f'   Plex: could not set the name and pre-rolls ({exc}). Rerun setup with --update.', flush=True)
                # Once each, so changes made in Plex afterwards are kept.
                for flag, apply, problem in (
                        ('plex_settings_applied', lambda: apply_plex_settings(token), 'apply the default settings'),
                        ('plex_libraries_applied', lambda: seed_plex_libraries(token), 'add the libraries')):
                    if options.get(flag):
                        continue
                    try:
                        apply()
                        options[flag] = True
                        write_secret(state / 'install-options.json', json.dumps(options))
                    except (OSError, ValueError) as exc:
                        print(f'   Plex: could not {problem} ({exc}). Rerun setup with --update to try again.', flush=True)
            else:
                print('   Plex isn’t linked to a Plex account yet. Open Plex Web to claim it, then rerun setup with '
                      '--update to connect Live, apply the default settings and add the libraries.', flush=True)
        web = ['dashboard', 'controller']
        # The controller runs Compose exactly as setup does, with the final addresses and ports, and starts setup
        # again, from a newer image, to update MartinFlix or apply settings changed in the dashboard.
        home = Path(os.environ.get('HOME') or Path.home())
        try:
            previous_version = json.loads((state / 'controller' / 'compose.json').read_text()).get('version')
        except (OSError, ValueError):
            previous_version = None
        write_secret(state / 'controller' / 'compose.json', json.dumps({
            'project': project, 'names': environment['MARTINFLIX_NAMES'], 'compose': compose[1:], 'stateDir': str(state),
            'backupDir': environment['MARTINFLIX_BACKUP_DIR'], 'host': socket.gethostname(),
            'image': OWN_IMAGE, 'version': VERSION, 'revision': REVISION, 'channel': options['channel'],
            'setup': {'home': str(home), 'runAs': f'{os.getuid()}:{os.getgid()}',
                      'socket': environment['MARTINFLIX_DOCKER_SOCKET']},
            'env': {key: value for key, value in environment.items() if key.startswith('MARTINFLIX_')},
            **({'transmissionPasswordFile': str(state / 'download-secrets' / 'transmission_password')}
               if 'transmission' in downloads else {})}))
        # Containers from installs that ran the dashboard from a copy of the repo, or the Transmission helper.
        subprocess.run(['docker', 'rm', '--force', f'{project}-transmission-auth-1'], env=environment,
                       capture_output=True, timeout=60)
        before_build = container_states(project, environment)
        compose_step(f'{step + 1}. MartinFlix {VERSION}: dashboard and controller…',
                     ['up', '-d', '--wait', '--wait-timeout', '90', *web], watch=web)
        after_build = container_states(project, environment)
        # Images that older installs built from a copy of the repo.
        subprocess.run(['docker', 'image', 'rm', f'{project}-dashboard', f'{project}-controller'], env=environment,
                       capture_output=True, timeout=60)
        for name in sorted(web, key=lambda item: app_label(item).casefold()):
            outcome = restart_outcome(before_build.get(name), after_build.get(name), False)
            if outcome == 'restarted with new settings' and previous_version != VERSION:
                outcome = 'restarted with the new version'
            if outcome:
                print(f'   {app_label(name):<20} {outcome}', flush=True)
        verify = '''
import server
for app in ('sonarr', 'radarr'):
    result = server.arr_data(app)
    if not result['configured'] or result.get('error'):
        raise SystemExit(f'{app} verification failed: {result.get("error", "key unavailable")}')
    print(f'{app} monitoring connected', flush=True)
'''
        compose_step(f'{step + 2}. Checking that the dashboard can read both libraries…', ['exec', '-T', 'dashboard', 'python', '-B', '-c', verify])
    print_summary(mode, plan, password, (auth_dir / 'password-hash').is_file(), personal_password_set,
                  old_logins=bool(options.get('legacy_media')) and not personal_password_set)


DONE = r"""
   ____
  |  _ \  ___  _ __   ___
  | | | |/ _ \| '_ \ / _ \
  | |_| | (_) | | | |  __/
  |____/ \___/|_| |_|\___|
"""


def heading(title, output):
    output.write(f'\n{title}\n{"─" * len(title)}\n')


def print_summary(mode, plan, password, password_set, apps_use_it, output=None, old_logins=False):
    """The end of a successful run: a clear Done, then where everything is and how to sign in."""
    output = output or sys.stdout
    enabled, ports, bind = plan['enabled'], plan['ports'], plan['bind']
    green = output.isatty() and os.environ.get('TERM') != 'dumb'
    output.write(f'\x1b[32m{DONE}\x1b[0m' if green else DONE)
    apps = [('sonarr', 'Sonarr'), ('radarr', 'Radarr'), ('prowlarr', 'Prowlarr'), ('transmission', 'Transmission'),
            ('bazarr', 'Bazarr'), ('flaresolverr', 'FlareSolverr')]
    names = [name for app, name in apps if app in ('sonarr', 'radarr') or app in plan['downloads']]
    names += ['Plex'] if plan['plex'] else []
    names += ['Gluetun (VPN)'] if plan['vpn'] else []
    listed = ', '.join(names[:-1]) + ' and ' + names[-1]
    done = {'update': 'Updated', 'reset': 'Installed from scratch', 'restore': 'Restored'}.get(mode, 'Installed')
    output.write(f'\n{done}: MartinFlix {VERSION} is running, with {listed} connected.\n')

    heading('Dashboard', output)
    output.write(f'Address   http://{bind}:{ports["dashboard"]}\nUsername  admin\n')
    if password_set:
        output.write('Password  Your dashboard password\n')
    else:
        output.write('Password  A temporary one, shown by this command on the VM:\n'
                     f'            cat {shlex.quote(str(password))}\n'
                     '          Sign in with it, then choose your own.\n')

    heading('Apps', output)
    if old_logins:
        # Adopted from another stack: setup only gives a login to an app that has none.
        output.write('Each keeps its login from the old stack until you choose your dashboard password, which then '
                     'becomes theirs too.\n')
    else:
        output.write(('Each signs in with admin and your dashboard password.\n' if apps_use_it else
                      'Each signs in with admin and the temporary password, until you choose your own in the dashboard.\n'))
    for app in plan['app_dirs']:
        output.write(f'{app.capitalize():13} http://{bind}:{ports[app]}\n')
    if plan['plex']:
        lan = bind if str(bind) != '127.0.0.1' else 'this-vm-address'
        output.write(f'{"Plex":13} http://{lan}:{PLEX_PORT}/web\n')
    if 'flaresolverr' in enabled:
        output.write('FlareSolverr  no web page; tag an indexer "flaresolverr" in Prowlarr to use it\n')
    if plan['vpn'] or plan['downloads']:
        heading('Network', output)
        if plan['vpn']:
            output.write(f'NordVPN exit   {plan["vpn_city"] or plan["vpn_country"] or "Automatic"}\n')
        if plan['downloads']:
            output.write(f'Downloads      {"through the VPN" if plan["download_network"] == "vpn" else "normal internet connection"}\n')

    heading('From now on', output)
    output.write('Everything else happens in the dashboard: restart and update apps on Services, and update MartinFlix\n'
                 'or change its settings under Settings, with your PIN. Forgot the PIN? Run the update with --change-pin.\n')
    output.flush()


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', type=Path,
                        help='Where app data is stored. Defaults to ~/.local/state/martinflix for new installs.')
    parser.add_argument('--dashboard-bind',
                        help='IPv4 address for the dashboard, Sonarr, and Radarr. Defaults to the saved address, '
                             'or 127.0.0.1 for a new installation.')
    parser.add_argument('--dashboard-port', type=int)
    parser.add_argument('--media-path', help='Existing writable media directory containing or receiving TV Shows, Movies, '
                                             'and downloads. Required for a new installation or a reset.')
    parser.add_argument('--enable-downloads', action='store_true',
                        help='Install Gluetun, Transmission, and Prowlarr. Requires --media-path. Defaults to VPN routing.')
    parser.add_argument('--enable-vpn', action='store_true', help='Install the NordVPN gateway.')
    parser.add_argument('--vpn-provider', choices=['nordvpn'], default='nordvpn')
    parser.add_argument('--vpn-country', choices=['Czech Republic', 'United Kingdom', 'Netherlands', 'Germany', 'United States'])
    parser.add_argument('--vpn-city', choices=['Prague'])
    parser.add_argument('--vpn-protocol', choices=['udp', 'tcp'],
                        help='OpenVPN transport. UDP is faster; TCP gets through networks that block VPN traffic over UDP.')
    parser.add_argument('--download-network', choices=['vpn', 'direct'])
    parser.add_argument('--backup-dir',
                        help=f'Folder for nightly backups, ideally on another disk or a NAS share. Each machine '
                             f'uses its own subfolder. Defaults to {DEFAULT_BACKUP_DIR}.')
    parser.add_argument('--enable-plex', action='store_true',
                        help='Install Plex Media Server for your media directory. Requires --media-path.')
    parser.add_argument('--plex-name',
                        help=f'Name of the Plex server this installation runs. Defaults to {PLEX_DEFAULT_NAME}.')
    parser.add_argument('--plex-url',
                        help='Address of your Plex server, such as http://192.168.1.10:32400, for Live’s now playing '
                             'and watch history. An empty value disconnects it.')
    parser.add_argument('--set-plex-token', action='store_true',
                        help='Enter or replace the Plex token that lets the dashboard read Plex.')
    parser.add_argument('--discover', choices=['on', 'off'],
                        help='Show or hide the dashboard’s Discover page of trending and upcoming titles. On by default.')
    parser.add_argument('--set-tmdb-key', action='store_true',
                        help='Use your own TMDB API key for Discover instead of MartinFlix’s, or go back to it.')
    parser.add_argument('--discover-region',
                        help='Two-letter country for Discover’s cinema and streaming lists. Defaults to GB.')
    parser.add_argument('--channel', choices=CHANNELS,
                        help='Which MartinFlix releases the dashboard offers: stable (version tags) or edge (every '
                             'change). Defaults to the channel of the image you ran.')
    parser.add_argument('--settings-file', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--restore', type=Path, metavar='BACKUP',
                        help='Restore a backup folder, such as /mnt/martinflix/Backups/vm/2026-11-22_010000, into this '
                             'installation, or into a new one. Setup checks the backup, stops the apps, puts their '
                             'data back and starts them again. Media files are not touched.')
    parser.add_argument('--keep-plex-identity', action='store_true',
                        help='With --restore: keep this machine’s own Plex server identity and sign-in, so it stays a '
                             'separate server with the backup’s libraries and history. For trying out a restore of '
                             'another machine’s backup while that machine keeps running.')
    parser.add_argument('--change-pin', action='store_true',
                        help='Choose a new PIN for restarting and updating apps from the dashboard.')
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


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if os.environ.get('MARTINFLIX_STAGE') != 'run':
        parser.exit(1, 'Setup now runs from the MartinFlix image, with no copy of the repo. Start it with:\n'
                       '  docker run --rm -it --pull always -v /var/run/docker.sock:/var/run/docker.sock -v "$HOME:$HOME" -e HOME \\\n'
                       '    ghcr.io/descalzi/martinflix:edge install --help\n')
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
