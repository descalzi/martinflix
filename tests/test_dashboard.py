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
            ('http://radarr:7878', '/api/v3/downloadclient/7?forceSave=true'),
            ('http://prowlarr:9696', '/api/v1/downloadclient/7?forceSave=true')])
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

        def fake_json(connection, endpoint, payload=None, method=None, timeout=5):
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

    def test_a_slow_answer_still_counts_when_the_title_was_added(self):
        choices = (('base', 'key'), self.profiles, self.folders, self.profiles[1], self.folders[0])
        calls = []

        def slow(connection, endpoint, payload=None, method=None, timeout=5):
            calls.append((endpoint, method, timeout))
            if method == 'POST':
                raise TimeoutError('timed out')
            if endpoint.startswith('/api/v3/series?tvdbId='):
                return [{'title': 'Slow Horses', 'tvdbId': 1234}] if added else []
            return [dict(self.series)]
        with patch.object(server, 'add_choices', return_value=choices), patch.object(server, 'arr_json', side_effect=slow):
            added = True
            self.assertTrue(server.add_title('sonarr', {'id': 1234})['added'])
            self.assertIn(('/api/v3/series', 'POST', 60), calls)
            added = False
            with self.assertRaises(TimeoutError):
                server.add_title('sonarr', {'id': 1234})

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


class DeleteTitleTests(unittest.TestCase):
    def delete(self, kind, request, item):
        calls = []

        def fake_json(connection, endpoint, payload=None, method=None, timeout=5):
            calls.append((endpoint, method))
            return None if method == 'DELETE' else item
        with patch.object(server, 'arr_connection', return_value=('base', 'key')), \
                patch.object(server, 'arr_json', side_effect=fake_json):
            return server.delete_title(kind, request), calls

    def test_keeps_files_unless_asked(self):
        result, calls = self.delete('sonarr', {'id': 7}, {'id': 7, 'title': 'Slow Horses'})
        self.assertEqual(calls[-1], ('/api/v3/series/7?deleteFiles=false&addImportListExclusion=false', 'DELETE'))
        self.assertEqual(result, {'deleted': True, 'title': 'Slow Horses', 'files': False})

    def test_deletes_a_films_files_when_asked(self):
        result, calls = self.delete('radarr', {'id': 3, 'files': True}, {'id': 3, 'title': 'Hamnet'})
        self.assertEqual(calls[-1], ('/api/v3/movie/3?deleteFiles=true&addImportExclusion=false', 'DELETE'))
        self.assertTrue(result['files'])

    def test_refuses_bad_requests_and_missing_titles(self):
        for request in ({'id': 'x'}, {'id': 0}, {'id': 7, 'files': 'yes'}):
            with self.assertRaises(server.AddError, msg=request):
                self.delete('sonarr', request, {'id': 7})
        with self.assertRaises(server.AddError):
            self.delete('sonarr', {'id': 7}, {'id': 8})
        gone = urllib.error.HTTPError('u', 404, 'Not Found', {}, io.BytesIO())
        with patch.object(server, 'arr_connection', return_value=('base', 'key')), \
                patch.object(server, 'arr_json', side_effect=gone), self.assertRaises(server.AddError):
            server.delete_title('radarr', {'id': 3})


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
            return self.respond({'result': 'success', 'arguments': {'current-stats': {'secondsActive': 7200}}})

        server.TRANSMISSION_SESSION['id'] = ''
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
        for page in ('library', 'services', 'backups', 'settings'):
            status, body = self.request('/' + page)
            self.assertEqual(status, 200)
            self.assertIn(f'data-page="{page}"'.encode(), body)
        status, body = self.request('/')
        self.assertIn(b'data-page="overview"', body)

    def test_the_discover_menu_item_is_in_the_page_from_the_start(self):
        with patch.object(server, 'tmdb_key', return_value='key'):
            self.assertNotIn(b'id="nav-discover" hidden', self.request('/library')[1])
        with patch.object(server, 'tmdb_key', return_value=''):
            self.assertIn(b'id="nav-discover" hidden', self.request('/library')[1])

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

    def test_a_password_change_asks_the_controller_to_apply_it_to_transmission(self):
        secret = tempfile.NamedTemporaryFile('w', delete=False)
        secret.write('old password')
        secret.close()
        self.addCleanup(os.unlink, secret.name)
        def listing(*args, **kwargs):
            response = MagicMock()
            response.__enter__.return_value = io.BytesIO(b'[]')
            return response
        with patch.dict('os.environ', {'MARTINFLIX_TRANSMISSION_PASSWORD_FILE': secret.name}), \
                patch.object(server, 'synced_apps', return_value=['transmission']), \
                patch.object(server, 'installer_downloads', return_value=(set(), False, False)), \
                patch.object(server, 'arr_connection', return_value=('http://sonarr:8989', 'key')), \
                patch.object(server, 'arr_open', side_effect=listing), \
                patch.object(server, 'controller_request', return_value={'ok': True, 'restarted': True}) as controller:
            server.sync_app_passwords('a brand new password')
        self.assertEqual(Path(secret.name).read_text(), 'a brand new password')
        controller.assert_called_once_with('/transmission-password', {}, timeout=300)

    def test_back_up_now_needs_the_pin_and_goes_to_the_controller(self):
        with patch.object(server, 'controller_request', return_value={'ok': True, 'started': True}) as controller:
            status, data = self.post('/api/backup', {'pin': '2468'})
        self.assertEqual(status, 200)
        self.assertIn('Backup started', data['message'])
        controller.assert_called_once_with('/backup', {'pin': '2468'}, timeout=30)
        with patch.object(server, 'controller_request') as controller:
            self.assertEqual(self.post('/api/backup', {'pin': 'abc'})[0], 400)
            self.assertEqual(self.post('/api/backup', {'pin': '2468'}, origin='http://evil.example')[0], 403)
            controller.assert_not_called()
        with patch.object(server, 'controller_request', side_effect=server.ControllerError(409, 'A backup is already running.')):
            self.assertEqual(self.post('/api/backup', {'pin': '2468'}), (409, {'error': 'A backup is already running.'}))

    def test_martinflix_containers_get_their_own_section(self):
        helpers = server.helper_services({
            'dashboard': {'state': 'running', 'startedAt': '2026-11-20T08:00:00Z', 'dependents': [], 'canUpdate': False},
            'controller': {'state': 'running', 'startedAt': '2026-11-20T08:00:00Z', 'dependents': [], 'canUpdate': False},
            'transmission-auth': {'state': 'missing'}, 'sonarr': {'state': 'running', 'dependents': []}})
        self.assertEqual([item['name'] for item in helpers], ['dashboard', 'controller'])
        self.assertFalse(helpers[0]['control']['canUpdate'])

    def test_a_dashboard_restart_says_it_is_on_its_way(self):
        self.assertEqual(server.control_message({'action': 'restart', 'service': 'dashboard', 'deferred': True, 'followed': []}),
                         'Dashboard is restarting. This can take a few seconds.')

    def test_docker_fills_in_uptime_and_stopped_containers(self):
        services = [{'name': 'nordvpn', 'state': 'reachable', 'status': 'Responded to health check'},
                    {'name': 'radarr', 'state': 'unreachable', 'status': 'Health check did not respond'},
                    {'name': 'sonarr', 'state': 'reachable', 'status': 'ok', 'startedAt': '2026-11-20T09:00:00Z'}]
        server.apply_control_status(services, {
            'gluetun': {'state': 'running', 'startedAt': '2026-11-20T08:00:00Z', 'dependents': ['transmission', 'transmission-auth']},
            'radarr': {'state': 'exited', 'exitCode': 137, 'dependents': []},
            'sonarr': {'state': 'running', 'startedAt': '2026-11-20T08:59:00Z', 'dependents': []}})
        self.assertEqual(services[0]['startedAt'], '2026-11-20T08:00:00Z')
        self.assertEqual(services[0]['control'], {'followers': ['transmission-daemon'], 'canUpdate': True},
                         'helper sidecars stay out of the list')
        self.assertEqual((services[1]['state'], services[1]['status']), ('exited', 'Exited (exit code 137)'))
        self.assertEqual(services[2]['startedAt'], '2026-11-20T09:00:00Z', 'an app\'s own start time wins')


class SystemTests(unittest.TestCase):
    post = ControlTests.post
    request = RouteTests.request

    def test_updating_and_saving_settings_need_the_pin_and_go_to_the_controller(self):
        with patch.object(server, 'controller_request', return_value={'ok': True, 'started': True}) as controller:
            self.assertEqual(self.post('/api/system/update', {'pin': '2468'})[0], 200)
            controller.assert_called_with('/release/update', {'pin': '2468'}, timeout=60)
            status, _ = self.post('/api/settings', {'pin': '2468', 'settings': {'discover': 'off'}, 'secrets': {'tmdbKey': 'k' * 32}})
            self.assertEqual(status, 200)
            controller.assert_called_with('/settings', {'pin': '2468', 'settings': {'discover': 'off'},
                                                        'secrets': {'tmdbKey': 'k' * 32}}, timeout=60)
            self.assertEqual(self.post('/api/system/check', {})[0], 200)
            controller.assert_called_with('/release/check', {}, timeout=60)
        with patch.object(server, 'controller_request') as controller:
            self.assertEqual(self.post('/api/system/update', {'pin': 'abc'})[0], 400)
            self.assertEqual(self.post('/api/settings', {'pin': '2468'}, origin='http://evil.example')[0], 403)
            controller.assert_not_called()
        with patch.object(server, 'controller_request', side_effect=server.ControllerError(409, 'MartinFlix is already updating.')):
            self.assertEqual(self.post('/api/system/update', {'pin': '2468'}), (409, {'error': 'MartinFlix is already updating.'}))

    def test_a_restore_names_one_backup_and_needs_the_pin(self):
        with patch.object(server, 'controller_request', return_value={'ok': True, 'started': True}) as controller:
            self.assertEqual(self.post('/api/restore', {'pin': '2468', 'backup': '2026-11-22_010000'})[0], 200)
            controller.assert_called_with('/restore', {'pin': '2468', 'backup': '2026-11-22_010000'}, timeout=60)
        with patch.object(server, 'controller_request') as controller:
            self.assertEqual(self.post('/api/restore', {'pin': '2468', 'backup': '../../etc'})[0], 400)
            self.assertEqual(self.post('/api/restore', {'pin': '24', 'backup': '2026-11-22_010000'})[0], 400)
            controller.assert_not_called()

    def test_the_version_page_lists_what_changed_from_github(self):
        release = {'current': {'version': '1', 'revision': 'a' * 40, 'channel': 'edge'},
                   'available': {'version': '2', 'revision': 'b' * 40}, 'updater': None}
        compare = {'status': 'ahead', 'total_commits': 2, 'html_url': 'https://github.com/descalzi/martinflix/compare/x',
                   'commits': [{'sha': 'c' * 40, 'commit': {'message': 'First change\n\nDetails', 'committer': {'date': '2026-11-21T10:00:00Z'}}},
                               {'sha': 'd' * 40, 'commit': {'message': 'Second change', 'committer': {'date': '2026-11-22T10:00:00Z'}}}]}
        response = MagicMock()
        response.__enter__.return_value = io.BytesIO(json.dumps(compare).encode())
        server.CHANGES_CACHE.clear()
        with patch.object(server, 'controller_request', return_value=release), \
                patch.object(server.ARR_OPENER, 'open', return_value=response) as opened:
            status, body = self.request('/api/system')
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertEqual([commit['title'] for commit in data['changes']['commits']], ['Second change', 'First change'])
        self.assertIn(f'compare/{"a" * 40}...{"b" * 40}', opened.call_args[0][0].full_url)
        self.assertIsNone(server.release_changes('not-a-commit', 'b' * 40))

    def test_the_backups_page_says_why_there_is_no_schedule(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        with patch.dict(os.environ, {'MARTINFLIX_BACKUP_DIR': folder.name}):
            with patch.object(server, 'controller_request', return_value={'services': {}, 'backups': {'next': 'x'}}):
                self.assertEqual(server.backup_status()['schedule'], {'next': 'x'})
                self.assertIsNone(server.backup_status()['scheduleProblem'])
            with patch.object(server, 'controller_request', return_value={'services': {}}):
                self.assertIn('older than backups', server.backup_status()['scheduleProblem'])
            with patch.object(server, 'controller_request', side_effect=server.ControllerError(502, 'Docker status is unavailable.')):
                self.assertIn('Docker status is unavailable.', server.backup_status()['scheduleProblem'])
            with patch.object(server, 'controller_request', return_value=None):
                self.assertIn('doesn’t have', server.backup_status()['scheduleProblem'])

    def test_the_vpn_test_compares_the_exit_with_this_networks_own_address(self):
        check = {'installed': True, 'probe': 'transmission', 'exit': {'ip': '185.242.6.10', 'city': 'Prague'}, 'problem': None}
        with patch.object(server, 'controller_request', return_value=check) as controller, \
                patch.object(server, 'home_address', return_value={'ip': '86.1.2.3', 'org': 'AS5089 Virgin Media'}):
            status, body = self.post('/api/vpn-test', {})
        self.assertEqual(status, 200)
        self.assertEqual(body['verdict'], 'protected')
        controller.assert_called_with('/vpn-test', {}, timeout=90)
        self.assertEqual(server.vpn_verdict(check, {'ip': '185.242.6.10'}), 'exposed')
        self.assertEqual(server.vpn_verdict({'exit': None, 'problem': 'x'}, {'ip': '86.1.2.3'}), 'down')
        self.assertEqual(server.vpn_verdict(check, None), 'protected', 'the VPN is up even if our own address is unknown')

    def test_sign_ins_survive_a_restart_until_the_password_changes(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        state = Path(folder.name) / 'password-hash'
        first = server.load_session_key(state)
        self.assertEqual(server.load_session_key(state), first)
        self.assertEqual(oct((Path(folder.name) / 'session-key').stat().st_mode & 0o777), '0o600')
        self.assertNotEqual(server.new_session_key(state), first)
        self.assertNotEqual(server.load_session_key(state), first)
        self.assertEqual(len(server.load_session_key(None)), 32)


class DiscoverTests(unittest.TestCase):
    def setUp(self):
        server.TMDB_CACHE.clear()

    def test_martinflix_key_is_the_default_until_replaced_or_turned_off(self):
        with patch.dict('os.environ', {}, clear=True):
            self.assertEqual(server.tmdb_key(), server.DEFAULT_TMDB_KEY)
        secret = tempfile.NamedTemporaryFile('w', delete=False)
        secret.write('f' * 32)
        secret.close()
        self.addCleanup(os.unlink, secret.name)
        with patch.dict('os.environ', {'MARTINFLIX_TMDB_KEY_FILE': secret.name}, clear=True):
            self.assertEqual(server.tmdb_key(), 'f' * 32)
        with patch.dict('os.environ', {'MARTINFLIX_TMDB_KEY_FILE': secret.name, 'MARTINFLIX_DISCOVER': 'off'}, clear=True):
            self.assertEqual(server.tmdb_key(), '')

    def test_tmdb_entries_keep_only_safe_fields(self):
        film = server.tmdb_item({'id': 42, 'title': ' Dune ', 'release_date': '2026-12-18', 'poster_path': '/abc.jpg',
                                 'overview': 'Sand.'}, 'radarr')
        self.assertEqual(film, {'id': 42, 'kind': 'radarr', 'title': 'Dune', 'date': '2026-12-18', 'year': 2026,
                                'poster': 'https://image.tmdb.org/t/p/w342/abc.jpg', 'overview': 'Sand.'})
        odd = server.tmdb_item({'id': 7, 'name': 'Show', 'first_air_date': 'soon', 'poster_path': '//evil.example/x.jpg'}, 'sonarr')
        self.assertEqual((odd['date'], odd['poster']), (None, None))
        self.assertIsNone(server.tmdb_item({'id': 'x', 'title': 'No'}, 'radarr'))

    def test_rows_use_the_region_and_mark_titles_already_in_the_library(self):
        calls = []
        def fake(path, **params):
            calls.append((path, params))
            if path.startswith('/trending/tv') or path == '/discover/tv':
                return {'results': [{'id': 5, 'name': 'Owned show'}, {'id': 6, 'name': 'New show'}]}
            return {'results': [{'id': 1, 'title': 'Owned film'}, {'id': 2, 'title': 'New film'}]}
        owned = {'radarr': {1}, 'sonarr': {5}}
        with patch.dict('os.environ', {'MARTINFLIX_DISCOVER_REGION': 'gb'}), \
                patch.object(server, 'tmdb_cached', side_effect=fake), \
                patch.object(server, 'library_tmdb_ids', side_effect=lambda kind: owned[kind]):
            data = server.discover_rows()
        self.assertEqual(data['region'], 'GB')
        self.assertEqual([row['title'] for row in data['rows']],
                         ['Trending films', 'Trending series', 'Coming to UK cinemas', 'New films on UK streaming',
                          'New series on UK streaming'])
        cinemas = next(params for path, params in calls if params.get('region'))
        self.assertEqual((cinemas['region'], cinemas['with_release_type']), ('GB', '2|3'))
        self.assertEqual([(item['title'], item['inLibrary']) for item in data['rows'][1]['items']],
                         [('Owned show', True), ('New show', False)])

    def test_keys_and_read_tokens_are_sent_the_way_tmdb_expects(self):
        seen = []
        def fake_open(request, timeout):
            seen.append(request)
            response = MagicMock()
            response.__enter__.return_value = io.BytesIO(b'{}')
            return response
        for key in ('a' * 32, 'eyJ' + 'b' * 40):
            secret = tempfile.NamedTemporaryFile('w', delete=False)
            secret.write(key)
            secret.close()
            self.addCleanup(os.unlink, secret.name)
            with patch.dict('os.environ', {'MARTINFLIX_TMDB_KEY_FILE': secret.name}), \
                    patch.object(server.ARR_OPENER, 'open', side_effect=fake_open):
                server.tmdb_get('/trending/movie/week', language='en-GB')
        self.assertIn('api_key=' + 'a' * 32, seen[0].full_url)
        self.assertNotIn('api_key', seen[1].full_url)
        self.assertEqual(seen[1].get_header('Authorization'), 'Bearer eyJ' + 'b' * 40)

    def test_series_from_discover_are_found_in_sonarr_by_their_tvdb_id(self):
        terms = []
        with patch.object(server, 'arr_connection', return_value=('http://sonarr:8989', 'key')), \
                patch.object(server, 'tmdb_cached', return_value={'tvdb_id': 81189}), \
                patch.object(server, 'arr_json', side_effect=lambda connection, route: terms.append(route) or []):
            server.search_titles('sonarr', 'tmdb:1396')
        self.assertIn('term=tvdb%3A81189', terms[0])
        with patch.object(server, 'arr_connection', return_value=('http://sonarr:8989', 'key')), \
                patch.object(server, 'tmdb_cached', return_value={'tvdb_id': None}):
            with self.assertRaisesRegex(server.AddError, 'TheTVDB'):
                server.search_titles('sonarr', 'tmdb:1396')



class PlexProbeTests(unittest.TestCase):
    def test_an_installed_plex_gets_its_own_section(self):
        environment = {'MARTINFLIX_ARR_COMPOSE_NETWORK': '1', 'MARTINFLIX_DOWNLOAD_APPS': '', 'MARTINFLIX_DOWNLOAD_GATEWAY': '',
                       'MARTINFLIX_PLEX_INSTALLED': '1', 'MARTINFLIX_PLEX_URL': 'http://host.docker.internal:32400'}
        seen = []
        def fake(url, timeout):
            seen.append(url)
            response = MagicMock()
            response.__enter__.return_value.status = 200
            return response
        with patch.dict('os.environ', environment, clear=True), patch.object(server.ARR_OPENER, 'open', side_effect=fake):
            status = server.container_status()
        self.assertIn('http://host.docker.internal:32400/identity', seen)
        self.assertEqual([group['name'] for group in status['groups']], ['Media server', 'Library apps'])
        self.assertEqual([service['name'] for service in status['groups'][0]['services']], ['plex'])

class LiveTests(unittest.TestCase):
    def setUp(self):
        server.QUEUE_CACHE.update(at=0.0, value={})
        server.PLEX_CACHE.clear()

    def test_torrents_are_named_after_the_titles_they_belong_to(self):
        torrents = [
            {'hashString': 'AAA', 'name': 'Slow.Horses.S05E03.1080p', 'status': 4, 'percentDone': 0.624, 'rateDownload': 3120000,
             'eta': 410, 'sizeWhenDone': 2400000000, 'error': 0},
            {'hashString': 'bbb', 'name': 'Some.Season.Pack', 'status': 4, 'percentDone': 0.5, 'rateDownload': 900, 'eta': -1,
             'sizeWhenDone': 10, 'error': 0},
            {'hashString': 'ccc', 'name': 'linux.iso', 'status': 3, 'percentDone': 0, 'rateDownload': 0, 'eta': -1, 'sizeWhenDone': 4, 'error': 0},
            {'hashString': 'ddd', 'name': 'Old.Film', 'status': 6, 'percentDone': 1, 'rateDownload': 0, 'eta': -1, 'sizeWhenDone': 9, 'error': 0},
            {'hashString': 'eee', 'name': 'Stopped', 'status': 0, 'percentDone': 0.2, 'rateDownload': 0, 'eta': -1, 'sizeWhenDone': 9, 'error': 0}]
        def rpc(method, arguments=None, behind_vpn=None):
            if method == 'session-stats':
                return {'downloadSpeed': 3121000, 'uploadSpeed': 50000,
                        'cumulative-stats': {'downloadedBytes': 1000, 'uploadedBytes': 1840}}
            return {'torrents': torrents}
        queues = {
            '/api/v3/queue?pageSize=200&includeSeries=true&includeEpisode=true': {'records': [
                {'downloadId': 'AAA', 'seriesId': 7, 'series': {'title': 'Slow Horses'},
                 'episode': {'seasonNumber': 5, 'episodeNumber': 3, 'title': 'Hello Goodbye'}},
                *({'downloadId': 'BBB', 'seriesId': 8, 'series': {'title': 'The Traitors'},
                   'episode': {'seasonNumber': 4, 'episodeNumber': number, 'title': 'x'}} for number in range(1, 10))]},
            '/api/v3/queue?pageSize=200&includeMovie=true': {'records': []}}
        with patch.object(server, 'transmission_url', return_value='http://transmission:9091/transmission/rpc'), \
                patch.object(server, 'transmission_rpc', side_effect=rpc), \
                patch.object(server, 'arr_connection', return_value=('http://arr', 'key')), \
                patch.object(server, 'arr_json', side_effect=lambda connection, route: queues[route]):
            data = server.live_transfer()
        self.assertEqual((data['downloading'], data['queued'], data['seeding'], data['paused'], data['ratio']), (2, 1, 1, 1, 1.84))
        first, second, third = data['downloads']
        self.assertEqual((first['title'], first['detail'], first['percent'], first['poster']),
                         ('Slow Horses', 'S05E03 · Hello Goodbye', 62.4, '/api/posters/sonarr/7'))
        self.assertEqual((second['title'], second['detail'], second['eta']), ('The Traitors', 'Season 4 · 9 episodes', None))
        self.assertEqual((third['title'], third['matched'], third['state']), ('linux.iso', False, 'queued'))

    def test_without_transmission_live_says_so(self):
        with patch.object(server, 'transmission_url', return_value=None):
            self.assertEqual(server.live_transfer(), {'available': False})

    def test_plex_sessions_become_titles_users_and_progress(self):
        container = {'Metadata': [
            {'type': 'episode', 'grandparentTitle': 'Severance', 'parentIndex': 2, 'index': 7, 'title': 'Chikhai Bardo',
             'viewOffset': 1290000, 'duration': 3000000, 'grandparentThumb': '/library/metadata/55/thumb/1700000000',
             'User': {'title': 'Martin'}, 'Player': {'title': 'Living room TV', 'state': 'playing', 'local': True}},
            {'type': 'movie', 'title': 'Wicked', 'year': 2024, 'thumb': 'https://evil.example/x.jpg',
             'User': {'title': 'Ana'}, 'Player': {'product': 'Plex for iOS', 'state': 'paused'},
             'TranscodeSession': {'videoDecision': 'transcode'}}]}
        with patch.object(server, 'plex_json', return_value=container):
            first, second = server.plex_sessions()
        self.assertEqual((first['title'], first['detail'], first['percent'], first['stream'], first['user']),
                         ('Severance', 'S02E07 · Chikhai Bardo', 43, 'Direct play', 'Martin'))
        self.assertEqual(first['poster'], '/api/plex-art?path=%2Flibrary%2Fmetadata%2F55%2Fthumb%2F1700000000')
        self.assertEqual((second['detail'], second['state'], second['stream'], second['poster']), ('2024', 'paused', 'Transcoding', None))

    def test_history_names_each_viewer(self):
        def plex_json(path, params=None, cache_seconds=0):
            if path == '/accounts':
                return {'Account': [{'id': 1, 'name': 'Martin'}, {'id': 3, 'name': 'Ana'}]}
            return {'Metadata': [{'type': 'movie', 'title': 'Dune: Part Two', 'year': 2024, 'accountID': 3, 'viewedAt': 1763800000},
                                 {'type': 'movie', 'title': 'Unknown', 'accountID': 99}]}
        with patch.object(server, 'plex_json', side_effect=plex_json):
            history = server.plex_history()
        self.assertEqual([(item['title'], item['user'], item['watchedAt']) for item in history],
                         [('Dune: Part Two', 'Ana', 1763800000), ('Unknown', 'Someone', None)])

    def test_only_plex_library_artwork_is_fetched(self):
        with patch.object(server, 'plex_configured', return_value=True), patch.object(server, 'plex_open') as plex:
            for path in ('/library/metadata/1/../../:/prefs', 'http://evil.example/x', '/library/metadata/1/thumb/2?x=1'):
                self.assertIsNone(server.plex_art(path))
            plex.assert_not_called()

    def test_the_speed_sampler_records_gaps_when_transmission_is_unreachable(self):
        server.SPEED_SAMPLES.clear()
        with patch.object(server, 'transmission_rpc', side_effect=[{'downloadSpeed': 500, 'uploadSpeed': 20}, OSError('down')]):
            server.sample_speeds_once()
            server.sample_speeds_once()
        self.assertEqual([sample[1:] for sample in server.SPEED_SAMPLES], [[500, 20], [None, None]])

@unittest.skipUnless(shutil.which('node'), 'Node.js is needed to test the setup wizard')
class WizardTests(unittest.TestCase):
    def test_wizard_command_builder(self):
        result = subprocess.run(['node', '--test', str(ROOT / 'tests/wizard.test.js')], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
