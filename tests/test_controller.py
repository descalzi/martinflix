import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'deploy/controller'))
import controller  # noqa: E402

SERVICES = {
    'gluetun': {'image': 'qmcgaw/gluetun:v3.41.3'},
    'transmission': {'image': 'lscr.io/linuxserver/transmission:latest', 'network_mode': 'service:gluetun'},
    'transmission-auth': {'image': 'busybox', 'network_mode': 'none', 'pid': 'service:transmission'},
    'prowlarr': {'image': 'lscr.io/linuxserver/prowlarr:latest', 'network_mode': 'service:gluetun'},
    'sonarr': {'image': 'lscr.io/linuxserver/sonarr:latest'},
    'dashboard': {'build': {'context': '.'}},
    'controller': {'build': {'context': '.'}},
}


class PinTests(unittest.TestCase):
    def guard(self, pin='2468'):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        path = Path(folder.name) / 'pin-hash'
        path.write_text(controller.hash_pin(pin))
        self.now = 1000.0
        return controller.PinGuard(path, clock=lambda: self.now)

    def test_only_the_stored_pin_passes(self):
        guard = self.guard()
        self.assertIsNone(guard.check('2468'))
        self.assertEqual(guard.check('2469')[0], 403)
        self.assertEqual(guard.check('24a8')[0], 403, 'letters never match')

    def test_five_wrong_pins_lock_actions_for_five_minutes(self):
        guard = self.guard()
        for _ in range(4):
            self.assertEqual(guard.check('0000')[0], 403)
        self.assertEqual(guard.check('0000')[0], 423)
        self.assertEqual(guard.check('2468')[0], 423, 'even the right PIN waits out the lock')
        self.now += controller.LOCKOUT_SECONDS + 1
        self.assertIsNone(guard.check('2468'))

    def test_a_right_pin_resets_the_count(self):
        guard = self.guard()
        for _ in range(4):
            guard.check('0000')
        self.assertIsNone(guard.check('2468'))
        self.assertEqual(guard.check('0000')[1], 'Wrong PIN. 4 tries left.')

    def test_changing_the_pin_needs_the_current_one(self):
        guard = self.guard()
        self.assertEqual(guard.replace('1111', '9753')[0], 403)
        self.assertEqual(guard.replace('2468', '97')[0], 400)
        self.assertEqual(guard.replace('2468', '2468')[0], 400)
        self.assertIsNone(guard.replace('2468', '9753'))
        self.assertIsNone(guard.check('9753'))
        self.assertEqual(guard.check('2468')[0], 403, 'the old PIN stops working')
        self.assertEqual(guard.pin_file.stat().st_mode & 0o777, 0o600)
        self.assertEqual([path.name for path in guard.pin_file.parent.iterdir()], ['pin-hash'], 'no temporary files left')

    def test_pin_shape(self):
        self.assertTrue(controller.valid_pin('1234'))
        self.assertTrue(controller.valid_pin('12345678'))
        for pin in ('123', '123456789', '12 4', '١٢٣٤', None, 1234):
            self.assertFalse(controller.valid_pin(pin), pin)


class StackTests(unittest.TestCase):
    def stack(self, ids):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        config = Path(folder.name) / 'compose.json'
        config.write_text(json.dumps({'project': 'martinflix-test', 'compose': ['compose', '-p', 'martinflix-test'],
                                      'env': {'MARTINFLIX_UID': '1000'}}))
        self.calls = []
        self.ids = ids

        def runner(command, **kwargs):
            args = command[1:]
            self.calls.append(args)
            if args[-3:] == ['config', '--format', 'json']:
                return SimpleNamespace(returncode=0, stdout=json.dumps({'services': SERVICES}), stderr='')
            if args[0] == 'ps':
                return SimpleNamespace(returncode=0, stdout='\n'.join(self.ids), stderr='')
            if args[0] == 'inspect':
                return SimpleNamespace(returncode=0, stderr='', stdout=json.dumps([
                    {'Id': self.ids[service], 'Config': {'Labels': {'com.docker.compose.service': service}, 'Image': 'x'},
                     'State': {'Status': 'running', 'StartedAt': '2026-11-20T10:00:00Z'}} for service in self.ids]))
            if args[-3:-1] == ['up', '-d'] or args[-2:] == ['--no-deps', 'gluetun']:
                self.ids = {**self.ids, 'gluetun': self.ids['gluetun'] + '-new'} if self.image_changed else self.ids
            return SimpleNamespace(returncode=0, stdout='', stderr='')
        self.image_changed = False
        return controller.Stack(config, runner=runner)

    def test_followers_share_its_network_or_processes(self):
        self.assertEqual(controller.dependents(SERVICES, 'gluetun'), ['transmission', 'prowlarr', 'transmission-auth'])
        self.assertEqual(controller.dependents(SERVICES, 'transmission'), ['transmission-auth'])
        self.assertEqual(controller.dependents(SERVICES, 'sonarr'), [])

    def test_the_dashboard_and_controller_restart_after_answering_and_never_update_here(self):
        deferred = []
        stack = self.stack({'gluetun': 'g'})
        stack.later = deferred.append
        self.assertEqual(stack.restart('controller'), {'followed': [], 'deferred': True})
        self.assertFalse(any('restart' in call for call in self.calls), 'nothing restarts before the answer is sent')
        deferred[0]()
        self.assertEqual(self.calls[-1][-2:], ['restart', 'controller'])
        with self.assertRaisesRegex(ValueError, 'git pull'):
            stack.update('dashboard')

    def test_restarting_gluetun_restarts_the_apps_on_its_network_after_it(self):
        stack = self.stack({'gluetun': 'g'})
        result = stack.restart('gluetun')
        restarts = [call[call.index('restart') + 1:] for call in self.calls if 'restart' in call]
        self.assertEqual(restarts, [['gluetun'], ['transmission', 'prowlarr', 'transmission-auth']])
        self.assertEqual(result, {'followed': ['transmission', 'prowlarr', 'transmission-auth']})

    def test_an_update_without_a_new_image_changes_nothing_else(self):
        stack = self.stack({'gluetun': 'g'})
        self.assertEqual(stack.update('gluetun'), {'updated': False, 'followed': [], 'image': 'qmcgaw/gluetun:v3.41.3'})
        self.assertFalse(any('--force-recreate' in call for call in self.calls))

    def test_a_new_gluetun_container_brings_its_followers_with_it(self):
        stack = self.stack({'gluetun': 'g'})
        self.image_changed = True
        result = stack.update('gluetun')
        self.assertTrue(result['updated'])
        recreate = next(call for call in self.calls if '--force-recreate' in call)
        self.assertEqual(recreate[recreate.index('--force-recreate') + 1:], ['transmission', 'prowlarr', 'transmission-auth'])
        self.assertIn(['compose', '-p', 'martinflix-test', 'pull', '--quiet', 'gluetun'], self.calls)




class TransmissionPasswordTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.password = self.folder / 'transmission_password'
        self.password.write_text('first')
        self.restarts = []
        stack = SimpleNamespace(config=lambda: {'transmissionPasswordFile': str(self.password)},
                                controllable=lambda: {'transmission': {}},
                                restart=lambda name: self.restarts.append(name))
        self.watch = controller.TransmissionPasswordWatch(stack, controller.threading.Lock(), self.folder / 'applied')

    def test_a_new_password_restarts_transmission_once(self):
        self.assertFalse(self.watch.check(), 'the first look only records the password already in use')
        self.assertFalse(self.watch.check())
        self.password.write_text('second')
        self.assertTrue(self.watch.check())
        self.assertFalse(self.watch.check())
        self.assertEqual(self.restarts, ['transmission'])

    def test_a_failed_restart_is_tried_again(self):
        self.watch.check()
        self.password.write_text('second')
        self.watch.stack.restart = lambda name: (_ for _ in ()).throw(RuntimeError('not running'))
        with self.assertRaises(RuntimeError):
            self.watch.check()
        self.watch.stack.restart = lambda name: self.restarts.append(name)
        self.assertTrue(self.watch.check())

class BuildContextTests(unittest.TestCase):
    def test_every_copied_file_is_let_into_the_build_context(self):
        root = Path(__file__).resolve().parents[1]
        allowed = [line[1:].rstrip('/*') for line in (root / '.dockerignore').read_text().splitlines() if line.startswith('!')]
        for dockerfile in root.glob('deploy/*/Dockerfile'):
            for line in dockerfile.read_text().splitlines():
                parts = line.split()
                if not parts or parts[0] != 'COPY' or any(part.startswith('--from') for part in parts):
                    continue
                for source in parts[1:-1]:
                    source = source.rstrip('/')
                    self.assertTrue(any(source == item or source.startswith(item + '/') for item in allowed),
                                    f'{dockerfile.relative_to(root)} copies {source}, which .dockerignore leaves out')

if __name__ == '__main__':
    unittest.main()
