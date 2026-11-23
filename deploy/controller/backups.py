"""Backups of a MartinFlix stack, taken by the controller.

Each backup is a folder named by its start time, holding one archive per app and a manifest with sizes and SHA-256
checksums. Apps with their own backup feature (Sonarr, Radarr, Prowlarr, Bazarr) are asked to make one, so their
databases are copied consistently while they run. Plex's databases are copied with Plex's own SQLite. Transmission's
settings and torrent list, and MartinFlix's options and secrets, are copied as files.

The folder is written as a hidden ".partial" folder and renamed when complete, so a backup either exists whole or not
at all. Old backups are thinned to the last 14 plus one a week for 8 weeks.
"""
import datetime
import hashlib
import json
import os
import re
import shutil
import socket
import tarfile
import threading
import time
import zipfile
from pathlib import Path

NAME = re.compile(r'\d{4}-\d{2}-\d{2}_\d{6}')
KEEP_DAILY = 14
KEEP_WEEKLY = 8
SCHEDULE_HOUR = 1
# Bazarr writes its backup in the background; this is how long to wait for it.
BAZARR_WAIT_SECONDS = 120
# App backups through each app's own API, run inside its container so the controller needs no network to the apps.
ARR_APPS = {'sonarr': (8989, 'v3'), 'radarr': (7878, 'v3'), 'prowlarr': (9696, 'v1')}
PLEX_DATABASES = 'Library/Application Support/Plex Media Server/Plug-in Support/Databases'
PLEX_PREFERENCES = 'Library/Application Support/Plex Media Server/Preferences.xml'
PLEX_SQLITE = '/usr/lib/plexmediaserver/Plex SQLite'
# Setup's own files: options, the dashboard's password hash and keys, VPN credentials and the PIN hash.
MARTINFLIX_FILES = ('install-options.json', 'dashboard-auth', 'dashboard-secrets', 'download-secrets',
                    'controller/pin-hash')


class BackupError(Exception):
    pass


def checksum(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def newest(folder, pattern, since):
    """The newest file matching the pattern under the folder, written after the given time."""
    found = [path for path in Path(folder).rglob(pattern) if path.is_file() and path.stat().st_mtime >= since - 2]
    return max(found, key=lambda path: path.stat().st_mtime) if found else None


def complete_zip(path, last_member):
    """True once a zip file is whole: readable, with the member its writer adds last."""
    try:
        with zipfile.ZipFile(path) as archive:
            return last_member in archive.namelist()
    except (OSError, zipfile.BadZipFile):
        return False


def add_tree(archive, source, name):
    if source.is_dir():
        archive.add(source, arcname=name, recursive=True)
    elif source.is_file():
        archive.add(source, arcname=name)


class Backups:
    # Plex's artwork and other data beyond its databases: only an export of an older stack carries them.
    plex_data = None

    def __init__(self, stack, clock=time.time, sleep=time.sleep):
        self.stack = stack
        self.clock = clock
        self.sleep = sleep
        self.running = None
        self.last_error = ''
        self.lock = threading.Lock()

    # Settings, from the controller config setup writes.
    def settings(self):
        config = self.stack.config()
        return Path(config['backupDir']), Path(config['stateDir'])

    def root(self):
        folder, _ = self.settings()
        return folder / (self.stack.config().get('host') or socket.gethostname())

    def exec(self, service, *command, timeout=600):
        result = self.stack.compose('exec', '-T', service, *command, timeout=timeout)
        if result.returncode:
            raise BackupError((result.stderr or result.stdout or '').strip()[-300:] or f'exit code {result.returncode}')
        return result.stdout

    def config_dir(self, state, app):
        """Where an app keeps its settings and data on this machine."""
        return state / app

    def extra_manifest(self):
        return {}

    def address(self, state, app):
        """The app's address inside its own container."""
        port = ARR_APPS[app][0] if app in ARR_APPS else 6767
        return f'http://127.0.0.1:{port}'

    def key(self, state, app):
        try:
            return (state / 'dashboard-secrets' / f'{app}-key').read_text().strip()
        except OSError as exc:
            raise BackupError(f'its API key is missing ({exc.strerror})') from None

    # One capture per app. Each returns the archive it wrote into the backup folder.
    def capture_arr(self, app, state, target):
        _, version = ARR_APPS[app]
        started = self.clock()
        base = f'{self.address(state, app)}/api/{version}'
        header = f'X-Api-Key: {self.key(state, app)}'
        reply = self.exec(app, 'curl', '-fsS', '-X', 'POST', '-H', header, '-H', 'Content-Type: application/json',
                          '-d', '{"name": "Backup"}', f'{base}/command')
        command = json.loads(reply).get('id')
        for _ in range(120):
            status = json.loads(self.exec(app, 'curl', '-fsS', '-H', header, f'{base}/command/{command}')).get('status')
            if status == 'completed':
                break
            if status in ('failed', 'aborted', 'cancelled'):
                raise BackupError(f'its backup command {status}')
            self.sleep(1)
        else:
            raise BackupError('its backup did not finish within two minutes')
        made = newest(self.config_dir(state, app) / 'Backups', '*.zip', started)
        if made is None:
            raise BackupError('its backup file could not be found')
        destination = target / f'{app}.zip'
        shutil.copy2(made, destination)
        return destination

    def capture_bazarr(self, state, target):
        started = self.clock()
        self.exec('bazarr', 'curl', '-fsS', '-X', 'POST', '-H', f'X-API-KEY: {self.key(state, "bazarr")}',
                  f'{self.address(state, "bazarr")}/api/system/backups')
        # Bazarr answers straight away and writes the backup from its job queue, so wait for a complete zip:
        # its settings file is the last thing it adds.
        made = None
        for _ in range(BAZARR_WAIT_SECONDS // 2):
            made = newest(self.config_dir(state, 'bazarr'), '*.zip', started)
            if made is not None and complete_zip(made, 'config.yaml'):
                break
            made = None
            self.sleep(2)
        if made is None:
            raise BackupError(f'its backup file did not appear within {BAZARR_WAIT_SECONDS} seconds')
        destination = target / 'bazarr.zip'
        shutil.copy2(made, destination)
        return destination

    def capture_transmission(self, state, target):
        destination = target / 'transmission.tar.gz'
        with tarfile.open(destination, 'w:gz') as archive:
            for name in ('settings.json', 'resume', 'torrents'):
                add_tree(archive, state / 'transmission' / name, name)
        return destination

    def capture_plex(self, state, target):
        """Plex's settings and databases. Its caches and downloaded artwork are left out: Plex rebuilds them."""
        staging = f'/config/.martinflix-backup-{int(self.clock())}'
        self.exec('plex', 'mkdir', '-p', staging)
        try:
            for database in ('com.plexapp.plugins.library.db', 'com.plexapp.plugins.library.blobs.db'):
                source = f'/config/{PLEX_DATABASES}/{database}'
                self.exec('plex', 'test', '-f', source)
                # Plex's own SQLite copies a database consistently while Plex keeps running.
                self.exec('plex', PLEX_SQLITE, source, f".backup '{staging}/{database}'", timeout=1800)
            self.exec('plex', 'chmod', '-R', 'a+rX', staging)
            local = self.config_dir(state, 'plex') / staging.removeprefix('/config/')
            destination = target / ('plex.tar' if self.plex_data else 'plex.tar.gz')
            with tarfile.open(destination, 'w' if self.plex_data else 'w:gz') as archive:
                add_tree(archive, self.config_dir(state, 'plex') / PLEX_PREFERENCES, 'Preferences.xml')
                for database in sorted(local.iterdir()):
                    archive.add(database, arcname=f'Databases/{database.name}')
                if self.plex_data:
                    self.plex_data(archive, local)
            return destination
        finally:
            self.exec('plex', 'rm', '-rf', staging)

    def capture_martinflix(self, state, target):
        destination = target / 'martinflix.tar.gz'
        with tarfile.open(destination, 'w:gz') as archive:
            for name in MARTINFLIX_FILES:
                add_tree(archive, state / name, name)
        os.chmod(destination, 0o600)
        return destination

    def plan(self):
        """The captures this stack needs, in order, from the services setup installed."""
        services = self.stack.controllable()
        _, state = self.settings()
        steps = [(app, lambda target, app=app: self.capture_arr(app, state, target)) for app in ARR_APPS if app in services]
        if 'bazarr' in services:
            steps.append(('bazarr', lambda target: self.capture_bazarr(state, target)))
        if 'transmission' in services:
            steps.append(('transmission', lambda target: self.capture_transmission(state, target)))
        if 'plex' in services:
            steps.append(('plex', lambda target: self.capture_plex(state, target)))
        steps.append(('martinflix', lambda target: self.capture_martinflix(state, target)))
        return steps

    def run(self, kind='scheduled'):
        """Take one backup. Returns its manifest. Raises BackupError if the backup folder can't be used at all."""
        if not self.lock.acquire(blocking=False):
            raise BackupError('A backup is already running.')
        try:
            created = datetime.datetime.now().astimezone()
            self.running = created.isoformat(timespec='seconds')
            root = self.root()
            try:
                root.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise BackupError(f'The backup folder {root} cannot be written: {exc.strerror}. '
                                  'Check that the NAS is mounted.') from None
            name = created.strftime('%Y-%m-%d_%H%M%S')
            partial = root / f'.{name}.partial'
            partial.mkdir(mode=0o700)
            manifest = {'version': 1, 'created': created.isoformat(timespec='seconds'), 'kind': kind,
                        'host': root.name, 'apps': {}, 'files': [], 'missing': [], 'errors': [], **self.extra_manifest()}
            for app, capture in self.plan():
                try:
                    archive = capture(partial)
                    entry = {'file': archive.name, 'size': archive.stat().st_size, 'sha256': checksum(archive)}
                    manifest['apps'][app] = entry
                    manifest['files'].append(entry)
                except (BackupError, OSError, ValueError, KeyError, tarfile.TarError) as exc:
                    manifest['missing'].append(app)
                    manifest['errors'].append(f'{app}: {exc}')
            manifest['size'] = sum(entry['size'] for entry in manifest['files'])
            (partial / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
            os.chmod(partial / 'manifest.json', 0o600)
            partial.rename(root / name)
            self.last_error = '; '.join(manifest['errors'])
            self.prune(root)
            return manifest
        except BackupError as exc:
            self.last_error = str(exc)
            raise
        finally:
            self.running = None
            self.lock.release()

    def prune(self, root):
        """Keep the newest 14 backups, then the newest backup of each of the 8 weeks before those."""
        names = sorted((path.name for path in root.iterdir() if path.is_dir() and NAME.fullmatch(path.name)), reverse=True)
        keep = set(names[:KEEP_DAILY])
        weeks = {}
        for name in names[KEEP_DAILY:]:
            week = datetime.date.fromisoformat(name[:10]).isocalendar()[:2]
            weeks.setdefault(week, name)
        keep.update(sorted(weeks.values(), reverse=True)[:KEEP_WEEKLY])
        for name in names:
            if name not in keep:
                shutil.rmtree(root / name, ignore_errors=True)
        # A backup interrupted part-way (the VM stopped, say) leaves a partial folder; clear any older than a day.
        for path in root.glob('.*.partial'):
            if path.is_dir() and self.clock() - path.stat().st_mtime > 86400:
                shutil.rmtree(path, ignore_errors=True)

    def latest(self):
        try:
            names = sorted(path.name for path in self.root().iterdir() if path.is_dir() and NAME.fullmatch(path.name))
        except OSError:
            return None
        return names[-1] if names else None

    def next_run(self, now=None):
        now = now or datetime.datetime.now().astimezone()
        upcoming = now.replace(hour=SCHEDULE_HOUR, minute=0, second=0, microsecond=0)
        return upcoming if upcoming > now else upcoming + datetime.timedelta(days=1)

    def status(self):
        return {'running': self.running, 'next': self.next_run().isoformat(timespec='minutes'),
                'folder': str(self.root()), 'lastError': self.last_error}

    @staticmethod
    def now():
        return datetime.datetime.now().astimezone()

    def first_run(self):
        """The next 01:00, or ten minutes from now if the last backup is over a day old (the VM was off, say)."""
        latest = self.latest()
        if latest is not None:
            taken = datetime.datetime.strptime(latest, '%Y-%m-%d_%H%M%S').astimezone()
            if self.now() - taken <= datetime.timedelta(hours=26):
                return self.next_run()
        return self.now() + datetime.timedelta(minutes=10)
