"""Export a media stack that setup didn't install, such as the reference host's own Compose stacks, as a MartinFlix
backup. `install --restore` (or `update --restore`) then moves it onto a stack setup manages.

The export runs beside the old stack, which keeps running. It finds the old containers by name, asks Sonarr, Radarr,
Prowlarr and Bazarr for fresh backups, copies Plex's databases with Plex's own SQLite plus its artwork and other
data, and keeps Transmission's preferences (not its torrents: let downloads finish first). It also records what the
restore needs to adopt the old stack: where it kept media, which image tags its apps ran, and that every default
setup would otherwise add is already in place.

Nothing in the old stack is changed. The export is written to the backup folder like any other backup, in its own
"<host>-export" folder.
"""
import argparse
import json
import os
import re
import socket
import sqlite3
import stat
import subprocess
import sys
import tarfile
from io import BytesIO
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'controller'))
import backups  # noqa: E402

# The old stack's containers, by app. --container app=name overrides one.
CONTAINERS = {'sonarr': 'sonarr', 'radarr': 'radarr', 'prowlarr': 'prowlarr', 'bazarr': 'bazarr',
              'transmission': 'transmission-daemon', 'plex': 'plex'}
REQUIRED = ('sonarr', 'radarr')
LABELS = {'sonarr': 'Sonarr', 'radarr': 'Radarr', 'prowlarr': 'Prowlarr', 'bazarr': 'Bazarr',
          'transmission': 'Transmission', 'plex': 'Plex', 'martinflix': 'MartinFlix options'}
# The images setup runs. An old app on another tag of the same image (Radarr's nightly, say) keeps that tag, because
# its database may already be newer than the latest release understands.
IMAGES = {'sonarr': 'lscr.io/linuxserver/sonarr', 'radarr': 'lscr.io/linuxserver/radarr',
          'prowlarr': 'lscr.io/linuxserver/prowlarr', 'bazarr': 'lscr.io/linuxserver/bazarr'}
TAG = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}')
# Transmission preferences worth keeping. Folders, addresses and logins belong to the old machine; setup's own
# settings file supplies those.
TRANSMISSION_PREFERENCES = (
    'alt-speed-down', 'alt-speed-enabled', 'alt-speed-time-begin', 'alt-speed-time-day', 'alt-speed-time-enabled',
    'alt-speed-time-end', 'alt-speed-up', 'blocklist-enabled', 'blocklist-url', 'dht-enabled', 'download-queue-enabled',
    'download-queue-size', 'encryption', 'idle-seeding-limit', 'idle-seeding-limit-enabled', 'lpd-enabled',
    'peer-limit-global', 'peer-limit-per-torrent', 'pex-enabled', 'preallocation', 'queue-stalled-enabled',
    'queue-stalled-minutes', 'ratio-limit', 'ratio-limit-enabled', 'rename-partial-files', 'seed-queue-enabled',
    'seed-queue-size', 'speed-limit-down', 'speed-limit-down-enabled', 'speed-limit-up', 'speed-limit-up-enabled',
    'start-added-torrents', 'trash-original-torrent-files', 'utp-enabled')
TRANSMISSION_SETTINGS = Path(__file__).with_name('transmission-settings.json')
PLEX_SERVER = PurePosixPath('Library/Application Support/Plex Media Server')
# Plex's folders it rebuilds by itself, or that hold the old machine's own files, are left out. The databases and
# Preferences.xml are copied separately.
PLEX_SKIP = {'Cache', 'Codecs', 'Crash Reports', 'Diagnostics', 'Drivers', 'Logs', 'Updates', 'Preferences.xml',
             'plexmediaserver.pid', 'Setup Plex.html'}
PLEX_PLUGIN_SUPPORT = ('Data', 'Metadata Combination', 'Preferences')
# Read whole files up to this size, so a file Plex changes while it is copied can't leave a broken archive.
WHOLE_FILE = 256 * 1024 * 1024
# What setup otherwise adds once to a new installation. The old stack has its own choices, so none of it is added.
ALREADY_SET = {
    'sonarr_indexer_defaults_applied': True, 'radarr_indexer_defaults_pending': False,
    'prowlarr_indexer_defaults_applied': True, 'prowlarr_indexers_enabled': True, 'prowlarr_indexers_addressed': True,
    'prowlarr_tags_applied': True, 'bazarr_defaults_applied': True, 'transmission_defaults_applied': True,
    'plex_settings_applied': True, 'plex_libraries_applied': True,
}
PLEX_NAME = re.compile(r'[\w][\w .()\'-]{0,49}')


class ExportError(Exception):
    pass


def docker(*args, timeout=60):
    return subprocess.run(['docker', *args], capture_output=True, text=True, timeout=timeout)


def inspect(name):
    found = docker('inspect', '--type', 'container', name)
    if found.returncode:
        return None
    try:
        return json.loads(found.stdout)[0]
    except (ValueError, IndexError):
        return None


def mount_source(container, destination):
    """The folder on this machine shown to the container at `destination`, or at a folder above it."""
    destination = PurePosixPath(destination)
    best = None
    for mount in container.get('Mounts') or []:
        inside = PurePosixPath(mount.get('Destination') or '/nowhere')
        if destination == inside or inside in destination.parents:
            if best is None or len(inside.parts) > len(best[0].parts):
                best = (inside, PurePosixPath(mount.get('Source') or ''))
    if best is None:
        return None
    inside, source = best
    return source / destination.relative_to(inside)


def container_path(container, host_path):
    """Where the container sees a folder on this machine, or None if it doesn't."""
    host_path = PurePosixPath(host_path)
    best = None
    for mount in container.get('Mounts') or []:
        source = PurePosixPath(mount.get('Source') or '/nowhere')
        if host_path == source or source in host_path.parents:
            if best is None or len(source.parts) > len(best[0].parts):
                best = (source, PurePosixPath(mount.get('Destination') or ''))
    if best is None:
        return None
    source, inside = best
    return inside / host_path.relative_to(source)


def find_containers(overrides):
    """{app: (container name, inspect data, config folder on this machine)} for the old apps that exist."""
    found = {}
    for app, default in CONTAINERS.items():
        name = overrides.get(app, default)
        data = inspect(name)
        if data is None:
            if app in REQUIRED:
                raise ExportError(f'No container named {name} was found for {LABELS[app]}. Add --container '
                                  f'{app}=<name> with its container name (docker ps lists them).')
            continue
        if not (data.get('State') or {}).get('Running'):
            raise ExportError(f'{LABELS[app]} ({name}) is not running. Start it, then export again: the export asks '
                              'each app for its own backup.')
        config = mount_source(data, '/config')
        if config is None:
            raise ExportError(f'{LABELS[app]} ({name}) keeps no /config folder on this machine, so it cannot be exported.')
        found[app] = (name, data, Path(config))
    return found


def folders(argv):
    """The folders the export reads and writes, for the launcher to share with it: the backup folder, and each old
    app's config folder."""
    args = build_parser().parse_args(argv)
    result = [(Path(args.backup_dir), False)]
    for _, _, config in find_containers(container_overrides(args.container)).values():
        result.append((config, True))
    return result


def container_overrides(values):
    overrides = {}
    for value in values or []:
        app, _, name = value.partition('=')
        if app not in CONTAINERS or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', name):
            raise ExportError(f'Use --container app=name, with app one of {", ".join(CONTAINERS)}.')
        overrides[app] = name
    return overrides


class Containers:
    """The old stack's containers, run the way the backup code runs Compose services."""

    def __init__(self, names):
        self.names = names

    def compose(self, *args, timeout=600):
        if args[:2] != ('exec', '-T') or args[2] not in self.names:
            raise ExportError(f'Unexpected command for the old stack: {args[:3]}')
        return docker('exec', self.names[args[2]], *args[3:], timeout=timeout)


def arr_key(config):
    try:
        key = ElementTree.parse(config / 'config.xml').getroot().findtext('ApiKey', '').strip()
    except (OSError, ElementTree.ParseError) as exc:
        raise backups.BackupError(f'its config.xml cannot be read ({exc})') from None
    if not key:
        raise backups.BackupError('its config.xml has no API key')
    return key


def bazarr_setting(config, section, key):
    """One top-level section value from Bazarr's config.yaml, without a YAML library."""
    try:
        lines = (config / 'config/config.yaml').read_text().splitlines()
    except OSError as exc:
        raise backups.BackupError(f'its config.yaml cannot be read ({exc.strerror})') from None
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


def bazarr_key(config):
    key = bazarr_setting(config, 'auth', 'apikey')
    if not key:
        raise backups.BackupError('its config.yaml has no API key')
    return key


def arr_address(config, default_port):
    """Port and URL base from an app's config.xml, so an app moved off its usual address is still reached."""
    try:
        root = ElementTree.parse(config / 'config.xml').getroot()
    except (OSError, ElementTree.ParseError):
        return f'http://127.0.0.1:{default_port}'
    port = (root.findtext('Port') or '').strip()
    base = (root.findtext('UrlBase') or '').strip().strip('/')
    port = port if port.isdigit() else default_port
    return f'http://127.0.0.1:{port}' + (f'/{base}' if base else '')


def safe_link(name, target):
    """True if a symbolic link at `name` pointing at `target` stays inside the archive's own folders."""
    if not target or target.startswith('/'):
        return False
    depth = len(PurePosixPath(name).parts) - 1
    for part in PurePosixPath(target).parts:
        depth += -1 if part == '..' else 0 if part == '.' else 1
        if depth < 0:
            return False
    return True


class Exporter(backups.Backups):
    def __init__(self, found, backup_dir, log=print):
        super().__init__(Containers({app: name for app, (name, _, _) in found.items()}))
        self.found = found
        self.backup_dir = Path(backup_dir)
        self.log = log
        self.adoption = {}
        self.skipped = []
        self.plex_files = 0

    # The old stack's own places, instead of setup's state folder.
    def settings(self):
        return self.backup_dir, Path('/')

    def root(self):
        return self.backup_dir / f'{socket.gethostname()}-export'

    def config_dir(self, state, app):
        return self.found[app][2]

    def key(self, state, app):
        return bazarr_key(self.found[app][2]) if app == 'bazarr' else arr_key(self.found[app][2])

    def prune(self, root):
        pass

    def address(self, state, app):
        config = self.found[app][2]
        if app == 'bazarr':
            port = bazarr_setting(config, 'general', 'port') or '6767'
            base = (bazarr_setting(config, 'general', 'base_url') or '').strip('/')
            return f'http://127.0.0.1:{port if port.isdigit() else 6767}' + (f'/{base}' if base else '')
        return arr_address(config, backups.ARR_APPS[app][0])

    def extra_manifest(self):
        return {'export': {'host': socket.gethostname(), **{key: value for key, value in self.adoption.items()
                                                            if key in ('media', 'mediaHost', 'plexMedia')}}}

    def api(self, app, route):
        _, version = backups.ARR_APPS[app]
        reply = self.exec(app, 'curl', '-fsS', '-H', f'X-Api-Key: {self.key(None, app)}',
                          f'{self.address(None, app)}/api/{version}/{route}')
        return json.loads(reply)

    def survey(self):
        """Where the old stack keeps media, as Sonarr, Radarr and Plex see it, and the image tags it runs."""
        roots = [item['path'].rstrip('/') for app in REQUIRED for item in self.api(app, 'rootfolder')
                 if isinstance(item, dict) and isinstance(item.get('path'), str)]
        if not roots:
            raise ExportError('Sonarr and Radarr have no root folders, so there is no library to move.')
        media = os.path.commonpath(roots) if len(roots) > 1 else str(PurePosixPath(roots[0]).parent)
        if media == '/':
            raise ExportError('Sonarr’s and Radarr’s root folders share no parent folder: ' + ', '.join(roots))
        host = mount_source(self.found['sonarr'][1], media)
        self.adoption = {'media': media, 'roots': roots, 'mediaHost': str(host) if host else None}
        if 'plex' in self.found and host:
            plex = container_path(self.found['plex'][1], host)
            self.adoption['plexMedia'] = str(plex) if plex else None
        tags = {}
        for app, image in IMAGES.items():
            if app not in self.found:
                continue
            repository, _, tag = (self.found[app][1].get('Config') or {}).get('Image', '').partition(':')
            if repository.removeprefix('docker.io/') in (image, image.removeprefix('lscr.io/')) and tag \
                    and tag != 'latest' and TAG.fullmatch(tag):
                tags[app] = tag
        self.adoption['images'] = tags
        return self.adoption

    def capture_transmission(self, state, target):
        config = self.found['transmission'][2]
        try:
            old = json.loads((config / 'settings.json').read_text())
        except (OSError, ValueError) as exc:
            raise backups.BackupError(f'its settings.json cannot be read ({exc})') from None
        settings = json.loads(TRANSMISSION_SETTINGS.read_text())
        settings.update({key: old[key] for key in TRANSMISSION_PREFERENCES if key in old})
        body = json.dumps(settings, indent=4, sort_keys=True).encode()
        destination = target / 'transmission.tar.gz'
        with tarfile.open(destination, 'w:gz') as archive:
            info = tarfile.TarInfo('settings.json')
            info.size, info.mode = len(body), 0o600
            archive.addfile(info, BytesIO(body))
        return destination

    def plex_data(self, archive, databases):
        """Plex's artwork, video previews and agent data, so the library looks as it did straight away."""
        server = self.found['plex'][2] / PLEX_SERVER
        tops = []
        for item in sorted(server.iterdir()):
            if item.name in PLEX_SKIP or item.name.startswith('.'):
                continue
            if item.name == 'Plug-in Support':
                tops += [item / name for name in PLEX_PLUGIN_SUPPORT if (item / name).exists()]
            else:
                tops.append(item)
        for top in tops:
            for folder, names, files in os.walk(top, onerror=lambda error: self.skip(error.filename, error, server)):
                names.sort()
                for name in sorted(files) + [name for name in names if (Path(folder) / name).is_symlink()]:
                    self.add_file(archive, Path(folder) / name, server)
        self.locations(databases)

    def skip(self, path, error, server):
        """Note a file or folder that couldn't be copied, and why."""
        try:
            relative = str(Path(path).relative_to(server))
        except (TypeError, ValueError):
            relative = str(path)
        reason = 'gone by the time it was copied' if isinstance(error, FileNotFoundError) else \
            'no permission to read it' if isinstance(error, PermissionError) else error.strerror or str(error)
        self.skipped.append((relative, reason))

    def add_file(self, archive, path, server):
        relative = str(path.relative_to(server))
        name = 'Data/' + relative
        try:
            info = path.lstat()
            entry = tarfile.TarInfo(name)
            entry.mtime, entry.mode = int(info.st_mtime), 0o600
            if stat.S_ISLNK(info.st_mode):
                target = os.readlink(path)
                if not safe_link(relative, target):
                    return
                entry.type, entry.linkname = tarfile.SYMTYPE, target
                archive.addfile(entry)
            elif stat.S_ISREG(info.st_mode):
                with open(path, 'rb') as source:
                    if info.st_size <= WHOLE_FILE:
                        body = source.read()
                        entry.size = len(body)
                        archive.addfile(entry, BytesIO(body))
                    else:
                        entry.size = info.st_size
                        archive.addfile(entry, source)
            else:
                return
            self.plex_files += 1
        except OSError as exc:
            self.skip(path, exc, server)

    def locations(self, databases):
        """Plex's library folders, so the export can say whether the new Plex will find them."""
        try:
            # Immutable: the copy is on a read-only mount, where SQLite can't make its lock files.
            db = sqlite3.connect(f'file:{databases / "com.plexapp.plugins.library.db"}?immutable=1', uri=True)
            try:
                rows = db.execute('SELECT root_path FROM section_locations').fetchall()
            finally:
                db.close()
        except sqlite3.Error:
            return
        self.adoption['plexFolders'] = sorted(row[0] for row in rows if isinstance(row[0], str))

    def capture_martinflix(self, state, target):
        options = dict(ALREADY_SET)
        options.update(legacy_media=self.adoption['media'], legacy_media_host=self.adoption.get('mediaHost'))
        if self.adoption.get('plexMedia'):
            options['plex_media_alias'] = self.adoption['plexMedia']
        if self.adoption.get('images'):
            options['image_tags'] = self.adoption['images']
        if 'plex' in self.found:
            try:
                name = ElementTree.parse(self.found['plex'][2] / PLEX_SERVER / 'Preferences.xml').getroot().get('FriendlyName')
            except (OSError, ElementTree.ParseError):
                name = None
            if name and PLEX_NAME.fullmatch(name.strip()):
                options['plex_name'] = name.strip()
        body = json.dumps(options).encode()
        destination = target / 'martinflix.tar.gz'
        with tarfile.open(destination, 'w:gz') as archive:
            info = tarfile.TarInfo('install-options.json')
            info.size, info.mode = len(body), 0o600
            archive.addfile(info, BytesIO(body))
        os.chmod(destination, 0o600)
        return destination

    def plan(self):
        steps = [(app, lambda target, app=app: self.capture_arr(app, None, target))
                 for app in backups.ARR_APPS if app in self.found]
        if 'bazarr' in self.found:
            steps.append(('bazarr', lambda target: self.capture_bazarr(None, target)))
        if 'transmission' in self.found:
            steps.append(('transmission', lambda target: self.capture_transmission(None, target)))
        if 'plex' in self.found:
            steps.append(('plex', lambda target: self.capture_plex(None, target)))
        steps.append(('martinflix', lambda target: self.capture_martinflix(None, target)))
        return [(app, self.logged(app, capture)) for app, capture in steps]

    def logged(self, app, capture):
        def run(target):
            live = sys.stdout.isatty()
            if live:
                self.log(f'   {LABELS[app]:<20} …', end='\r', flush=True)
            try:
                archive = capture(target)
            except Exception as exc:
                self.log(f'   {LABELS[app]:<20} failed: {exc}' + (' ' * 10 if live else ''), flush=True)
                raise
            self.log(f'   {LABELS[app]:<20} {size_text(archive.stat().st_size)}' + (' ' * 10 if live else ''), flush=True)
            return archive
        return run


def size_text(size):
    for unit in ('bytes', 'KB', 'MB', 'GB', 'TB'):
        if size < 1000 or unit == 'TB':
            return f'{size:.0f} {unit}' if unit == 'bytes' else f'{size:.1f} {unit}'
        size /= 1000


def build_parser():
    parser = argparse.ArgumentParser(prog='martinflix export', description=__doc__.split('\n\n')[0])
    parser.add_argument('--backup-dir', required=True,
                        help='Folder to write the export into, such as the NAS backup folder. It goes in a '
                             '"<host>-export" folder inside it.')
    parser.add_argument('--container', action='append', metavar='APP=NAME',
                        help='Container name of an old app, if it isn’t the usual one: ' +
                             ', '.join(f'{app}={name}' for app, name in CONTAINERS.items()))
    parser.add_argument('--check', action='store_true', help='Find the old stack and describe it, without exporting.')
    return parser


def describe(found, adoption):
    print('Found', flush=True)
    for app, (name, data, config) in found.items():
        image = (data.get('Config') or {}).get('Image', '')
        print(f'   {LABELS[app]:<14} {name:<22} {config}   {image}')
    print(f'\nMedia      {adoption["media"]}' + (f' (on this machine: {adoption["mediaHost"]})'
                                              if adoption.get('mediaHost') else ''))
    print('Folders    ' + ', '.join(adoption['roots']))
    if 'plex' in found:
        print(f'Plex sees  {adoption.get("plexMedia") or "none of it"}')
    if adoption.get('images'):
        print('Keeps      ' + ', '.join(f'{LABELS[app]} on its {tag} images' for app, tag in adoption['images'].items()))
    print(flush=True)


def main(argv):
    args = build_parser().parse_args(argv)
    try:
        if os.getuid() == 0:
            raise ExportError('Run the export as your own account, not with sudo.')
        found = find_containers(container_overrides(args.container))
        exporter = Exporter(found, args.backup_dir)
        try:
            adoption = exporter.survey()
        except (backups.BackupError, ValueError, KeyError, TypeError) as exc:
            raise ExportError(f'Sonarr and Radarr could not be asked for their folders ({exc}).') from None
        describe(found, adoption)
        if args.check:
            print('Ready to export. Run the same command without --check.')
            return 0
        print(f'Exporting into {exporter.root()}. The old stack keeps running…', flush=True)
        manifest = exporter.run(kind='export')
    except (ExportError, backups.BackupError) as exc:
        print(f'\n{exc}', file=sys.stderr)
        return 1
    folder = exporter.root() / manifest['created'][:19].replace('T', '_').replace(':', '')
    if exporter.skipped:
        count = len(exporter.skipped)
        print(f'   Plex: {count} file{"s" if count != 1 else ""} could not be copied and {"were" if count != 1 else "was"} '
              f'left out. The first few:')
        for relative, reason in exporter.skipped[:8]:
            print(f'      {relative}: {reason}')
        try:
            listing = folder / 'plex-skipped.txt'
            listing.write_text(''.join(f'{relative}\t{reason}\n' for relative, reason in exporter.skipped))
            print(f'   Every one is listed in {listing}')
        except OSError:
            pass
    plex_folders = adoption.get('plexFolders') or []
    alias = adoption.get('plexMedia')
    outside = [path for path in plex_folders if not alias or not (path == alias or path.startswith(alias.rstrip('/') + '/'))]
    if outside:
        print('   Plex: these library folders are outside the media folder, so the new Plex won’t find them: '
              + ', '.join(outside))
    if manifest['missing']:
        names = ', '.join(LABELS.get(app, app) for app in manifest['missing'])
        print(f'\nExport incomplete: {names} could not be exported (see above). Fix that, then export again; '
              f'this attempt, in {folder}, should not be restored.', file=sys.stderr)
        return 1
    print(f'\nExport complete: {folder}')
    print(f'Size: {size_text(manifest["size"])}')
    return 0
