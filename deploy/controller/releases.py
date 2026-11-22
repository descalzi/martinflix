"""MartinFlix's own updates, and settings changed in the dashboard.

The controller looks for a newer MartinFlix image on the installation's channel by asking Docker to download it:
Docker reaches the registry, so the controller itself stays off the internet. Installing that version, or applying
settings changed in the dashboard, runs setup again in a container of its own, the updater, because setup recreates
the controller along with the dashboard. The updater's log stays in Docker, so the new controller can report how it
went.
"""
import json
import os
import re
import secrets
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

CHECK_EVERY_SECONDS = 6 * 3600
FIRST_CHECK_SECONDS = 90
LOG_LINES = 400
VERSION_LABEL = 'org.opencontainers.image.version'
REVISION_LABEL = 'org.opencontainers.image.revision'
CHANNELS = ('edge', 'stable')
ANSI = re.compile(r'\x1b\[[0-9;?]*[A-Za-z]')

# The settings the dashboard can change, as setup's options. The controller checks their shape; setup checks the rest.
VPN_CITIES = ('Prague',)
VPN_COUNTRIES = ('Czech Republic', 'United Kingdom', 'Netherlands', 'Germany', 'United States')
SETTINGS = {
    'discover': ('--discover', re.compile(r'on|off')),
    'discoverRegion': ('--discover-region', re.compile(r'[A-Za-z]{2}')),
    'plexName': ('--plex-name', re.compile(r"[\w][\w .()'-]{0,49}")),
    'plexUrl': ('--plex-url', re.compile(r'(https?://[A-Za-z0-9.\[\]:-]+(:[0-9]{1,5})?/?)?')),
    'backupDir': ('--backup-dir', re.compile(r'/[^\x00-\x1f,]{0,250}')),
    'vpnProtocol': ('--vpn-protocol', re.compile(r'udp|tcp')),
    'channel': ('--channel', re.compile(r'edge|stable')),
}
SECRETS = ('tmdbKey', 'plexToken', 'vpnUser', 'vpnPassword')


class UpdateError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def tail(result, limit=600):
    text = ((result.stderr or '') + (result.stdout or '')).strip()
    return text[-limit:] or f'exit code {result.returncode}'


def setup_arguments(settings):
    """Setup's options for the settings the dashboard sent. Raises UpdateError for anything unexpected."""
    if not isinstance(settings, dict):
        raise UpdateError(400, 'Invalid settings.')
    arguments = []
    for name, value in settings.items():
        if not isinstance(value, str):
            raise UpdateError(400, f'The {name} setting is not valid.')
        if name == 'vpnLocation':
            if value in VPN_CITIES:
                arguments += ['--vpn-city', value]
            elif value in VPN_COUNTRIES:
                arguments += ['--vpn-country', value]
            else:
                raise UpdateError(400, 'Choose one of the VPN exit locations listed.')
        elif name in SETTINGS and SETTINGS[name][1].fullmatch(value):
            arguments += [SETTINGS[name][0], value]
        else:
            raise UpdateError(400, f'The {name} setting is not valid.')
    return arguments


def secret_values(values):
    if values is None:
        return {}
    if not isinstance(values, dict) or any(name not in SECRETS or not isinstance(value, str) or len(value) > 1024
                                           for name, value in values.items()):
        raise UpdateError(400, 'Invalid secret settings.')
    return values


def now_text(clock=time.time):
    return datetime.fromtimestamp(clock(), timezone.utc).isoformat(timespec='seconds')


class Releases:
    def __init__(self, stack, clock=time.time, environ=os.environ, sleep=time.sleep):
        self.stack = stack
        self.clock = clock
        self.environ = environ
        self.sleep = sleep
        self.available = None
        self.checked_at = None
        self.problem = None
        self.lock = threading.Lock()

    # This installation

    def current(self):
        """The version this controller runs, which setup installed alongside it, and the channel it follows."""
        config = self.stack.config()
        channel = config.get('channel') or self.environ.get('MARTINFLIX_CHANNEL', 'dev')
        return {'version': self.environ.get('MARTINFLIX_VERSION', 'dev'),
                'revision': self.environ.get('MARTINFLIX_REVISION', ''),
                'channel': channel if channel in CHANNELS else 'dev',
                'repo': self.environ.get('MARTINFLIX_IMAGE_REPO', 'ghcr.io/descalzi/martinflix')}

    def updater_name(self):
        return f'{self.stack.config()["project"]}-updater'

    # Checking for a newer version

    def check(self):
        """Download the channel's newest image, if Docker doesn't have it, and compare its version with ours."""
        if not self.lock.acquire(blocking=False):
            return
        try:
            current = self.current()
            if current['channel'] not in CHANNELS:
                self.available, self.problem = None, None
                return
            reference = f'{current["repo"]}:{current["channel"]}'
            pulled = self.stack.run(['pull', '--quiet', reference], timeout=900)
            # Without the registry, a newer image already here (downloaded by hand, say) still counts.
            self.problem = 'Could not reach the image registry: ' + tail(pulled, 300) if pulled.returncode else None
            inspected = self.stack.run(['image', 'inspect', reference], timeout=60)
            if inspected.returncode:
                return
            item = json.loads(inspected.stdout)[0]
            labels = item.get('Config', {}).get('Labels') or {}
            version = labels.get(VERSION_LABEL)
            self.available = None if not version or version == current['version'] else {
                'version': version, 'revision': labels.get(REVISION_LABEL, ''), 'id': item['Id'],
                'created': item.get('Created', '')[:19] + 'Z' if item.get('Created') else None}
        except (OSError, ValueError, KeyError, IndexError, subprocess.SubprocessError):
            self.problem = 'Could not check for updates. Docker did not answer as expected.'
        finally:
            self.checked_at = now_text(self.clock)
            self.lock.release()

    def check_soon(self):
        threading.Thread(target=self.check, daemon=True).start()

    def check_forever(self):
        self.sleep(FIRST_CHECK_SECONDS)
        while True:
            self.check()
            self.sleep(CHECK_EVERY_SECONDS)

    # The updater

    def updater(self):
        """The last update or settings change: its state, and the end of setup's output. None if there was none."""
        name = self.updater_name()
        inspected = self.stack.run(['inspect', name], timeout=30)
        if inspected.returncode:
            return None
        try:
            item = json.loads(inspected.stdout)[0]
        except (ValueError, IndexError):
            return None
        state = item.get('State') or {}
        labels = (item.get('Config') or {}).get('Labels') or {}
        logs = self.stack.run(['logs', '--tail', str(LOG_LINES), name], timeout=30)
        text = ANSI.sub('', (logs.stdout or '') + (logs.stderr or '')).replace('\r', '')
        running = bool(state.get('Running'))
        return {'state': 'running' if running else 'succeeded' if state.get('ExitCode') == 0 else 'failed',
                'kind': labels.get('io.martinflix.kind', 'update'), 'version': labels.get('io.martinflix.version'),
                'startedAt': state.get('StartedAt'), 'finishedAt': None if running else state.get('FinishedAt'),
                'log': text.strip()[-30000:]}

    def running(self):
        current = self.updater()
        return bool(current and current['state'] == 'running')

    def start(self, kind, arguments=(), secrets_given=None):
        """Run setup in the updater: from the newer image for an update, from this one to apply settings."""
        if self.running():
            raise UpdateError(409, 'MartinFlix is already updating. Wait for it to finish.')
        config = self.stack.config()
        setup = config.get('setup')
        if not setup:
            raise UpdateError(409, 'This installation was set up before the dashboard could update it. '
                                   'Run the update once from a terminal.')
        current = self.current()
        if kind == 'update':
            if not self.available:
                raise UpdateError(409, 'There is no newer version to install. Check for updates first.')
            version = self.available['version']
            image = f'{current["repo"]}:{version}'
            # Named by its version, as setup expects, so the update needs no second download.
            tagged = self.stack.run(['image', 'tag', self.available['id'], image], timeout=60)
            if tagged.returncode:
                raise UpdateError(502, 'Docker could not prepare the new version: ' + tail(tagged))
        else:
            version = current['version']
            image = config.get('image') or f'{current["repo"]}:{version}'
        name = self.updater_name()
        self.stack.run(['rm', '--force', name], timeout=60)
        extra, secrets_file = [], None
        if secrets_given:
            secrets_file = Path(config['stateDir']) / 'controller' / f'settings-{secrets.token_hex(8)}.json'
            descriptor = os.open(secrets_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, 'w') as output:
                json.dump(secrets_given, output)
            extra = ['--settings-file', str(secrets_file)]
        command = ['run', '--detach', '--name', name,
                   '--label', f'io.martinflix.updater={config["project"]}', '--label', f'io.martinflix.kind={kind}',
                   '--label', f'io.martinflix.version={version}',
                   '-e', f'HOME={setup["home"]}', '-e', f'MARTINFLIX_RUN_AS={setup["runAs"]}',
                   '-e', f'MARTINFLIX_DOCKER_SOCKET={setup["socket"]}',
                   '-v', f'{setup["socket"]}:/var/run/docker.sock',
                   '--mount', f'type=bind,source={setup["home"]},target={setup["home"]}',
                   *([] if Path(config['stateDir']).is_relative_to(setup['home']) else
                     ['--mount', f'type=bind,source={config["stateDir"]},target={config["stateDir"]}']),
                   image, 'update', '--state-dir', config['stateDir'], *extra, *arguments]
        result = self.stack.run(command, timeout=120)
        if result.returncode:
            if secrets_file:
                secrets_file.unlink(missing_ok=True)
            raise UpdateError(502, 'Docker could not start the update: ' + tail(result))
        return {'started': True, 'kind': kind, 'version': version}

    # Reporting

    def brief(self):
        current = self.current()
        return {'current': {key: current[key] for key in ('version', 'revision', 'channel')},
                'available': {key: self.available[key] for key in ('version', 'revision', 'created')}
                if self.available else None,
                'checkedAt': self.checked_at, 'problem': self.problem}

    def status(self):
        return {**self.brief(), 'checking': self.lock.locked(), 'updater': self.updater()}

    def settings(self):
        """The settings the dashboard can change, as setup saved them. Secrets are only said to be set or not."""
        config = self.stack.config()
        state = Path(config['stateDir'])
        options = json.loads((state / 'install-options.json').read_text())
        secrets_dir = state / 'dashboard-secrets'
        return {
            'discover': options.get('discover') or 'on', 'discoverRegion': options.get('discover_region') or 'GB',
            'ownTmdbKey': (secrets_dir / 'tmdb-key').is_file(),
            'plex': bool(options.get('plex')), 'plexName': options.get('plex_name') or 'martinflix',
            'plexUrl': options.get('plex_url') or '', 'plexToken': (secrets_dir / 'plex-token').is_file(),
            'backupDir': options.get('backup_dir') or '',
            'vpn': bool(options.get('vpn')),
            'vpnLocation': options.get('vpn_city') or options.get('vpn_country') or '',
            'vpnProtocol': options.get('vpn_protocol') or 'udp',
            'channel': options.get('channel') or self.current()['channel'],
            'mediaPath': options.get('media_path') or '',
            'choices': {'vpnLocations': [*VPN_CITIES, *VPN_COUNTRIES], 'channels': list(CHANNELS)},
        }
