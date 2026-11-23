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
import os
import shutil
import tarfile
import zipfile
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree

ARR_APPS = ('sonarr', 'radarr', 'prowlarr')
PLEX_DATABASES = Path('Library/Application Support/Plex Media Server/Plug-in Support/Databases')
PLEX_SERVER = Path('Library/Application Support/Plex Media Server')
PLEX_PREFERENCES = PLEX_SERVER / 'Preferences.xml'
# What makes a Plex server itself, rather than its library: its identity, its sign-in to plex.tv, its name and how
# it is reached. --keep-plex-identity keeps these from this machine's own Plex.
PLEX_IDENTITY = ('MachineIdentifier', 'ProcessedMachineIdentifier', 'AnonymousMachineIdentifier', 'CertificateUUID',
                 'PlexOnlineToken', 'PlexOnlineUsername', 'PlexOnlineMail', 'PlexOnlineHome', 'FriendlyName',
                 'PublishServerOnPlexOnlineKey', 'ManualPortMappingMode', 'ManualPortMappingPort',
                 'LastAutomaticMappedPort', 'customConnections')
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


# Large reads: over a NAS, many small ones are slow.
READ_BUFFER = 4 * 1024 * 1024


class Progress:
    """Shows how far through a large archive a long read has got: in place on a terminal, otherwise a line every
    tenth. Small archives show nothing."""

    LARGE = 200 * 1000 * 1000

    def __init__(self, label, total, output=None):
        self.label, self.total, self.output = label, total, output or __import__('sys').stdout
        self.done = 0
        self.shown = -1
        self.live = self.output.isatty()

    def add(self, count):
        self.done += count
        if self.total < self.LARGE:
            return
        percent = min(100, self.done * 100 // self.total)
        step = percent if self.live else percent // 10 * 10
        if step != self.shown:
            self.shown = step
            self.output.write(f'\r   {self.label} {step}%' if self.live else f'   {self.label} {step}%\n')
            self.output.flush()

    def finish(self):
        if self.total >= self.LARGE and self.live:
            self.output.write('\r\x1b[2K')
            self.output.flush()


class HashingReader:
    """A file read once from start to end, hashed on the way, for tarfile's stream mode."""

    def __init__(self, raw, digest, progress):
        self.raw, self.digest, self.progress = raw, digest, progress

    def read(self, size=-1):
        data = self.raw.read(size)
        self.digest.update(data)
        self.progress.add(len(data))
        return data


def scan(path, label, output=None):
    """An archive's SHA-256 and the names in it, from one read through the file: over a NAS, reading a large
    archive twice, or jumping about in it, takes many minutes."""
    digest = hashlib.sha256()
    progress = Progress(f'Checking {label}', path.stat().st_size, output)
    with open(path, 'rb', buffering=READ_BUFFER) as raw:
        reader = HashingReader(raw, digest, progress)
        if path.suffix == '.zip':
            while reader.read(1024 * 1024):
                pass
            with zipfile.ZipFile(path) as archive:
                names = archive.namelist()
        else:
            with tarfile.open(fileobj=reader, mode='r|*') as archive:
                names = [member.name for member in archive]
            while reader.read(1024 * 1024):
                pass
    progress.finish()
    return digest.hexdigest(), names


class Backup:
    """One backup folder, checked: every archive its manifest lists is present and unchanged."""

    def __init__(self, folder, output=None):
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
            try:
                digest, names = scan(path, f'{LABELS[app]}’s archive', output)
            except (OSError, zipfile.BadZipFile, tarfile.TarError) as exc:
                raise RestoreError(f'{LABELS[app]}’s archive, {path.name}, cannot be read ({exc}).') from None
            if digest != entry.get('sha256'):
                raise RestoreError(f'{LABELS[app]}’s archive, {path.name}, doesn’t match its checksum: it changed or '
                                   'was damaged after the backup. Restore another backup.')
            # Every name is checked before anything is restored, so a bad one stops the restore up front.
            for name in names:
                safe_name(name)
            self.apps[app] = path
        if not self.apps:
            raise RestoreError('This backup holds no app data to restore.')

    @property
    def exported(self):
        """An export of a stack setup didn't install (see export.py), rather than one of MartinFlix's own backups."""
        return self.manifest.get('kind') == 'export'

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
        if self.exported:
            source = self.manifest.get('export') or {}
            lines.insert(1, f'From       an export of {source.get("host") or "another stack"}'
                            + (f', with media at {source["media"]}' if source.get('media') else ''))
        if self.missing:
            lines.append(f'Not in it  {", ".join(LABELS[app] for app in self.missing)} (left as they are now)')
        return '\n'.join(lines)


def backup_size(folder):
    """The backup's size from its manifest, in words, or '' if it can't be read."""
    try:
        size = json.loads((Path(folder) / 'manifest.json').read_text()).get('size')
    except (OSError, ValueError, AttributeError):
        return ''
    if not isinstance(size, int) or size <= 0:
        return ''
    for unit in ('bytes', 'KB', 'MB', 'GB', 'TB'):
        if size < 1000 or unit == 'TB':
            return f'{size} bytes' if unit == 'bytes' else f'{size:.1f} {unit}'
        size /= 1000


def safe_name(name):
    """An archive member's path, refused if it could reach outside the folder it is restored into."""
    path = PurePosixPath(name)
    if path.is_absolute() or '..' in path.parts or not path.parts:
        raise RestoreError(f'The backup contains an unsafe path: {name}')
    return path


class Restorer:
    """Puts archives back into the state folder. Replaced files and folders go to the aside folder, keeping their
    place relative to the state folder, so nothing is lost."""

    def __init__(self, state, aside, log=print, keep_plex_identity=False):
        self.state = Path(state)
        self.aside = Path(aside)
        self.log = log
        self.keep_plex_identity = keep_plex_identity
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
        with tarfile.open(archive_path) as archive:
            names = [str(safe_name(member.name)) for member in archive.getmembers() if member.isfile()]
        # Only what the backup holds is replaced: an export carries options alone, and keeps this machine's secrets.
        held = [entry for entry in MARTINFLIX_ENTRIES if any(name == entry or name.startswith(entry + '/') for name in names)]
        for top in held:
            self.move_aside(self.state / top)
        for name, source, mode in self.tar_entries(archive_path):
            if not any(str(name) == entry or str(name).startswith(entry + '/') for entry in held):
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
        server = folder / PLEX_SERVER
        identity = plex_identity(folder / PLEX_PREFERENCES) if self.keep_plex_identity else None
        progress = Progress('Restoring Plex', Path(archive_path).stat().st_size)
        with open(archive_path, 'rb', buffering=READ_BUFFER) as raw, \
                tarfile.open(fileobj=HashingReader(raw, hashlib.sha256(), progress), mode='r|*') as archive:
            for member in archive:
                name = safe_name(member.name)
                if str(name) == 'Preferences.xml' and member.isfile():
                    self.write(archive.extractfile(member), folder / PLEX_PREFERENCES)
                    if identity is not None:
                        keep_identity(folder / PLEX_PREFERENCES, identity)
                elif name.parts[0] == 'Databases' and len(name.parts) == 2 and name.suffix == '.db' and member.isfile():
                    self.database(archive.extractfile(member), folder / PLEX_DATABASES / name.name)
                elif name.parts[0] == 'Data' and len(name.parts) > 2 and (member.isfile() or member.issym()):
                    # An export's artwork and agent data. Each top folder is replaced whole, the first time it comes.
                    relative = PurePosixPath(*name.parts[1:])
                    unit = relative.parts[:2] if relative.parts[0] == 'Plug-in Support' else relative.parts[:1]
                    self.move_aside(server.joinpath(*unit))
                    target = server / relative
                    if member.issym():
                        if not safe_link(relative, member.linkname):
                            continue
                        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                        if not target.is_symlink():
                            os.symlink(member.linkname, target)
                    else:
                        self.write(archive.extractfile(member), target)
        progress.finish()

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


def safe_link(name, target):
    """True if a symbolic link at `name` pointing at `target` stays inside the folder it is restored into."""
    if not target or target.startswith('/'):
        return False
    depth = len(PurePosixPath(name).parts) - 1
    for part in PurePosixPath(target).parts:
        depth += -1 if part == '..' else 0 if part == '.' else 1
        if depth < 0:
            return False
    return True


def plex_identity(preferences):
    """This machine's own Plex identity, from its Preferences.xml."""
    try:
        root = ElementTree.parse(preferences).getroot()
    except (OSError, ElementTree.ParseError) as exc:
        raise RestoreError(f'This machine’s Plex settings cannot be read, so its identity cannot be kept ({exc}).') from None
    if not root.get('MachineIdentifier'):
        raise RestoreError('This machine’s Plex has no identity to keep yet. Leave out --keep-plex-identity.')
    return {key: root.get(key) for key in PLEX_IDENTITY}


def keep_identity(preferences, identity):
    """Put this machine's Plex identity into restored settings, so the restored Plex stays this machine's server."""
    tree = ElementTree.parse(preferences)
    root = tree.getroot()
    for key, value in identity.items():
        if value is None:
            root.attrib.pop(key, None)
        else:
            root.set(key, value)
    tree.write(preferences, encoding='utf-8', xml_declaration=True)
    preferences.chmod(0o600)


def merge_exported(restored, current):
    """An export holds only what adopting the old stack needs, so this installation's options stay, with the
    export's added on top. Options that describe this machine stay as they are now."""
    merged = {**current, **{key: value for key, value in restored.items() if key not in MACHINE_OPTIONS}}
    merged.update({key: current[key] for key in MACHINE_OPTIONS if key in current})
    # The export's data comes back as the old stack had it, so it is adopted again even if an earlier one was.
    merged['legacy_adopted'] = False
    return merged


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
