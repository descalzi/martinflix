"""The controller's daily check for newer app images, and how the dashboard shows it."""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'deploy/controller'))
sys.path.insert(0, str(ROOT))
import controller  # noqa: E402
import server  # noqa: E402


class FakeStack(controller.Stack):
    def __init__(self, images, running, failing=()):
        self.images, self.running, self.failing = images, running, set(failing)
        self.calls = []
        self.app_updates = None
        self.later = lambda action: action()

    def controllable(self):
        return {'sonarr': {'image': 'lscr.io/linuxserver/sonarr:latest'}, 'plex': {'image': 'plexinc/pms-docker:latest'},
                'listenarr': {'image': 'ghcr.io/listenarrs/listenarr:canary-1.10.6'},
                'dashboard': {'image': 'ghcr.io/descalzi/martinflix:edge'}}

    def compose(self, *args, timeout=900):
        self.calls.append(args)
        if args[0] == 'pull':
            for name in args[3:]:
                if name not in self.failing:
                    self.images[name] = self.images.get(f'new:{name}', self.images[name])
        return SimpleNamespace(returncode=0, stdout='', stderr='')

    def run(self, args, timeout=900):
        image = args[-1]
        name = next(name for name, spec in self.controllable().items() if spec['image'] == image)
        return SimpleNamespace(returncode=0, stdout=self.images[name] + '\n', stderr='')

    def containers(self):
        return {name: {'state': 'running', 'imageId': image} for name, image in self.running.items()}


class AppUpdateTests(unittest.TestCase):
    def stack(self, **kwargs):
        images = {'sonarr': 'sha256:s1', 'plex': 'sha256:p1', 'listenarr': 'sha256:l1', 'new:sonarr': 'sha256:s2',
                  'new:plex': 'sha256:p2'}
        stack = FakeStack(images, {'sonarr': 'sha256:s1', 'plex': 'sha256:p1', 'listenarr': 'sha256:l1',
                                   'dashboard': 'sha256:m1'}, **kwargs)
        stack.app_updates = controller.AppUpdates(stack, clock=lambda: 0)
        return stack

    def test_newer_images_are_downloaded_and_flagged(self):
        stack = self.stack()
        stack.app_updates.check()
        self.assertEqual(stack.calls[0][:3], ('pull', '--quiet', '--ignore-pull-failures'))
        self.assertNotIn('dashboard', stack.calls[0], 'MartinFlix updates itself from Settings')
        status = stack.status()['services']
        self.assertEqual({name: item['updateAvailable'] for name, item in status.items()},
                         {'sonarr': True, 'plex': True, 'listenarr': False, 'dashboard': False})
        self.assertIsNotNone(stack.status()['appUpdatesCheckedAt'])

    def test_a_failed_download_hides_only_that_app(self):
        stack = self.stack(failing=['plex'])
        stack.app_updates.check()
        self.assertEqual(sorted(stack.app_updates.available), ['sonarr'])

    def test_the_flag_goes_once_the_app_runs_the_new_image(self):
        stack = self.stack()
        stack.app_updates.check()
        stack.running['sonarr'] = 'sha256:s2'
        self.assertFalse(stack.status()['services']['sonarr']['updateAvailable'], 'recreated by setup, say')
        stack.update('plex')
        self.assertNotIn('plex', stack.app_updates.available)

    def test_nothing_is_checked_while_setup_runs(self):
        stack = self.stack()
        stack.app_updates.busy = lambda: True
        stack.app_updates.check()
        self.assertEqual(stack.calls, [])

    def test_the_dashboard_passes_the_flag_on(self):
        services = [{'name': 'sonarr', 'state': 'running'}]
        server.apply_control_status(services, {'sonarr': {'state': 'running', 'updateAvailable': True}})
        self.assertTrue(services[0]['control']['updateAvailable'])

    def test_a_sign_in_lasts_a_month(self):
        self.assertEqual(server.SESSION_SECONDS, 30 * 24 * 3600)


if __name__ == '__main__':
    unittest.main()
