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
import configure  # noqa: E402
import install  # noqa: E402


def arguments(**overrides):
    """The installer's real defaults, so new flags never drift from the tests."""
    values = vars(install.build_parser().parse_args([]))
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

    def test_download_apps_follow_the_lan_address_once_prowlarr_has_a_login(self):
        overlay = install.download_overlay(['transmission', 'prowlarr'], {'transmission': 9091, 'prowlarr': 9696}, True)
        self.assertEqual(overlay['services']['gluetun']['ports'],
                         ['${MARTINFLIX_DOWNLOAD_BIND}:9091:9091', '${MARTINFLIX_DOWNLOAD_BIND}:9696:9696'])
        self.assertEqual(overlay['services']['dashboard']['environment']['MARTINFLIX_DOWNLOAD_BIND'], '${MARTINFLIX_DOWNLOAD_BIND}')
        plan = self.plan({'dashboard_bind': '192.168.4.166', 'download_apps': ['prowlarr'], 'download_network': 'direct'},
                         enable_prowlarr=True)
        self.assertTrue(plan['bootstrapping_prowlarr'])
        self.assertIn(('prowlarr', '192.168.4.166', 9696), plan['bindings'])
        self.assertIn(('prowlarr', '127.0.0.1', 9696), plan['bindings'])

    def test_transmission_peer_port_is_published_only_for_direct_downloads(self):
        ports = {'transmission': 9091, 'transmission_peer': 51155}
        direct = install.download_overlay(['transmission'], ports, False)
        self.assertEqual(direct['services']['transmission']['ports'],
                         ['51155:51155/tcp', '51155:51155/udp', '${MARTINFLIX_DOWNLOAD_BIND}:9091:9091'])
        vpn = install.download_overlay(['transmission'], ports, True)
        self.assertEqual(vpn['services']['gluetun']['ports'], ['${MARTINFLIX_DOWNLOAD_BIND}:9091:9091'])
        media = self.root / 'media'
        media.mkdir()
        plan = self.plan(media_path=str(media), download_client='transmission', download_network='direct',
                         transmission_peer_port=51413)
        self.assertEqual(plan['ports']['transmission_peer'], 51413)
        self.assertIn(('transmission_peer', '0.0.0.0', 51413), plan['bindings'])

    def test_transmission_settings_carry_the_reference_choices(self):
        settings = json.loads(install.TRANSMISSION_SETTINGS.read_text())
        self.assertEqual((settings['ratio-limit'], settings['ratio-limit-enabled']), (0.0, True))
        self.assertEqual((settings['encryption'], settings['blocklist-enabled'], settings['utp-enabled']), (2, True, True))
        self.assertEqual(settings['rpc-password'], '')
        self.assertTrue(settings['incomplete-dir-enabled'])

    def test_media_check_covers_the_download_category_folders(self):
        media = self.root / 'media'
        (media / 'downloads/complete').mkdir(parents=True)
        (media / 'downloads/complete/sonarr').symlink_to(self.root)
        with self.assertRaisesRegex(ValueError, 'cannot be a link'):
            self.plan(media_path=str(media))

    def test_bazarr_needs_media_and_gets_a_port(self):
        with self.assertRaisesRegex(ValueError, 'Bazarr saves subtitles'):
            self.plan(enable_bazarr=True)
        media = self.root / 'media'
        media.mkdir()
        plan = self.plan(media_path=str(media), enable_bazarr=True, dashboard_bind='127.0.0.1')
        self.assertEqual(plan['downloads'], ['bazarr'])
        self.assertEqual(plan['ports']['bazarr'], 6767)
        self.assertTrue(plan['bootstrapping_prowlarr'])

    def test_legacy_download_flag_keeps_its_three_apps(self):
        media = self.root / 'media'
        media.mkdir()
        with patch.object(install.Path, 'is_char_device', return_value=True):
            plan = self.plan(media_path=str(media), enable_downloads=True)
        self.assertEqual(plan['downloads'], ['transmission', 'prowlarr', 'flaresolverr'])

    def test_transmission_requires_media(self):
        with self.assertRaisesRegex(ValueError, 'media directory'):
            self.plan(download_client='transmission')

    def test_flaresolverr_requires_prowlarr(self):
        with self.assertRaisesRegex(ValueError, 'enable-prowlarr'):
            self.plan(enable_flaresolverr=True)

    def test_download_route_can_change_on_update(self):
        previous = {'download_apps': ['prowlarr'], 'download_network': 'direct'}
        with patch.object(install.Path, 'is_char_device', return_value=True):
            plan = self.plan(previous, enable_vpn=True, download_network='vpn')
        self.assertEqual((plan['vpn'], plan['download_network']), (True, 'vpn'))
        with patch.object(install.Path, 'is_char_device', return_value=True):
            plan = self.plan(previous, enable_vpn=True)
        self.assertEqual(plan['download_network'], 'direct', 'adding the gateway alone keeps the current route')

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


class BazarrTests(unittest.TestCase):
    def test_reads_settings_from_bazarr_yaml(self):
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder)
            self.assertTrue(install.bazarr_needs_login(state))
            (state / 'config').mkdir()
            (state / 'config/config.yaml').write_text(
                "analytics:\n  enabled: false\nauth:\n  apikey: 0123abcd\n  password: ''\n  type: null\n"
                "  username: ''\nbackup:\n  day: 6\n")
            self.assertEqual(install.bazarr_setting(state, 'auth', 'apikey'), '0123abcd')
            self.assertTrue(install.bazarr_needs_login(state))
            (state / 'config/config.yaml').write_text("auth:\n  apikey: 0123abcd\n  type: form\n")
            self.assertFalse(install.bazarr_needs_login(state))

    def fields(self, **options):
        api = SimpleNamespace(saved=[])
        api.save = api.saved.extend
        configure.configure_bazarr(api, {'sonarr': 'skey', 'radarr': 'rkey'}, **options)
        return api.saved

    def test_connects_to_sonarr_and_radarr_every_run(self):
        with patch('builtins.print'):
            fields = dict(self.fields())
        self.assertEqual((fields['settings-sonarr-ip'], fields['settings-sonarr-port'], fields['settings-sonarr-apikey']),
                         ('sonarr', '8989', 'skey'))
        self.assertEqual(fields['settings-radarr-apikey'], 'rkey')
        self.assertNotIn('languages-profiles', fields)

    def test_seeds_reference_choices_once(self):
        with patch('builtins.print'):
            fields = self.fields(seed=True, opensubtitles=('someone', 'secret'))
        providers = [value for name, value in fields if name == 'settings-general-enabled_providers']
        self.assertIn('opensubtitlescom', providers)
        self.assertEqual([value for name, value in fields if name == 'languages-enabled'], ['en', 'fr', 'pb', 'es', 'ea'])
        profiles = json.loads(dict(fields)['languages-profiles'])
        self.assertEqual([(p['profileId'], p['name'], [i['language'] for i in p['items']]) for p in profiles],
                         [(1, 'All Languages', ['ea', 'pb', 'en', 'fr']), (2, 'Not-English', ['pb', 'ea'])])
        self.assertEqual(dict(fields)['settings-opensubtitlescom-username'], 'someone')
        self.assertEqual([value for name, value in fields if name == 'settings-general-language_equals'], ['spa:spl', 'spl:spa'])
        self.assertEqual(dict(fields)['subzero-remove_HI'], 'true')

    def test_skipping_the_opensubtitles_login_leaves_it_off(self):
        with patch('builtins.print'):
            fields = self.fields(seed=True)
        self.assertNotIn('opensubtitlescom', [value for name, value in fields if name == 'settings-general-enabled_providers'])
        self.assertNotIn('settings-opensubtitlescom-username', dict(fields))


class RouteChangeTests(unittest.TestCase):
    """Moving the download apps behind the VPN, or back, repoints every saved connection."""

    def setUp(self):
        self.puts, self.deletes, self.posts = [], [], []

    def api(self, records):
        def request(route, payload=None, method=None):
            if method == 'PUT':
                self.puts.append((route, payload))
            elif method == 'DELETE':
                self.deletes.append(route)
            elif payload is not None:
                self.posts.append((route, payload))
            else:
                return records.get(route, [])
        return SimpleNamespace(name='App', request=request)

    def test_transmission_connection_and_mapping_follow_the_route(self):
        api = self.api({
            'downloadclient': [{'id': 3, 'name': 'MartinFlix Transmission', 'implementation': 'Transmission', 'fields': [
                {'name': 'host', 'value': 'transmission'}, {'name': 'username', 'value': 'admin'},
                {'name': 'password', 'value': '********'}]}],
            'remotepathmapping': [{'id': 5, 'host': 'transmission', 'remotePath': '/downloads/', 'localPath': '/data/downloads/'},
                                  {'id': 6, 'host': 'nas', 'remotePath': '/downloads/', 'localPath': '/elsewhere/'}],
        })
        configure.sync_transmission_client(api, 'gluetun', 'shared')
        configure.ensure_download_path(api, 'gluetun')
        route, payload = self.puts[0]
        self.assertEqual(route, 'downloadclient/3?forceSave=true')
        self.assertEqual({field['name']: field['value'] for field in payload['fields']},
                         {'host': 'gluetun', 'username': 'admin', 'password': 'shared'})
        self.assertEqual(self.deletes, ['remotepathmapping/5'], 'only the old route\'s mapping goes')
        self.assertEqual(self.posts, [('remotepathmapping', {'host': 'gluetun', 'remotePath': '/downloads/', 'localPath': '/data/downloads/'})])

    def test_prowlarr_urls_and_flaresolverr_proxy_follow_the_route(self):
        prowlarr = self.api({
            'applications': [{'id': 1, 'implementation': 'Sonarr', 'fields': [{'name': 'prowlarrUrl', 'value': 'http://prowlarr:9696'}]},
                             {'id': 2, 'implementation': 'Radarr', 'fields': [{'name': 'prowlarrUrl', 'value': 'http://gluetun:9696'}]},
                             {'id': 3, 'implementation': 'Lidarr', 'fields': [{'name': 'prowlarrUrl', 'value': 'http://prowlarr:9696'}]}],
            'indexerproxy': [{'id': 4, 'implementation': 'FlareSolverr', 'fields': [{'name': 'host', 'value': 'http://flaresolverr:8191/'}]},
                             {'id': 5, 'implementation': 'FlareSolverr', 'fields': [{'name': 'host', 'value': 'http://custom:8191/'}]}],
        })
        configure.sync_prowlarr_routes(prowlarr, 'gluetun', True)
        self.assertEqual([route for route, _ in self.puts], ['applications/1?forceSave=true', 'indexerproxy/4?forceSave=true'])
        self.assertEqual(self.puts[1][1]['fields'][0]['value'], 'http://127.0.0.1:8191/')


class VpnDiagnosisTests(unittest.TestCase):
    def diagnose(self, log):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        secrets = Path(folder.name)
        for name in ('openvpn_user', 'openvpn_password'):
            (secrets / name).write_text('saved')
        printed = []
        with patch.object(install.subprocess, 'run', return_value=SimpleNamespace(stdout=log)), \
                patch('builtins.print', side_effect=lambda *args, **kwargs: printed.append(' '.join(map(str, args)))):
            install.diagnose_vpn(['docker', 'compose'], {}, secrets)
        return secrets, '\n'.join(printed)

    def test_server_list_refresh_is_best_effort(self):
        with tempfile.TemporaryDirectory() as folder:
            calls, printed = [], []
            with patch.object(install.subprocess, 'run', side_effect=lambda command, **kwargs: calls.append(command) or
                              SimpleNamespace(returncode=1)), \
                    patch('builtins.print', side_effect=lambda *args, **kwargs: printed.append(' '.join(map(str, args)))):
                install.refresh_vpn_servers(['docker', 'compose'], {}, Path(folder) / 'gluetun')
            self.assertEqual(calls[0][-6:], ['gluetun', 'update', '-enduser', '-providers', 'nordvpn'][-6:])
            self.assertIn('list built into its image', printed[-1])
            self.assertTrue((Path(folder) / 'gluetun').is_dir())

    def test_unreachable_servers_suggest_tcp(self):
        _, output = self.diagnose('gluetun-1  | WARN [openvpn] TLS Error: TLS key negotiation failed to occur within 60 seconds')
        self.assertIn('--vpn-protocol tcp', output)

    def test_rejected_login_clears_the_saved_credentials(self):
        secrets, output = self.diagnose('gluetun-1  | 2026-11-20T13:30:01Z ERROR [openvpn] AUTH: Received control message: AUTH_FAILED')
        self.assertIn('service credentials', output)
        self.assertFalse((secrets / 'openvpn_user').exists())
        self.assertFalse((secrets / 'openvpn_password').exists())

    def test_other_failures_keep_the_credentials(self):
        secrets, output = self.diagnose('gluetun-1  | INFO [healthcheck] program has been unhealthy for 6s: restarting VPN')
        self.assertIn('Gluetun log', output)
        self.assertTrue((secrets / 'openvpn_user').exists())


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
