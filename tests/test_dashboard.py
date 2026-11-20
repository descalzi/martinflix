import io
import json
import os
import shutil
import tempfile
import urllib.error
import urllib.parse
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
            'MARTINFLIX_ARR_COMPOSE_NETWORK': '1', 'MARTINFLIX_DOWNLOAD_APPS': 'transmission,prowlarr,flaresolverr,bazarr',
            'MARTINFLIX_DOWNLOAD_GATEWAY': '', 'MARTINFLIX_TRANSMISSION_PASSWORD_FILE': secret.name,
            'MARTINFLIX_SONARR_KEY': 's', 'MARTINFLIX_RADARR_KEY': 'r', 'MARTINFLIX_PROWLARR_KEY': 'p',
            'MARTINFLIX_BAZARR_KEY': 'b',
            'MARTINFLIX_SONARR_URL': 'http://sonarr:8989', 'MARTINFLIX_RADARR_URL': 'http://radarr:7878',
        }
        writes = []

        def fake_open(connection, endpoint, *, payload=None, method=None):
            base = connection[0]
            if method == 'PUT':
                writes.append((base, endpoint, payload))
                body = b''
            elif endpoint == '/api/system/settings':
                body = json.dumps({'auth': {'type': None}}).encode()
            elif endpoint.endswith('/config/host'):
                username = '' if 'prowlarr' in base else 'admin'
                body = json.dumps({'id': 1, 'username': username, 'authenticationMethod': 'forms' if username else 'none'}).encode()
            else:
                body = json.dumps([{'id': 7, 'name': 'MartinFlix Transmission', 'implementation': 'Transmission',
                                    'fields': [{'name': 'username', 'value': 'martinflix'},
                                               {'name': 'password', 'value': '********'}]},
                                   {'id': 8, 'name': 'Other', 'implementation': 'Transmission', 'fields': []}]).encode()
            return MagicMock(__enter__=lambda self: io.BytesIO(body), __exit__=lambda *args: False)

        bazarr_posts = []

        def fake_bazarr(request, timeout):
            bazarr_posts.append((request.full_url, urllib.parse.parse_qs(request.data.decode()), request.get_header('X-api-key')))
            return MagicMock(__enter__=lambda self: self, __exit__=lambda *args: False)

        try:
            with patch.dict('os.environ', environment, clear=True), patch.object(server, 'arr_open', side_effect=fake_open), \
                    patch.object(server.ARR_OPENER, 'open', side_effect=fake_bazarr):
                self.assertEqual(server.synced_apps(), ['sonarr', 'radarr', 'prowlarr', 'bazarr', 'transmission'])
                self.assertEqual(server.app_list(), 'Sonarr, Radarr, Prowlarr, Bazarr and Transmission')
                server.sync_app_passwords('new shared password')
            self.assertEqual(Path(secret.name).read_text(), 'new shared password')
            self.assertEqual(bazarr_posts, [('http://bazarr:6767/api/system/settings', {
                'settings-auth-type': ['form'], 'settings-auth-username': ['admin'],
                'settings-auth-password': ['new shared password']}, 'b')])
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


class UptimeTests(unittest.TestCase):
    def test_docker_status_gives_uptime(self):
        self.assertEqual(server.docker_uptime('Up 3 days (healthy)'), '3 days')
        self.assertEqual(server.docker_uptime('Up About an hour'), 'About an hour')
        self.assertEqual(server.docker_uptime('Up 2 minutes (health: starting)'), '2 minutes')
        self.assertIsNone(server.docker_uptime('Exited (1) 3 hours ago'))

    def respond(self, payload):
        return MagicMock(__enter__=lambda self: io.BytesIO(json.dumps(payload).encode()), __exit__=lambda *args: False)

    def test_apps_report_when_they_started(self):
        environment = {'MARTINFLIX_SONARR_KEY': 's', 'MARTINFLIX_BAZARR_KEY': 'b'}
        with patch.dict('os.environ', environment, clear=True):
            with patch.object(server, 'arr_open', return_value=self.respond({'startTime': '2026-11-18T09:30:00Z'})) as opened:
                self.assertEqual(server.app_started_at('sonarr', False), '2026-11-18T09:30:00+00:00')
            self.assertEqual(opened.call_args.args[1], '/api/v3/system/status')
            with patch.object(server, 'arr_open', return_value=self.respond({'data': {'start_time': 1763458200.5}})):
                self.assertEqual(server.app_started_at('bazarr', False), '2025-11-18T09:30:00.500000+00:00')
            with patch.object(server, 'arr_open', side_effect=OSError):
                self.assertIsNone(server.app_started_at('sonarr', False))
        with patch.dict('os.environ', {}, clear=True):
            self.assertIsNone(server.app_started_at('radarr', False), 'no key, no lookup')

    def test_transmission_session_age_uses_the_shared_login(self):
        with tempfile.NamedTemporaryFile('w', delete=False) as secret:
            secret.write('shared password')
        environment = {'MARTINFLIX_ARR_COMPOSE_NETWORK': '1', 'MARTINFLIX_TRANSMISSION_PASSWORD_FILE': secret.name}
        conflict = urllib.error.HTTPError('http://transmission:9091/transmission/rpc', 409, 'Conflict',
                                          {'X-Transmission-Session-Id': 'abc'}, io.BytesIO())
        seen = []

        def fake(request, timeout):
            seen.append((request.full_url, request.get_header('Authorization'), request.get_header('X-transmission-session-id')))
            if len(seen) == 1:
                raise conflict
            return self.respond({'arguments': {'current-stats': {'secondsActive': 7200}}})

        try:
            with patch.dict('os.environ', environment, clear=True), patch.object(server.ARR_OPENER, 'open', side_effect=fake):
                self.assertEqual(server.transmission_seconds_active(False), 7200)
        finally:
            os.unlink(secret.name)
        self.assertEqual(seen[1], ('http://transmission:9091/transmission/rpc', 'Basic YWRtaW46c2hhcmVkIHBhc3N3b3Jk', 'abc'))


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



class ControlTests(unittest.TestCase):
    def post(self, path, payload, origin='http://dash:8765'):
        handler = object.__new__(server.Handler)
        handler.server = SimpleNamespace(admin_state_file=None, admin_user='admin', admin_password='long test password',
                                         password_setup_required=False)
        handler.path = path
        body = json.dumps(payload).encode()
        handler.headers = __import__('email.message', fromlist=['Message']).Message()
        for key, value in {'Authorization': 'Basic YWRtaW46bG9uZyB0ZXN0IHBhc3N3b3Jk', 'Host': 'dash:8765',
                           'Origin': origin, 'Content-Type': 'application/json', 'Content-Length': str(len(body))}.items():
            handler.headers[key] = value
        handler.rfile = io.BytesIO(body)
        handler.wfile = io.BytesIO()
        sent = {}
        handler.send_response = lambda code, *args: sent.setdefault('status', code)
        handler.send_header = lambda *args: None
        handler.end_headers = lambda: None
        handler.send_error = lambda code, *args: sent.setdefault('status', code)
        handler.do_POST()
        raw = handler.wfile.getvalue()
        return sent.get('status'), json.loads(raw) if raw else None

    def test_requests_reach_the_controller_under_compose_names(self):
        calls = []
        def fake(path, payload=None, timeout=5):
            calls.append((path, payload))
            return {'ok': True, 'action': 'restart', 'service': 'gluetun',
                    'followed': ['transmission', 'transmission-auth', 'prowlarr']}
        with patch.object(server, 'controller_request', side_effect=fake):
            status, data = self.post('/api/control/restart', {'service': 'nordvpn', 'pin': '2468'})
        self.assertEqual(status, 200)
        self.assertEqual(calls, [('/restart', {'service': 'gluetun', 'pin': '2468'})])
        self.assertEqual(data['message'], 'Gluetun restarted, then Transmission and Prowlarr.')

    def test_bad_input_and_other_sites_never_reach_the_controller(self):
        with patch.object(server, 'controller_request') as controller:
            self.assertEqual(self.post('/api/control/restart', {'service': 'nordvpn', 'pin': '24'})[0], 400)
            self.assertEqual(self.post('/api/control/update', {'service': 'docker', 'pin': '2468'})[0], 400)
            self.assertEqual(self.post('/api/control/restart', {'service': 'sonarr', 'pin': '2468'},
                                       origin='http://evil.example')[0], 403)
            controller.assert_not_called()

    def test_controller_refusals_are_passed_on(self):
        with patch.object(server, 'controller_request', side_effect=server.ControllerError(423, 'Too many wrong PINs.')):
            self.assertEqual(self.post('/api/control/update', {'service': 'sonarr', 'pin': '1111'}),
                             (423, {'error': 'Too many wrong PINs.'}))

    def form(self, fields, token='token'):
        handler = object.__new__(server.Handler)
        handler.server = SimpleNamespace(admin_state_file=Path('/nonexistent'), password_setup_required=False,
                                         password_setup_token='token')
        handler.path = '/change-pin'
        body = urllib.parse.urlencode({'token': token, **fields}).encode()
        handler.headers = __import__('email.message', fromlist=['Message']).Message()
        for key, value in {'Host': 'dash:8765', 'Origin': 'http://dash:8765', 'Content-Length': str(len(body)),
                           'Content-Type': 'application/x-www-form-urlencoded'}.items():
            handler.headers[key] = value
        handler.authorized = lambda: True
        handler.rfile = io.BytesIO(body)
        handler.wfile = io.BytesIO()
        sent = {}
        handler.send_response = lambda code, *args: sent.setdefault('status', code)
        handler.send_header = lambda *args: None
        handler.end_headers = lambda: None
        handler.send_error = lambda code, *args: sent.setdefault('status', code)
        handler.do_POST()
        return sent.get('status'), handler.wfile.getvalue().decode()

    def test_pin_changes_go_to_the_controller_with_the_current_pin(self):
        with patch.object(server, 'controller_request', return_value={'ok': True}) as controller:
            status, page = self.form({'current': '2468', 'pin': '9753', 'confirm': '9753'})
        self.assertEqual(status, 200)
        self.assertIn('PIN changed', page)
        controller.assert_called_once_with('/pin', {'pin': '2468', 'new': '9753'}, timeout=30)

    def test_pin_changes_are_checked_before_they_leave_the_dashboard(self):
        with patch.object(server, 'controller_request') as controller:
            self.assertEqual(self.form({'current': '2468', 'pin': '9753', 'confirm': '9754'})[0], 400)
            self.assertEqual(self.form({'current': '2468', 'pin': '97', 'confirm': '97'})[0], 400)
            self.assertEqual(self.form({'current': '2468', 'pin': '9753', 'confirm': '9753'}, token='stale')[0], 403)
            controller.assert_not_called()
        with patch.object(server, 'controller_request', side_effect=server.ControllerError(403, 'Wrong PIN. 4 tries left.')):
            status, page = self.form({'current': '1111', 'pin': '9753', 'confirm': '9753'})
        self.assertEqual(status, 403)
        self.assertIn('Wrong PIN. 4 tries left.', page)

    def test_docker_fills_in_uptime_and_stopped_containers(self):
        services = [{'name': 'nordvpn', 'state': 'reachable', 'status': 'Responded to health check'},
                    {'name': 'radarr', 'state': 'unreachable', 'status': 'Health check did not respond'},
                    {'name': 'sonarr', 'state': 'reachable', 'status': 'ok', 'startedAt': '2026-11-20T09:00:00Z'}]
        server.apply_control_status(services, {
            'gluetun': {'state': 'running', 'startedAt': '2026-11-20T08:00:00Z', 'dependents': ['transmission', 'transmission-auth']},
            'radarr': {'state': 'exited', 'exitCode': 137, 'dependents': []},
            'sonarr': {'state': 'running', 'startedAt': '2026-11-20T08:59:00Z', 'dependents': []}})
        self.assertEqual(services[0]['startedAt'], '2026-11-20T08:00:00Z')
        self.assertEqual(services[0]['control'], {'followers': ['transmission-daemon']}, 'helper sidecars stay hidden')
        self.assertEqual((services[1]['state'], services[1]['status']), ('exited', 'Exited (exit code 137)'))
        self.assertEqual(services[2]['startedAt'], '2026-11-20T09:00:00Z', 'an app\'s own start time wins')

@unittest.skipUnless(shutil.which('node'), 'Node.js is needed to test the setup wizard')
class WizardTests(unittest.TestCase):
    def test_wizard_command_builder(self):
        result = subprocess.run(['node', '--test', str(ROOT / 'tests/wizard.test.js')], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
