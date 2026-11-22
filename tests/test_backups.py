import datetime
import json
import os
import sys
import tarfile
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'deploy/controller'))
import backups  # noqa: E402


class FakeStack:
    """Compose replaced by scripted replies: each app's own backup writes a zip into its config folder."""

    def __init__(self, state, backup_dir, services):
        self.state = state
        self.backup_dir = backup_dir
        self.services = services
        self.calls = []
        self.fail = set()
        self.never = set()

    def tick(self):
        """Time passes: Bazarr's queued backup gets written, first half-done, then whole."""
        if not getattr(self, 'bazarr_pending', False) or 'bazarr' in self.never:
            return
        folder = self.state / 'bazarr' / 'backup'
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / 'bazarr_backup_v1.5.3_2026.11.22_20.10.05.zip'
        if not path.exists():
            path.write_bytes(b'PK\x03\x04 still writing')
            return
        with zipfile.ZipFile(path, 'w') as archive:
            archive.writestr('bazarr.db', 'SQLite format 3')
            archive.writestr('config.yaml', 'auth: {}')
        self.bazarr_pending = False

    def config(self):
        return {'stateDir': str(self.state), 'backupDir': str(self.backup_dir), 'host': 'martinflix-test'}

    def controllable(self):
        return {name: {} for name in self.services}

    def compose(self, *args, timeout=600):
        self.calls.append(args)
        service, command = args[2], args[3:]
        if service in self.fail:
            return SimpleNamespace(returncode=7, stdout='', stderr='curl: (7) Failed to connect')
        if command[0] == 'curl' and '-X' in command and command[-1].endswith('/command'):
            folder = self.state / service / 'Backups' / 'manual'
            folder.mkdir(parents=True, exist_ok=True)
            (folder / f'{service}_backup.zip').write_bytes(b'PK' + service.encode())
            return SimpleNamespace(returncode=0, stdout=json.dumps({'id': 42}), stderr='')
        if command[0] == 'curl' and command[-1].endswith('/command/42'):
            return SimpleNamespace(returncode=0, stdout=json.dumps({'status': 'completed'}), stderr='')
        if command[0] == 'curl' and command[-1].endswith('/api/system/backups'):
            # Like Bazarr, answer at once and write the zip a little later, from its job queue.
            self.bazarr_pending = True
            return SimpleNamespace(returncode=0, stdout='', stderr='')
        if command[0] == 'mkdir':
            (self.state / 'plex' / command[-1].removeprefix('/config/')).mkdir(parents=True, exist_ok=True)
        if command[0] == backups.PLEX_SQLITE:
            target = command[-1].split("'")[1].removeprefix('/config/')
            (self.state / 'plex' / target).write_bytes(b'SQLite format 3')
        if command[0] == 'rm':
            import shutil
            shutil.rmtree(self.state / 'plex' / command[-1].removeprefix('/config/'), ignore_errors=True)
        return SimpleNamespace(returncode=0, stdout='', stderr='')


class BackupTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        self.state, self.backup_dir = root / 'state', root / 'nas'
        self.backup_dir.mkdir()
        for app in ('sonarr', 'radarr', 'prowlarr', 'bazarr'):
            (self.state / 'dashboard-secrets').mkdir(parents=True, exist_ok=True)
            (self.state / 'dashboard-secrets' / f'{app}-key').write_text(f'{app}-key\n')
        (self.state / 'transmission' / 'torrents').mkdir(parents=True)
        (self.state / 'transmission' / 'settings.json').write_text('{}')
        (self.state / 'transmission' / 'torrents' / 'a.torrent').write_text('t')
        (self.state / 'plex' / backups.PLEX_PREFERENCES).parent.mkdir(parents=True)
        (self.state / 'plex' / backups.PLEX_PREFERENCES).write_text('<Preferences/>')
        (self.state / 'install-options.json').write_text('{}')
        (self.state / 'controller').mkdir()
        (self.state / 'controller' / 'pin-hash').write_text('scrypt$x$y')

    def make(self, services=('sonarr', 'radarr', 'prowlarr', 'bazarr', 'transmission', 'plex')):
        stack = FakeStack(self.state, self.backup_dir, services)
        return stack, backups.Backups(stack, sleep=lambda _: stack.tick())

    def test_a_backup_holds_every_app_with_checksums(self):
        stack, engine = self.make()
        manifest = engine.run('manual')
        self.assertEqual(manifest['errors'], [])
        self.assertEqual(sorted(manifest['apps']), ['bazarr', 'martinflix', 'plex', 'prowlarr', 'radarr', 'sonarr', 'transmission'])
        folders = list((self.backup_dir / 'martinflix-test').iterdir())
        self.assertEqual(len(folders), 1, 'only the finished folder remains; no .partial')
        snapshot = folders[0]
        self.assertTrue(backups.NAME.fullmatch(snapshot.name))
        on_disk = json.loads((snapshot / 'manifest.json').read_text())
        self.assertEqual(on_disk['apps']['sonarr']['sha256'], backups.checksum(snapshot / 'sonarr.zip'))
        self.assertEqual(on_disk['size'], sum(entry['size'] for entry in on_disk['files']))
        with tarfile.open(snapshot / 'plex.tar.gz') as archive:
            self.assertEqual(sorted(archive.getnames()), ['Databases/com.plexapp.plugins.library.blobs.db',
                                                          'Databases/com.plexapp.plugins.library.db', 'Preferences.xml'])
        with tarfile.open(snapshot / 'transmission.tar.gz') as archive:
            self.assertIn('torrents/a.torrent', archive.getnames())
        with tarfile.open(snapshot / 'martinflix.tar.gz') as archive:
            self.assertIn('dashboard-secrets/sonarr-key', archive.getnames())
        self.assertEqual(os.stat(snapshot / 'martinflix.tar.gz').st_mode & 0o777, 0o600, 'secrets stay private')
        self.assertFalse(any((self.state / 'plex').glob('.martinflix-backup-*')), 'Plex staging is cleaned up')

    def test_one_app_failing_is_recorded_and_the_rest_still_saved(self):
        stack, engine = self.make()
        stack.fail.add('prowlarr')
        manifest = engine.run()
        self.assertEqual(manifest['missing'], ['prowlarr'])
        self.assertIn('prowlarr:', manifest['errors'][0])
        self.assertIn('sonarr', manifest['apps'])

    def test_an_unmounted_nas_is_reported(self):
        stack, engine = self.make()
        stack.backup_dir = self.backup_dir / 'missing' / 'nested'
        stack.backup_dir.parent.mkdir()
        stack.backup_dir.parent.chmod(0o500)
        self.addCleanup(stack.backup_dir.parent.chmod, 0o700)
        if os.geteuid() == 0:
            self.skipTest('root can write anywhere')
        with self.assertRaisesRegex(backups.BackupError, 'Check that the NAS is mounted'):
            engine.run()

    def test_old_backups_thin_to_fourteen_daily_and_eight_weekly(self):
        _, engine = self.make()
        root = self.backup_dir / 'martinflix-test'
        start = datetime.date(2026, 11, 22)
        for days in range(70):
            (root / f'{start - datetime.timedelta(days=days):%Y-%m-%d}_010000').mkdir(parents=True)
        (root / 'not-a-backup').mkdir()
        engine.prune(root)
        kept = sorted((path.name for path in root.iterdir() if backups.NAME.fullmatch(path.name)), reverse=True)
        self.assertEqual(len(kept), 22)
        self.assertEqual(kept[13], '2026-11-09_010000', 'the 14 newest are all kept')
        weekly = [datetime.date.fromisoformat(name[:10]).isocalendar()[:2] for name in kept[14:]]
        self.assertEqual(len(set(weekly)), 8, 'then one per week')
        self.assertTrue((root / 'not-a-backup').exists(), 'other folders are never touched')

    def test_the_next_run_is_one_in_the_morning(self):
        _, engine = self.make()
        evening = datetime.datetime(2026, 11, 22, 18, 30).astimezone()
        self.assertEqual(engine.next_run(evening).strftime('%Y-%m-%d %H:%M'), '2026-11-23 01:00')
        night = datetime.datetime(2026, 11, 23, 0, 30).astimezone()
        self.assertEqual(engine.next_run(night).strftime('%Y-%m-%d %H:%M'), '2026-11-23 01:00')

    def test_a_missed_night_is_caught_up_soon_after_starting(self):
        _, engine = self.make()
        soon = engine.first_run()
        self.assertLess((soon - engine.now()).total_seconds(), 11 * 60, 'no backups yet')
        recent = engine.now() - datetime.timedelta(hours=3)
        (self.backup_dir / 'martinflix-test' / recent.strftime('%Y-%m-%d_%H%M%S')).mkdir(parents=True)
        self.assertEqual(engine.first_run(), engine.next_run())

    def test_only_one_backup_runs_at_a_time(self):
        _, engine = self.make()
        engine.lock.acquire()
        with self.assertRaisesRegex(backups.BackupError, 'already running'):
            engine.run()
        engine.lock.release()


class BazarrTests(unittest.TestCase):
    setUp = BackupTests.setUp
    make = BackupTests.make

    def test_bazarr_is_collected_once_its_queued_backup_is_complete(self):
        stack, engine = self.make(('bazarr',))
        destination = engine.capture_bazarr(self.state, self.backup_dir)
        with zipfile.ZipFile(destination) as archive:
            self.assertEqual(sorted(archive.namelist()), ['bazarr.db', 'config.yaml'])

    def test_a_bazarr_backup_that_never_appears_is_reported(self):
        stack, engine = self.make(('bazarr',))
        stack.never.add('bazarr')
        with self.assertRaisesRegex(backups.BackupError, 'did not appear within 120 seconds'):
            engine.capture_bazarr(self.state, self.backup_dir)


if __name__ == '__main__':
    unittest.main()
