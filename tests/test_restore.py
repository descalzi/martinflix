import argparse
import io
import json
import os
import sys
import tarfile
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'deploy/bootstrap'))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'deploy/controller'))
import restore  # noqa: E402
import install  # noqa: E402
import backups  # noqa: E402

PLEX = Path('plex')


def tar(path, files):
    with tarfile.open(path, 'w:gz') as archive:
        for name, content in files.items():
            data = content.encode()
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), 0o600
            archive.addfile(info, io.BytesIO(data))


def zipped(path, files):
    with zipfile.ZipFile(path, 'w') as archive:
        for name, content in files.items():
            archive.writestr(name, content)


class RestoreTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.state = self.root / 'state'
        self.backup = self.root / 'nas/vm/2026-11-22_010000'
        self.backup.mkdir(parents=True)
        zipped(self.backup / 'sonarr.zip', {'config.xml': '<Config><ApiKey>old</ApiKey></Config>', 'sonarr.db': 'series then'})
        zipped(self.backup / 'bazarr.zip', {'bazarr.db': 'subtitles then', 'config.yaml': 'auth: then'})
        tar(self.backup / 'transmission.tar.gz', {'settings.json': '{"then": 1}', 'torrents/a.torrent': 'a'})
        tar(self.backup / 'plex.tar.gz', {'Preferences.xml': '<Preferences then="1"/>',
                                          'Databases/com.plexapp.plugins.library.db': 'library then'})
        tar(self.backup / 'martinflix.tar.gz', {
            'install-options.json': json.dumps({'project': 'martinflix-old', 'media_path': '/old/media', 'plex': True,
                                                'plex_name': 'martinflix', 'download_apps': ['transmission', 'bazarr']}),
            'dashboard-auth/password-hash': 'hash then', 'dashboard-secrets/sonarr-key': 'old',
            'download-secrets/transmission_password': 'password then', 'controller/pin-hash': 'pin then'})
        self.write_manifest()
        # The installation as it is now, changed since the backup.
        for path, content in {'sonarr/config.xml': '<Config><ApiKey>new</ApiKey></Config>', 'sonarr/sonarr.db': 'series now',
                              'sonarr/sonarr.db-wal': 'pending writes', 'sonarr/logs.db': 'logs',
                              'bazarr/db/bazarr.db': 'subtitles now', 'bazarr/config/config.yaml': 'auth: now',
                              'transmission/settings.json': '{"now": 1}', 'transmission/torrents/b.torrent': 'b',
                              f'plex/{restore.PLEX_PREFERENCES}': '<Preferences now="1"/>',
                              f'plex/{restore.PLEX_DATABASES}/com.plexapp.plugins.library.db': 'library now',
                              'install-options.json': json.dumps({'project': 'martinflix-new', 'media_path': '/mnt/media'}),
                              'dashboard-secrets/radarr-key': 'radarr now', 'controller/pin-hash': 'pin now',
                              'controller/compose.json': '{}'}.items():
            target = self.state / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)

    def write_manifest(self, **changes):
        apps = {}
        for app, name in (('sonarr', 'sonarr.zip'), ('bazarr', 'bazarr.zip'), ('transmission', 'transmission.tar.gz'),
                          ('plex', 'plex.tar.gz'), ('martinflix', 'martinflix.tar.gz')):
            apps[app] = {'file': name, 'sha256': backups.checksum(self.backup / name)}
        manifest = {'version': 1, 'created': '2026-11-22T01:00:00+00:00', 'apps': apps, 'missing': ['radarr'], **changes}
        (self.backup / 'manifest.json').write_text(json.dumps(manifest))

    def test_every_app_goes_back_and_what_it_replaces_is_kept(self):
        backup = restore.Backup(self.backup)
        self.assertIn('Not in it  Radarr', backup.describe())
        aside = self.state / 'before-restore-1'
        restore.Restorer(self.state, aside, log=lambda line: None).restore(backup)
        read = lambda path: (self.state / path).read_text()
        self.assertEqual(read('sonarr/sonarr.db'), 'series then')
        self.assertEqual(read('sonarr/config.xml'), '<Config><ApiKey>old</ApiKey></Config>')
        self.assertFalse((self.state / 'sonarr/sonarr.db-wal').exists(), 'the old database’s pending writes go aside')
        self.assertEqual(read('sonarr/logs.db'), 'logs', 'files the backup doesn’t have stay')
        self.assertEqual(read('bazarr/db/bazarr.db'), 'subtitles then')
        self.assertEqual(read('bazarr/config/config.yaml'), 'auth: then')
        self.assertEqual(sorted(path.name for path in (self.state / 'transmission/torrents').iterdir()), ['a.torrent'])
        self.assertEqual(read(f'plex/{restore.PLEX_DATABASES}/com.plexapp.plugins.library.db'), 'library then')
        self.assertEqual(read(f'plex/{restore.PLEX_PREFERENCES}'), '<Preferences then="1"/>')
        self.assertEqual(read('controller/pin-hash'), 'pin then')
        self.assertEqual(read('controller/compose.json'), '{}', 'the controller’s own config is not part of a backup')
        self.assertFalse((self.state / 'dashboard-secrets/radarr-key').exists())
        self.assertEqual(oct((self.state / 'download-secrets/transmission_password').stat().st_mode & 0o777), '0o600')
        # Nothing is lost: everything replaced is in the aside folder, in the same place.
        self.assertEqual((aside / 'sonarr/sonarr.db').read_text(), 'series now')
        self.assertEqual((aside / 'sonarr/sonarr.db-wal').read_text(), 'pending writes')
        self.assertEqual((aside / 'transmission/torrents/b.torrent').read_text(), 'b')
        self.assertEqual((aside / 'dashboard-secrets/radarr-key').read_text(), 'radarr now')
        self.assertEqual((aside / f'plex/{restore.PLEX_DATABASES}/com.plexapp.plugins.library.db').read_text(), 'library now')

    def test_a_damaged_or_foreign_backup_is_refused_before_anything_changes(self):
        with zipfile.ZipFile(self.backup / 'sonarr.zip', 'a') as archive:
            archive.writestr('extra', 'x')
        with self.assertRaisesRegex(restore.RestoreError, 'Sonarr.*checksum'):
            restore.Backup(self.backup)
        self.write_manifest(version=2)
        with self.assertRaisesRegex(restore.RestoreError, 'different version'):
            restore.Backup(self.backup)
        with self.assertRaisesRegex(restore.RestoreError, 'no manifest'):
            restore.Backup(self.root)
        self.write_manifest()
        (self.backup / 'bazarr.zip').unlink()
        with self.assertRaisesRegex(restore.RestoreError, 'Bazarr’s archive, bazarr.zip, is missing'):
            restore.Backup(self.backup)

    def test_paths_that_leave_the_state_folder_are_refused(self):
        tar(self.backup / 'transmission.tar.gz', {'torrents/../../../escape': 'x'})
        self.write_manifest()
        with self.assertRaisesRegex(restore.RestoreError, 'unsafe path'):
            restore.Backup(self.backup)
        self.assertFalse((self.root / 'escape').exists())
        self.assertEqual((self.state / 'controller/pin-hash').read_text(), 'pin now', 'nothing restored before the check')

    def test_this_machines_options_stay_and_the_stacks_come_back(self):
        merged = restore.merge_options({'project': 'old', 'media_path': '/old', 'plex': True, 'ports': {'sonarr': 1},
                                        'plex_settings_applied': True},
                                       {'project': 'new', 'media_path': '/mnt/media', 'plex': False})
        self.assertEqual(merged, {'project': 'new', 'media_path': '/mnt/media', 'plex': True, 'plex_settings_applied': True})
        self.assertEqual(restore.merge_options({'media_path': '/old', 'vpn': True}, {}), {'vpn': True},
                         'a new installation takes its paths from setup’s options')

    def test_only_the_newest_aside_folder_is_kept(self):
        for name in ('before-restore-1', 'before-restore-2'):
            (self.state / name).mkdir()
        restore.prune_aside(self.state, self.state / 'before-restore-2')
        self.assertEqual(sorted(path.name for path in self.state.glob('before-restore-*')), ['before-restore-2'])

    def test_setup_stops_the_stack_restores_and_plans_with_the_backups_options(self):
        state = self.state
        state.chmod(0o700)
        for folder in ('sonarr', 'radarr', 'dashboard-secrets', 'dashboard-auth'):
            (state / folder).mkdir(mode=0o700, exist_ok=True)
            (state / folder).chmod(0o700)
        media = self.root / 'media'
        media.mkdir()
        args = argparse.Namespace(**{**vars(install.build_parser().parse_args([])), 'media_path': None})
        previous = {'project': 'martinflix-new', 'media_path': str(media)}
        plan = {'compose': ['docker', 'compose'], 'environment': {}}
        calls = []
        with patch.object(install.subprocess, 'run', side_effect=lambda command, **kwargs: calls.append(command)
                          or SimpleNamespace(returncode=0, stdout='sonarr\ndashboard\nradarr\ncontroller\n', stderr='')), \
                patch.object(install, 'plan_installation', side_effect=lambda *a, **k: {'planned': a[2]}), \
                patch.object(install.sys.stdin, 'isatty', return_value=False), redirect_stdout(io.StringIO()):
            options, new_plan = install.restore_backup(args, restore.Backup(self.backup), state, previous, plan,
                                                       'martinflix-new', self.root / 'overlay.json')
        # The dashboard and controller keep running, to show the restore as it goes.
        self.assertEqual(calls, [['docker', 'compose', 'config', '--services'], ['docker', 'compose', 'stop', 'sonarr', 'radarr']])
        self.assertEqual(options['project'], 'martinflix-new')
        self.assertEqual(options['media_path'], str(media))
        self.assertTrue(options['plex'])
        self.assertEqual(json.loads((state / 'install-options.json').read_text()), options)
        self.assertEqual(new_plan, {'planned': options})
        self.assertEqual(len(list(state.glob('before-restore-*'))), 1)


if __name__ == '__main__':
    unittest.main()
