"""Restoring a backup the controller took (deploy/controller/backups.py) into an installation's state folder.

Setup does the restoring, not the controller, because the controller only reads the state folder. Setup stops the
stack, checks every archive against the backup's checksums, then puts each app's data back. Whatever a restore
replaces is moved aside, not deleted, into a "before-restore" folder inside the state folder. Setup then runs as for
an update, which starts the apps on the restored data and reconnects them.

Media files are never part of a backup, so a restore leaves them as they are.
"""
import datetime
import hashlib
import json
import shutil
import tarfile
import zipfile
from pathlib import Path, PurePosixPath

ARR_APPS = ('sonarr', 'radarr', 'prowlarr')
PLEX_DATABASES = Path('Library/Application Support/Plex Media Server/Plug-in Support/Databases')
PLEX_PREFERENCES = Path('Library/Application Support/Plex Media Server/Preferences.xml')
# MartinFlix's own files in its archive. Options that belong to this machine are kept as they are (see options).
MARTINFLIX_ENTRIES = ('install-options.json', 'dashboard-auth', 'dashboard-secrets', 'download-secrets',
                      'controller/pin-hash')
# Settings that describe this machine rather than the stack: where things are and how they're reached.
MACHINE_OPTIONS = ('project', 'media_path', 'backup_dir', 'dashboard_bind', 'ports', 'channel')
ORDER = ('martinflix', 'sonarr', 'radarr', 'prowlarr', 'bazarr', 'transmission', 'plex')
LABELS = {'martinflix': 'MartinFlix', 'sonarr': 'Sonarr', 'radarr': 'Radarr', 'prowlarr': 'Prowlarr',
          'bazarr': 'Bazarr', 'transmission': 'Transmission', 'plex': 'Plex'}


class RestoreError(ValueError):
    pass


def checksum(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


class Backup:
    """One backup folder, checked: every archive its manifest lists is present and unchanged."""

    def __init__(self, folder):
        self.folder = Path(folder)
        try:
            self.manifest = json.loads((self.folder / 'manifest.json').read_text())
        except FileNotFoundError:
            raise RestoreError(f'{self.folder} is not a MartinFlix backup: it has no manifest.json.') from None
        except (OSError, ValueError) as exc:
            raise RestoreError(f'The backup’s manifest cannot be read ({exc}).') from None
        if not isinstance(self.manifest, dict) or self.manifest.get('version') != 1:
            raise RestoreError('This backup was made by a different version of MartinFlix and cannot be restored here.')
        self.apps = {}
        for app, entry in (self.manifest.get('apps') or {}).items():
            if app not in ORDER:
                continue
            path = self.folder / PurePosixPath(entry.get('file', '')).name
            if not path.is_file():
                raise RestoreError(f'{LABELS[app]}’s archive, {path.name}, is missing from the backup.')
            if checksum(path) != entry.get('sha256'):
                raise RestoreError(f'{LABELS[app]}’s archive, {path.name}, doesn’t match its checksum: it changed or '
                                   'was damaged after the backup. Restore another backup.')
            self.apps[app] = path
            self.check_paths(app, path)
        if not self.apps:
            raise RestoreError('This backup holds no app data to restore.')

    @staticmethod
    def check_paths(app, path):
        """Read every name in an archive before anything is restored, so a bad one stops the restore up front."""
        try:
            if path.suffix == '.zip':
                with zipfile.ZipFile(path) as archive:
                    names = archive.namelist()
            else:
                with tarfile.open(path) as archive:
                    names = archive.getnames()
        except (OSError, zipfile.BadZipFile, tarfile.TarError) as exc:
            raise RestoreError(f'{LABELS[app]}’s archive, {path.name}, cannot be read ({exc}).') from None
        for name in names:
            safe_name(name)

    @property
    def created(self):
        return self.manifest.get('created') or self.folder.name

    @property
    def missing(self):
        return [app for app in self.manifest.get('missing') or [] if app in ORDER]

    def describe(self):
        when = self.created
        try:
            when = datetime.datetime.fromisoformat(when).strftime('%a %d %b %Y at %H:%M')
        except ValueError:
            pass
        apps = ', '.join(LABELS[app] for app in ORDER if app in self.apps)
        lines = [f'Backup     {self.folder}', f'Taken      {when}', f'Restores   {apps}']
        if self.missing:
            lines.append(f'Not in it  {", ".join(LABELS[app] for app in self.missing)} (left as they are now)')
        return '\n'.join(lines)


def safe_name(name):
    """An archive member's path, refused if it could reach outside the folder it is restored into."""
    path = PurePosixPath(name)
    if path.is_absolute() or '..' in path.parts or not path.parts:
        raise RestoreError(f'The backup contains an unsafe path: {name}')
    return path


class Restorer:
    """Puts archives back into the state folder. Replaced files and folders go to the aside folder, keeping their
    place relative to the state folder, so nothing is lost."""

    def __init__(self, state, aside, log=print):
        self.state = Path(state)
        self.aside = Path(aside)
        self.log = log
        self.moved = set()

    def move_aside(self, target):
        target = Path(target)
        if target in self.moved or not (target.exists() or target.is_symlink()):
            return
        destination = self.aside / target.relative_to(self.state)
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        target.rename(destination)
        self.moved.add(target)

    def write(self, source, target, mode=0o600):
        """Write one file from an open archive member, after moving aside whatever was there."""
        self.move_aside(target)
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with open(target, 'wb') as output:
            shutil.copyfileobj(source, output, 1024 * 1024)
        target.chmod(mode)

    def database(self, source, target):
        # A database's write-ahead files belong to the database being replaced, so they go aside with it.
        for suffix in ('-wal', '-shm', '-journal'):
            self.move_aside(target.with_name(target.name + suffix))
        self.write(source, target)

    def tar_entries(self, archive_path):
        """(name, file object) for each regular file in a tar archive; folders are made as files need them."""
        with tarfile.open(archive_path) as archive:
            for member in archive:
                name = safe_name(member.name)
                if member.isfile():
                    yield name, archive.extractfile(member), member.mode

    # One method per app

    def martinflix(self, archive_path):
        for top in MARTINFLIX_ENTRIES:
            self.move_aside(self.state / top)
        for name, source, mode in self.tar_entries(archive_path):
            if not any(str(name) == entry or str(name).startswith(entry + '/') for entry in MARTINFLIX_ENTRIES):
                continue
            self.write(source, self.state / name, mode & 0o700 or 0o600)

    def arr(self, app, archive_path):
        folder = self.state / app
        with zipfile.ZipFile(archive_path) as archive:
            names = [safe_name(name) for name in archive.namelist() if not name.endswith('/')]
            if not any(name.name == 'config.xml' for name in names):
                raise RestoreError(f'{LABELS[app]}’s archive has no config.xml.')
            for name in names:
                if len(name.parts) != 1:
                    continue
                with archive.open(str(name)) as source:
                    if name.suffix == '.db':
                        self.database(source, folder / name.name)
                    elif name.name == 'config.xml':
                        self.write(source, folder / 'config.xml')

    def bazarr(self, archive_path):
        targets = {'bazarr.db': self.state / 'bazarr/db/bazarr.db', 'config.yaml': self.state / 'bazarr/config/config.yaml'}
        with zipfile.ZipFile(archive_path) as archive:
            names = set(archive.namelist())
            if 'config.yaml' not in names:
                raise RestoreError('Bazarr’s archive has no config.yaml.')
            for name, target in targets.items():
                if name in names:
                    with archive.open(name) as source:
                        (self.database if name.endswith('.db') else self.write)(source, target)

    def transmission(self, archive_path):
        folder = self.state / 'transmission'
        for name in ('settings.json', 'resume', 'torrents'):
            self.move_aside(folder / name)
        for name, source, _ in self.tar_entries(archive_path):
            if name.parts[0] in ('settings.json', 'resume', 'torrents'):
                self.write(source, folder / name)

    def plex(self, archive_path):
        folder = self.state / 'plex'
        for name, source, _ in self.tar_entries(archive_path):
            if str(name) == 'Preferences.xml':
                self.write(source, folder / PLEX_PREFERENCES)
            elif name.parts[0] == 'Databases' and len(name.parts) == 2 and name.suffix == '.db':
                self.database(source, folder / PLEX_DATABASES / name.name)

    def restore(self, backup):
        for app in ORDER:
            if app not in backup.apps:
                continue
            archive = backup.apps[app]
            if app in ARR_APPS:
                self.arr(app, archive)
            else:
                getattr(self, app)(archive)
            self.log(f'   {LABELS[app]:<20} restored')


def merge_options(restored, current):
    """The options to carry on with: the backup's, except those that describe this machine, which stay as they are
    now. On a new installation, those come from setup's options instead."""
    merged = {key: value for key, value in restored.items() if key not in MACHINE_OPTIONS}
    merged.update({key: current[key] for key in MACHINE_OPTIONS if key in current})
    return merged


def prune_aside(state, keep):
    """Keep only the newest folder of replaced data; older ones (whole Plex databases, say) would only fill the disk."""
    for folder in state.glob('before-restore-*'):
        if folder.is_dir() and folder != keep:
            shutil.rmtree(folder, ignore_errors=True)
