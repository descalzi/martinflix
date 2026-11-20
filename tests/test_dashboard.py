import io
import json
import os
import shutil
import tempfile
import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import server

ROOT = Path(__file__).resolve().parents[1]


class CatalogTests(unittest.TestCase):
    def test_catalog_matches_the_reference_stack_registry(self):
        registry = set()
        for line in (ROOT / 'deploy/hosts/martinflix/stacks.conf').read_text().splitlines():
            if not line.strip() or line.lstrip().startswith('#'):
                continue
            columns = [column.strip() for column in line.split('|')]
            registry.update(name for name in columns[4].split(',') if name)
        catalogued = {name for group in server.CATALOG['groups'] for name in group['services']}
        self.assertEqual(catalogued, registry)

    def test_every_catalogued_service_has_app_details(self):
        apps = server.CATALOG['apps']
        names = {name for group in server.CATALOG['groups'] for name in group['services']}
        self.assertFalse(names - apps.keys())
        for name, branch in server.CATALOG['branches'].items():
            self.assertIn(name, apps)
            self.assertFalse(set(branch['members']) - apps.keys())
        for app in apps.values():
            if app.get('icon'):
                self.assertTrue((ROOT / 'web/assets' / (app['icon'] + '.webp')).exists(), app['icon'])

    def test_web_links_come_from_the_catalog(self):
        status = {'available': True, 'groups': [{'name': 'Web services', 'services': [
            {'name': 'seerr', 'state': 'running', 'status': 'Up'},
            {'name': 'portainer', 'state': 'running', 'status': 'Up'},
            {'name': 'tailscale', 'state': 'running', 'status': 'Up'},
        ]}]}
        with patch.object(server, 'container_status', return_value=status):
            services = server.service_status()['groups'][0]['services']
        self.assertEqual(services[0]['web'], {'port': 5055, 'scheme': 'http', 'path': '/', 'localOnly': False})
        self.assertEqual(services[1]['web']['scheme'], 'https')
        self.assertNotIn('web', services[2])


class PasswordSyncTests(unittest.TestCase):
    def test_password_change_reaches_every_installer_app(self):
        with tempfile.NamedTemporaryFile('w', delete=False) as secret:
            secret.write('old-transmission-password-that-is-long')
        environment = {
            'MARTINFLIX_ARR_COMPOSE_NETWORK': '1', 'MARTINFLIX_DOWNLOAD_APPS': 'transmission,prowlarr,flaresolverr',
            'MARTINFLIX_DOWNLOAD_GATEWAY': '', 'MARTINFLIX_TRANSMISSION_PASSWORD_FILE': secret.name,
            'MARTINFLIX_SONARR_KEY': 's', 'MARTINFLIX_RADARR_KEY': 'r', 'MARTINFLIX_PROWLARR_KEY': 'p',
            'MARTINFLIX_SONARR_URL': 'http://sonarr:8989', 'MARTINFLIX_RADARR_URL': 'http://radarr:7878',
        }
        writes = []

        def fake_open(connection, endpoint, *, payload=None, method=None):
            base = connection[0]
            if method == 'PUT':
                writes.append((base, endpoint, payload))
                body = b''
            elif endpoint.endswith('/config/host'):
                username = '' if 'prowlarr' in base else 'admin'
                body = json.dumps({'id': 1, 'username': username, 'authenticationMethod': 'forms' if username else 'none'}).encode()
            else:
                body = json.dumps([{'id': 7, 'name': 'MartinFlix Transmission', 'implementation': 'Transmission',
                                    'fields': [{'name': 'username', 'value': 'martinflix'},
                                               {'name': 'password', 'value': '********'}]},
                                   {'id': 8, 'name': 'Other', 'implementation': 'Transmission', 'fields': []}]).encode()
            return MagicMock(__enter__=lambda self: io.BytesIO(body), __exit__=lambda *args: False)

        try:
            with patch.dict('os.environ', environment, clear=True), patch.object(server, 'arr_open', side_effect=fake_open):
                self.assertEqual(server.synced_apps(), ['sonarr', 'radarr', 'prowlarr', 'transmission'])
                self.assertEqual(server.app_list(), 'Sonarr, Radarr, Prowlarr and Transmission')
                server.sync_app_passwords('new shared password')
            self.assertEqual(Path(secret.name).read_text(), 'new shared password')
        finally:
            os.unlink(secret.name)
        hosts = {base: payload for base, endpoint, payload in writes if 'config/host' in endpoint}
        self.assertEqual(set(hosts), {'http://sonarr:8989', 'http://radarr:7878', 'http://prowlarr:9696'})
        self.assertTrue(all(payload['password'] == 'new shared password' for payload in hosts.values()))
        self.assertEqual(hosts['http://prowlarr:9696']['username'], 'admin')
        self.assertEqual(hosts['http://prowlarr:9696']['authenticationMethod'], 'forms')
        clients = [(base, endpoint, payload) for base, endpoint, payload in writes if 'downloadclient' in endpoint]
        self.assertEqual([(base, endpoint) for base, endpoint, _ in clients], [
            ('http://sonarr:8989', '/api/v3/downloadclient/7?forceSave=true'),
            ('http://radarr:7878', '/api/v3/downloadclient/7?forceSave=true')])
        self.assertEqual({field['name']: field['value'] for field in clients[0][2]['fields']},
                         {'username': 'admin', 'password': 'new shared password'})

    def test_existing_host_dashboard_syncs_nothing(self):
        with patch.dict('os.environ', {}, clear=True):
            self.assertEqual(server.synced_apps(), [])


class AddTitleTests(unittest.TestCase):
    profiles = [{'id': 1, 'name': 'Any'}, {'id': 4, 'name': 'MartinFlix HD (720p/1080p)'}, {'id': 6, 'name': 'Ultra-HD'}]
    folders = [{'path': '/data/TV Shows', 'accessible': True}, {'path': '/archive/TV', 'accessible': True}]
    series = {'title': 'Slow Horses', 'tvdbId': 1234, 'seasons': [{'seasonNumber': n, 'monitored': True} for n in range(5)]}

    def add(self, kind, request, lookup):
        posted = {}

        def fake_json(connection, endpoint, payload=None, method=None):
            if method == 'POST':
                posted.update(payload)
                return payload
            return lookup

        choices = (('base', 'key'), self.profiles, self.folders, self.profiles[1], self.folders[0])
        with patch.object(server, 'add_choices', return_value=choices), patch.object(server, 'arr_json', side_effect=fake_json):
            result = server.add_title(kind, request)
        return result, posted

    def monitored(self, posted):
        return [season['seasonNumber'] for season in posted['seasons'] if season['monitored']]

    def test_defaults_match_the_old_behaviour(self):
        _, posted = self.add('sonarr', {'id': 1234}, [dict(self.series)])
        self.assertEqual(posted['qualityProfileId'], 4)
        self.assertEqual(posted['rootFolderPath'], '/data/TV Shows')
        self.assertEqual(posted['addOptions'], {'searchForMissingEpisodes': True, 'searchForCutoffUnmetEpisodes': False, 'monitor': 'all'})

    def test_chosen_profile_folder_and_seasons(self):
        result, posted = self.add('sonarr', {'id': 1234, 'profile': 6, 'folder': '/archive/TV', 'monitor': 'custom',
                                             'seasons': [2, 4], 'search': False}, [dict(self.series)])
        self.assertEqual((posted['qualityProfileId'], posted['rootFolderPath']), (6, '/archive/TV'))
        self.assertEqual(self.monitored(posted), [2, 4])
        self.assertNotIn('monitor', posted['addOptions'])
        self.assertFalse(posted['addOptions']['searchForMissingEpisodes'])
        self.assertFalse(result['searching'])

    def test_latest_and_first_season_skip_specials(self):
        _, posted = self.add('sonarr', {'id': 1234, 'monitor': 'latest'}, [dict(self.series)])
        self.assertEqual(self.monitored(posted), [4])
        _, posted = self.add('sonarr', {'id': 1234, 'monitor': 'first'}, [dict(self.series)])
        self.assertEqual(self.monitored(posted), [1])

    def test_film_availability(self):
        _, posted = self.add('radarr', {'id': 99, 'availability': 'inCinemas'}, {'title': 'Hamnet', 'tmdbId': 99})
        self.assertEqual(posted['minimumAvailability'], 'inCinemas')
        self.assertEqual(posted['addOptions'], {'searchForMovie': True})

    def test_choices_outside_what_the_app_offers_are_refused(self):
        for request in ({'id': 1234, 'profile': 77}, {'id': 1234, 'folder': '/etc'}, {'id': 1234, 'monitor': 'pilot'},
                        {'id': 1234, 'monitor': 'custom', 'seasons': [9]}, {'id': 1234, 'monitor': 'custom', 'seasons': []},
                        {'id': 1234, 'search': 'yes'}, {'id': 'x'}):
            with self.assertRaises(server.AddError, msg=request):
                self.add('sonarr', request, [dict(self.series)])
        with self.assertRaises(server.AddError):
            self.add('radarr', {'id': 99, 'availability': 'tomorrow'}, {'title': 'Hamnet', 'tmdbId': 99})


class RouteTests(unittest.TestCase):
    def request(self, path):
        handler = object.__new__(server.Handler)
        handler.server = SimpleNamespace(admin_state_file=None, admin_user='admin', admin_password='long test password',
                                         password_setup_required=False)
        handler.path = path
        handler.headers = {'Authorization': 'Basic YWRtaW46bG9uZyB0ZXN0IHBhc3N3b3Jk'}
        handler.wfile = io.BytesIO()
        sent = {}
        handler.send_response = lambda code, *args: sent.setdefault('status', code)
        handler.send_header = lambda *args: None
        handler.end_headers = lambda: None
        handler.send_error = lambda code, *args: sent.setdefault('status', code)
        handler.do_GET()
        return sent.get('status'), handler.wfile.getvalue()

    def test_each_page_has_a_route(self):
        for page in ('library', 'services', 'backups'):
            status, body = self.request('/' + page)
            self.assertEqual(status, 200)
            self.assertIn(f'data-page="{page}"'.encode(), body)
        status, body = self.request('/')
        self.assertIn(b'data-page="overview"', body)

    def test_fonts_are_served_and_unknown_paths_are_not(self):
        self.assertEqual(self.request('/fonts/figtree.woff2')[0], 200)
        self.assertEqual(self.request('/fonts/../server.py')[0], 404)
        self.assertEqual(self.request('/nope')[0], 404)


@unittest.skipUnless(shutil.which('node'), 'Node.js is needed to test the setup wizard')
class WizardTests(unittest.TestCase):
    def test_wizard_command_builder(self):
        result = subprocess.run(['node', '--test', str(ROOT / 'tests/wizard.test.js')], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
