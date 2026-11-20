import argparse
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'deploy/bootstrap'))
import install  # noqa: E402


def arguments(**overrides):
    values = dict(state_dir=None, dashboard_bind=None, dashboard_port=None, media_path=None, enable_downloads=False,
                  enable_vpn=False, vpn_provider='nordvpn', vpn_country=None, vpn_city=None, download_network=None,
                  download_client=None, enable_prowlarr=False, enable_flaresolverr=False, prowlarr_port=None,
                  transmission_port=None, sonarr_port=None, radarr_port=None, check=False, update=False, reset=False)
    values.update(overrides)
    return argparse.Namespace(**values)


class InstallerPlanTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)
        self.state = self.root / 'state'
        self.overlay = self.root / 'downloads.json'

    def tearDown(self):
        self.folder.cleanup()

    def plan(self, previous=None, **overrides):
        return install.plan_installation(arguments(**overrides), self.state, previous or {}, 'martinflix-test', self.overlay)

    def test_defaults_for_a_new_installation(self):
        plan = self.plan()
        self.assertEqual(str(plan['bind']), '127.0.0.1')
        self.assertEqual(plan['ports'], {'dashboard': 8765, 'sonarr': 8989, 'radarr': 7878})
        self.assertEqual(plan['options']['dashboard_bind'], '127.0.0.1')
        self.assertFalse(plan['downloads'])

    def test_saved_address_and_ports_are_reused_unless_overridden(self):
        previous = {'dashboard_bind': '192.168.4.166', 'ports': {'dashboard': 18765, 'sonarr': 18989, 'radarr': 17878}}
        plan = self.plan(previous)
        self.assertEqual(str(plan['bind']), '192.168.4.166')
        self.assertEqual(plan['ports'], previous['ports'])
        plan = self.plan(previous, dashboard_port=8765)
        self.assertEqual(plan['ports']['dashboard'], 8765)
        self.assertEqual(plan['ports']['sonarr'], 18989)

    def test_transmission_requires_media(self):
        with self.assertRaisesRegex(ValueError, 'media directory'):
            self.plan(download_client='transmission')

    def test_flaresolverr_requires_prowlarr(self):
        with self.assertRaisesRegex(ValueError, 'enable-prowlarr'):
            self.plan(enable_flaresolverr=True)

    def test_download_route_cannot_change_without_reset(self):
        previous = {'download_apps': ['prowlarr'], 'download_network': 'direct'}
        with patch.object(install.Path, 'is_char_device', return_value=True):
            with self.assertRaisesRegex(ValueError, '--reset'):
                self.plan(previous, download_network='vpn')

    def test_duplicate_ports_are_rejected(self):
        with self.assertRaisesRegex(ValueError, 'distinct ports'):
            self.plan(sonarr_port=8765)

    def test_running_settings_recover_address_and_ports(self):
        published = [('dashboard', '192.168.4.166', 18765, 8765), ('sonarr', '192.168.4.166', 8989, 8989),
                     ('gluetun', '127.0.0.1', 19091, 9091), ('gluetun', '127.0.0.1', 9696, 9696)]
        bind, ports = install.running_settings(published)
        self.assertEqual(bind, '192.168.4.166')
        self.assertEqual(ports, {'dashboard': 18765, 'sonarr': 8989, 'transmission': 19091, 'prowlarr': 9696})

    def test_published_ports_filters_by_project(self):
        output = ('martinflix-a\tdashboard\t127.0.0.1:8765->8765/tcp\n'
                  'other\tweb\t0.0.0.0:80->80/tcp, [::]:80->80/tcp\n'
                  'martinflix-a\tgluetun\t127.0.0.1:9091->9091/tcp, 127.0.0.1:9696->9696/tcp\n')
        with patch.object(install, 'docker_check', return_value=SimpleNamespace(returncode=0, stdout=output)):
            self.assertEqual(install.published_ports({'martinflix-a'}), [
                ('dashboard', '127.0.0.1', 8765, 8765), ('gluetun', '127.0.0.1', 9091, 9091),
                ('gluetun', '127.0.0.1', 9696, 9696)])


class SharedPasswordTests(unittest.TestCase):
    def test_transmission_uses_the_dashboard_password(self):
        with tempfile.TemporaryDirectory() as folder:
            secrets = Path(folder) / 'download-secrets'
            # A new install, and an old one still on the temporary password, share the temporary password.
            self.assertEqual(install.prepare_transmission_password(secrets, 'temporary-password', False), 'temporary-password')
            target = secrets / 'transmission_password'
            target.write_text('old-random-password')
            inode = target.stat().st_ino
            self.assertEqual(install.prepare_transmission_password(secrets, 'temporary-password', False), 'temporary-password')
            self.assertEqual(target.read_text(), 'temporary-password')
            self.assertEqual(target.stat().st_ino, inode, 'running containers bind-mount this file')
            # Once a personal password exists, the dashboard owns the file and setup leaves it alone.
            target.write_text('personal password')
            self.assertEqual(install.prepare_transmission_password(secrets, 'temporary-password', True), 'personal password')


class ResetSummaryTests(unittest.TestCase):
    # The test host's saved options, plus the address and ports recovered from its running containers.
    current = {'media_path': '/mnt/martinflix/blah', 'downloads': True,
               'download_apps': ['transmission', 'prowlarr', 'flaresolverr'], 'vpn': False, 'vpn_country': '',
               'vpn_city': 'Prague', 'download_network': 'direct', 'dashboard_bind': '192.168.4.166',
               'ports': {'dashboard': 8765, 'sonarr': 8989, 'radarr': 7878, 'transmission': 9091, 'prowlarr': 9696}}

    def test_keep_flags_recreate_the_current_installation(self):
        self.assertEqual(install.keep_flags(self.current), [
            '--dashboard-bind', '192.168.4.166', '--media-path', '/mnt/martinflix/blah',
            '--download-client', 'transmission', '--enable-prowlarr', '--enable-flaresolverr',
            '--download-network', 'direct'])

    def test_a_bare_reset_lists_what_it_will_drop(self):
        plan = {'options': {'dashboard_bind': '127.0.0.1', 'media_path': None, 'download_apps': [], 'vpn': False,
                            'ports': {'dashboard': 8765, 'sonarr': 8989, 'radarr': 7878}},
                'bind': '127.0.0.1', 'media': None, 'vpn': False, 'downloads': [], 'ports': {}}
        lines = []
        with patch('builtins.print', side_effect=lambda *a, **k: lines.append(' '.join(map(str, a)))):
            install.describe_plan(plan, Path('/state'), arguments(reset=True), self.current)
        output = '\n'.join(lines)
        self.assertRegex(output, r'Address +192\.168\.4\.166 → 127\.0\.0\.1')
        self.assertRegex(output, r'Media directory +/mnt/martinflix/blah → Not set')
        self.assertRegex(output, r'Download apps +transmission, prowlarr, flaresolverr → None')
        self.assertNotIn('Transmission port', output)
        self.assertIn('--dashboard-bind 192.168.4.166', output)


class InstallerFlowTests(unittest.TestCase):
    """The installer must validate everything before it stops or archives anything."""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.home = Path(self.folder.name)
        self.state = self.home / 'state'
        self.state.mkdir(mode=0o700)
        (self.state / 'install-options.json').write_text(json.dumps({'project': install.compose_project(self.state, {}, fresh=True)}))
        os.chmod(self.state / 'install-options.json', 0o600)
        self.patches = [
            patch.object(install, 'refuse_root'),
            patch.object(install, 'preflight_docker', return_value=self.home),
            patch.object(install, 'published_ports', return_value=[]),
            patch.object(install, 'check_ports'),
            patch.object(install, 'reset_installation'),
            patch.object(install, 'run_installation'),
        ]
        self.mocks = [item.start() for item in self.patches]
        self.reset, self.run = self.mocks[4], self.mocks[5]

    def tearDown(self):
        patch.stopall()
        self.folder.cleanup()

    def call(self, **overrides):
        overrides.setdefault('state_dir', self.state)
        with patch('builtins.print'):
            install.install(arguments(**overrides), self.home / 'overlay.json')

    def test_invalid_reset_options_fail_before_anything_is_archived(self):
        with self.assertRaisesRegex(ValueError, 'media directory'):
            self.call(reset=True, download_client='transmission')
        self.reset.assert_not_called()
        self.run.assert_not_called()

    def test_reset_check_validates_without_archiving(self):
        self.call(reset=True, check=True)
        self.reset.assert_not_called()
        self.run.assert_not_called()

    def test_reset_archives_then_installs(self):
        self.call(reset=True)
        self.reset.assert_called_once()
        self.run.assert_called_once()

    def test_update_requires_an_existing_installation(self):
        with self.assertRaisesRegex(ValueError, 'No installer-managed'):
            self.call(update=True, state_dir=self.home / 'missing')
        self.run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
