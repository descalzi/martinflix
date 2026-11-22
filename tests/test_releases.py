import io
import json
import os
import stat
import sys
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'deploy/controller'))
import controller  # noqa: E402
import releases  # noqa: E402

REPO = 'ghcr.io/descalzi/martinflix'
ENVIRON = {'MARTINFLIX_VERSION': '2026.11.20.4', 'MARTINFLIX_REVISION': 'a' * 40, 'MARTINFLIX_CHANNEL': 'edge',
           'MARTINFLIX_IMAGE_REPO': REPO}


def done(stdout='', returncode=0, stderr=''):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


class FakeDocker:
    """Docker as the controller sees it: a newer image on the registry, and an updater container once started."""

    def __init__(self, newest='2026.11.22.7', pull_fails=False):
        self.newest = newest
        self.pull_fails = pull_fails
        self.calls = []
        self.updater = None

    def run(self, args, timeout=900):
        self.calls.append(args)
        if args[0] == 'pull':
            return done(returncode=1, stderr='toomanyrequests') if self.pull_fails else done()
        if args[:2] == ['image', 'inspect']:
            return done(json.dumps([{'Id': 'sha256:' + 'b' * 64, 'Created': '2026-11-22T18:00:00.123Z', 'Config': {
                'Labels': {releases.VERSION_LABEL: self.newest, releases.REVISION_LABEL: 'c' * 40}}}]))
        if args[0] == 'run':
            self.updater = {'State': {'Running': True, 'ExitCode': 0, 'StartedAt': '2026-11-22T19:00:00Z'},
                            'Config': {'Labels': {'io.martinflix.kind': args[args.index('--label') + 3].split('=')[1],
                                                  'io.martinflix.version': args[args.index('--label') + 5].split('=')[1]}}}
            return done('container-id')
        if args[0] == 'inspect':
            return done(json.dumps([self.updater])) if self.updater else done(returncode=1, stderr='No such object')
        if args[0] == 'logs':
            return done('\x1b[2K   Sonarr               up to date\n', stderr='2. Downloading…\n')
        return done()


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.home = self.root / 'home'
        self.state = self.home / '.local/state/martinflix'
        (self.state / 'controller').mkdir(parents=True)
        (self.state / 'dashboard-secrets').mkdir()
        self.config = {'project': 'martinflix-abc', 'stateDir': str(self.state), 'channel': 'edge',
                       'image': f'{REPO}:2026.11.20.4',
                       'setup': {'home': str(self.home), 'runAs': '1000:1000', 'socket': '/var/run/docker.sock'}}
        self.docker = FakeDocker()
        stack = SimpleNamespace(config=lambda: self.config, run=self.docker.run)
        self.releases = releases.Releases(stack, clock=lambda: 1795374166, environ=ENVIRON)

    def test_a_newer_image_on_the_channel_is_offered(self):
        self.releases.check()
        self.assertIn(['pull', '--quiet', f'{REPO}:edge'], self.docker.calls)
        brief = self.releases.brief()
        self.assertEqual(brief['current'], {'version': '2026.11.20.4', 'revision': 'a' * 40, 'channel': 'edge'})
        self.assertEqual(brief['available'], {'version': '2026.11.22.7', 'revision': 'c' * 40, 'created': '2026-11-22T18:00:00Z'})
        self.assertIsNone(brief['problem'])
        self.assertTrue(brief['checkedAt'].startswith('2026-11-22'))

    def test_the_same_version_is_not_offered_and_registry_problems_are_reported(self):
        self.docker.newest = '2026.11.20.4'
        self.releases.check()
        self.assertIsNone(self.releases.brief()['available'])
        self.docker.pull_fails = True
        self.releases.check()
        self.assertIn('toomanyrequests', self.releases.brief()['problem'])

    def test_a_development_build_checks_nothing(self):
        self.config['channel'] = None
        self.releases.environ = {**ENVIRON, 'MARTINFLIX_CHANNEL': 'dev'}
        self.releases.check()
        self.assertEqual(self.docker.calls, [])
        self.assertEqual(self.releases.current()['channel'], 'dev')

    def test_an_update_runs_setup_from_the_new_image_in_its_own_container(self):
        with self.assertRaises(releases.UpdateError) as caught:
            self.releases.start('update')
        self.assertEqual(caught.exception.status, 409)
        self.releases.check()
        result = self.releases.start('update')
        self.assertEqual(result, {'started': True, 'kind': 'update', 'version': '2026.11.22.7'})
        self.assertIn(['image', 'tag', 'sha256:' + 'b' * 64, f'{REPO}:2026.11.22.7'], self.docker.calls)
        command = self.docker.calls[-1]
        self.assertEqual(command[:4], ['run', '--detach', '--name', 'martinflix-abc-updater'])
        self.assertEqual(command[-4:], [f'{REPO}:2026.11.22.7', 'update', '--state-dir', str(self.state)])
        self.assertIn(f'HOME={self.home}', command)
        self.assertIn('MARTINFLIX_RUN_AS=1000:1000', command)
        self.assertIn(f'type=bind,source={self.home},target={self.home}', command)
        with self.assertRaises(releases.UpdateError) as caught:
            self.releases.start('update')
        self.assertEqual(caught.exception.status, 409, 'one update at a time')

    def test_settings_go_to_setup_as_options_and_secrets_in_a_private_file_only(self):
        arguments = releases.setup_arguments({'discover': 'off', 'vpnLocation': 'United Kingdom', 'plexUrl': '',
                                              'backupDir': '/mnt/martinflix/Backups', 'channel': 'stable'})
        self.assertEqual(arguments, ['--discover', 'off', '--vpn-country', 'United Kingdom', '--plex-url', '',
                                     '--backup-dir', '/mnt/martinflix/Backups', '--channel', 'stable'])
        self.releases.start('settings', arguments, {'tmdbKey': 'k' * 32})
        command = self.docker.calls[-1]
        secrets_file = Path(command[command.index('--settings-file') + 1])
        self.assertEqual(secrets_file.parent, self.state / 'controller')
        self.assertEqual(stat.S_IMODE(secrets_file.stat().st_mode), 0o600)
        self.assertEqual(json.loads(secrets_file.read_text()), {'tmdbKey': 'k' * 32})
        self.assertNotIn('k' * 32, ' '.join(command))
        self.assertEqual(command[command.index('update'):command.index('update') + 3], ['update', '--state-dir', str(self.state)])
        self.assertIn(f'{REPO}:2026.11.20.4', command, 'settings are applied by the version already running')

    def test_unexpected_settings_never_become_options(self):
        for bad in ({'stateDir': '/tmp'}, {'discover': 'maybe'}, {'backupDir': 'relative'}, {'backupDir': '/a,b'},
                    {'plexName': '--reset'}, {'vpnLocation': 'Mars'}, {'channel': 'nightly'}, {'discover': ['on']}):
            with self.assertRaises(releases.UpdateError, msg=bad):
                releases.setup_arguments(bad)
        with self.assertRaises(releases.UpdateError):
            releases.secret_values({'password': 'x'})

    def test_the_updater_reports_its_state_and_output(self):
        self.assertIsNone(self.releases.updater())
        self.releases.check()
        self.releases.start('update')
        updater = self.releases.updater()
        self.assertEqual(updater['state'], 'running')
        self.assertEqual(updater['kind'], 'update')
        self.assertEqual(updater['version'], '2026.11.22.7')
        self.assertIn('Sonarr               up to date', updater['log'])
        self.assertNotIn('\x1b', updater['log'])
        self.docker.updater['State'] = {'Running': False, 'Status': 'exited', 'ExitCode': 1, 'FinishedAt': '2026-11-22T19:04:00Z'}
        self.assertEqual(self.releases.updater()['state'], 'failed')
        # Once finished, its result is kept and the container goes.
        self.assertEqual(self.docker.calls[-1], ['rm', 'martinflix-abc-updater'])
        self.docker.updater = None
        kept = self.releases.updater()
        self.assertEqual((kept['state'], kept['version']), ('failed', '2026.11.22.7'))
        self.assertIn('Sonarr               up to date', kept['log'])

    def test_settings_are_read_from_setup_without_revealing_secrets(self):
        (self.state / 'install-options.json').write_text(json.dumps({
            'discover': 'on', 'discover_region': 'IE', 'plex': True, 'plex_name': 'martinflix-test', 'vpn': True,
            'vpn_city': 'Prague', 'vpn_protocol': 'udp', 'backup_dir': '/mnt/martinflix/Backups', 'channel': 'edge'}))
        (self.state / 'dashboard-secrets/tmdb-key').write_text('secret-key-value')
        settings = self.releases.settings()
        self.assertEqual(settings['discoverRegion'], 'IE')
        self.assertTrue(settings['ownTmdbKey'])
        self.assertFalse(settings['plexToken'])
        self.assertEqual(settings['vpnLocation'], 'Prague')
        self.assertNotIn('secret-key-value', json.dumps(settings))


class HandlerTests(unittest.TestCase):
    def request(self, path, body=None, token='t' * 40, backups=None):
        handler = object.__new__(controller.Handler)
        pins = SimpleNamespace(check=lambda pin: None if pin == '2468' else (403, 'Wrong PIN.'))
        started = []
        handler.server = SimpleNamespace(token='t' * 40, pins=pins, backups=backups, releases=SimpleNamespace(
            start=lambda kind, *args, **kwargs: started.append((kind, *args)) or {'started': True, 'kind': kind},
            running=lambda: False, check_soon=lambda: started.append(('check',))))
        payload = json.dumps(body or {}).encode()
        handler.path = path
        handler.headers = Message()
        handler.headers['Authorization'] = f'Bearer {token}'
        handler.headers['Content-Length'] = str(len(payload))
        handler.rfile = io.BytesIO(payload)
        handler.wfile = io.BytesIO()
        sent = {}
        handler.send_response = lambda code, *args: sent.setdefault('status', code)
        handler.send_header = lambda *args: None
        handler.end_headers = lambda: None
        handler.do_POST()
        return sent['status'], json.loads(handler.wfile.getvalue() or b'{}'), started

    def test_updating_and_settings_need_the_pin(self):
        self.assertEqual(self.request('/release/update', {'pin': '1111'})[0], 403)
        status, _, started = self.request('/release/update', {'pin': '2468'})
        self.assertEqual((status, started), (202, [('update',)]))
        status, _, started = self.request('/settings', {'pin': '2468', 'settings': {'discover': 'off'}})
        self.assertEqual(status, 202)
        self.assertEqual(started, [('settings', ['--discover', 'off'], {})])
        self.assertEqual(self.request('/settings', {'pin': '2468', 'settings': {'discover': 'nope'}})[0], 400)
        self.assertEqual(self.request('/settings', {'pin': '2468', 'settings': {}})[0], 400)
        self.assertEqual(self.request('/release/update', {'pin': '2468'}, token='wrong')[0], 401)

    def test_a_restore_names_a_backup_in_this_machines_folder(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        (root / '2026-11-22_010000').mkdir()
        (root / '2026-11-22_010000' / 'manifest.json').write_text('{}')
        backups_state = SimpleNamespace(root=lambda: root, running=None)
        statuses = []
        for body in ({'pin': '2468', 'backup': '2026-11-22_010000'}, {'pin': '2468', 'backup': '../etc'},
                     {'pin': '2468', 'backup': '2026-11-21_010000'}):
            handler_status, _, started = self.request('/restore', body, backups=backups_state)
            statuses.append(handler_status)
        self.assertEqual(statuses, [202, 400, 404])
        self.assertEqual(started, [])
        _, _, started = self.request('/restore', {'pin': '2468', 'backup': '2026-11-22_010000'}, backups=backups_state)
        self.assertEqual(started, [('restore', ['--restore', str(root / '2026-11-22_010000')])])
        backups_state.running = '2026-11-22T20:00:00'
        self.assertEqual(self.request('/restore', {'pin': '2468', 'backup': '2026-11-22_010000'}, backups=backups_state)[0], 409)

    def test_checking_for_updates_needs_only_the_token(self):
        status, _, started = self.request('/release/check')
        self.assertEqual((status, started), (202, [('check',)]))


if __name__ == '__main__':
    unittest.main()
