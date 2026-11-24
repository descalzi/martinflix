"""Audiobooks: Audiobookshelf and Listenarr, installed together by setup."""
import argparse
import io
import json
import os
import sys
import tarfile
import tempfile
import urllib.error
import threading
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'deploy/bootstrap'))
sys.path.insert(0, str(ROOT / 'deploy/controller'))
sys.path.insert(0, str(ROOT))
import backups  # noqa: E402
import configure  # noqa: E402
import install  # noqa: E402
import releases  # noqa: E402
import restore  # noqa: E402
import server  # noqa: E402


def arguments(**overrides):
    values = vars(install.build_parser().parse_args([]))
    values.update(overrides)
    return argparse.Namespace(**values)


class PlanTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.state = self.root / 'state'
        self.media = self.root / 'media'
        self.media.mkdir()

    def plan(self, previous=None, **overrides):
        return install.plan_installation(arguments(**overrides), self.state, previous or {}, 'martinflix-test',
                                         self.root / 'downloads.json')

    def test_audiobooks_need_the_media_directory(self):
        with self.assertRaisesRegex(ValueError, 'Audiobooks are kept in your media directory'):
            self.plan(enable_audiobooks=True)

    def test_both_apps_join_the_stack_with_their_own_folder_and_ports(self):
        plan = self.plan(media_path=str(self.media), enable_audiobooks=True, dashboard_bind='127.0.0.1')
        self.assertIn(str(install.COMPOSE_FILE.with_name('compose.audiobooks.yaml')), plan['compose'])
        self.assertEqual((plan['ports']['audiobookshelf'], plan['ports']['listenarr']), (13378, 4545))
        self.assertEqual(plan['options']['audiobooks_folder'], 'Audiobooks')
        self.assertTrue(plan['options']['audiobooks'])
        self.assertEqual(plan['environment']['MARTINFLIX_AUDIOBOOKS_PATH'], str(self.media / 'Audiobooks'))
        self.assertIn('audiobookshelf', plan['app_dirs'])
        self.assertIn('listenarr', plan['app_dirs'])
        self.assertTrue(plan['bootstrapping_audiobooks'], 'no login yet, so they start on loopback')

    def test_an_existing_folder_is_used_whatever_its_capitals(self):
        (self.media / 'AudioBooks').mkdir()
        plan = self.plan(media_path=str(self.media), enable_audiobooks=True)
        self.assertEqual(plan['options']['audiobooks_folder'], 'AudioBooks')
        # Once chosen, the folder stays, even if another appears.
        (self.media / 'audiobooks').mkdir()
        again = self.plan(plan['options'])
        self.assertEqual(again['options']['audiobooks_folder'], 'AudioBooks')

    def test_once_installed_they_stay_installed_and_keep_their_flag(self):
        previous = {'media_path': str(self.media), 'audiobooks': True, 'audiobooks_folder': 'Audiobooks'}
        plan = self.plan(previous)
        self.assertTrue(plan['audiobooks'])
        self.assertIn('--enable-audiobooks', install.keep_flags(plan['options']))
        self.assertEqual(install.current_settings(plan['options'])['Audiobooks'], 'Audiobookshelf and Listenarr')
        self.assertEqual(install.current_settings({})['Audiobooks'], 'Not installed')

    def test_a_bad_saved_folder_is_refused(self):
        with self.assertRaisesRegex(ValueError, 'audiobooks folder is not valid'):
            self.plan({'media_path': str(self.media), 'audiobooks': True, 'audiobooks_folder': '../etc'})

    def test_they_open_on_the_lan_once_both_have_a_login(self):
        for folder in ('', 'listenarr', 'audiobookshelf', 'audiobookshelf/config'):
            (self.state / folder).mkdir(mode=0o700, exist_ok=True)
        listenarr = self.state / 'listenarr'
        (listenarr / 'config.json').write_text(json.dumps({'ApiKey': 'k', 'AuthenticationRequired': 'true'}))
        database = self.state / 'audiobookshelf/config/absdatabase.sqlite'
        database.write_bytes(b'SQLite format 3')
        previous = {'media_path': str(self.media), 'audiobooks': True, 'audiobooks_folder': 'Audiobooks',
                    'audiobookshelf_admin': True}
        self.assertFalse(self.plan(previous)['bootstrapping_audiobooks'])
        (listenarr / 'config.json').write_text(json.dumps({'ApiKey': 'k', 'AuthenticationRequired': 'false'}))
        self.assertTrue(self.plan(previous)['bootstrapping_audiobooks'])

    def test_listenarr_settings_are_read_whatever_their_case(self):
        folder = self.root / 'listenarr'
        folder.mkdir()
        (folder / 'config.json').write_text(json.dumps({'apiKey': ' abc ', 'authenticationRequired': 'Enabled'}))
        self.assertEqual(install.wait_for_listenarr_key(folder), 'abc')
        self.assertFalse(install.listenarr_needs_login(folder))
        self.assertTrue(install.listenarr_needs_login(self.root / 'missing'))


class AudiobookshelfServer(BaseHTTPRequestHandler):
    """Enough of Audiobookshelf's API for setup: first-run account, sign-in, API keys and libraries."""
    state = None

    def log_message(self, *args):
        pass

    def reply(self, status, body=None):
        data = json.dumps(body).encode() if body is not None else b'OK'
        self.send_response(status)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def body(self):
        return json.loads(self.rfile.read(int(self.headers.get('Content-Length') or 0)) or b'{}')

    def signed_in(self):
        return self.headers.get('Authorization') in (f'Bearer {key}' for key in self.state['keys'] | {'access'})

    def do_GET(self):
        state = self.state
        if self.path == '/status':
            return self.reply(200, {'isInit': state['root'] is not None})
        if not self.signed_in():
            return self.reply(401)
        if self.path == '/api/me':
            return self.reply(200, {'id': 'root-id', 'username': state['root'][0]})
        if self.path == '/api/libraries':
            return self.reply(200, {'libraries': state['libraries']})
        self.reply(404)

    def do_POST(self):
        state = self.state
        body = self.body()
        state['posts'].append((self.path, body))
        if self.path == '/init':
            if state['root'] is not None:
                return self.reply(500)
            state['root'] = (body['newRoot']['username'], body['newRoot']['password'])
            return self.reply(200)
        if self.path == '/login':
            if (body.get('username'), body.get('password')) != state['root']:
                return self.reply(401)
            return self.reply(200, {'user': {'id': 'root-id', 'accessToken': 'access'}})
        if not self.signed_in():
            return self.reply(401)
        if self.path == '/api/api-keys':
            state['keys'].add('new-key')
            return self.reply(200, {'apiKey': {'apiKey': 'new-key', 'name': body['name']}})
        if self.path == '/api/libraries':
            state['libraries'].append({'name': body['name'], 'folders': body['folders']})
            return self.reply(200, state['libraries'][-1])
        self.reply(404)


class AudiobookshelfTests(unittest.TestCase):
    def setUp(self):
        self.state = {'root': None, 'keys': set(), 'libraries': [], 'posts': []}
        handler = type('Handler', (AudiobookshelfServer,), {'state': self.state})
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_address[1]

    def ensure(self, key=None, password='temporary', region='GB'):
        lines = []
        result = configure.ensure_audiobookshelf('127.0.0.1', self.port, key, lambda: password, region,
                                                 log=lambda line, **_: lines.append(line))
        return result, lines

    def test_a_new_server_gets_the_shared_admin_a_key_and_a_library(self):
        key, lines = self.ensure()
        self.assertEqual(key, 'new-key')
        self.assertEqual(self.state['root'], ('admin', 'temporary'))
        self.assertEqual(self.state['libraries'], [{'name': 'Audiobooks', 'folders': [{'fullPath': '/audiobooks'}]}])
        library = next(body for path, body in self.state['posts'] if path == '/api/libraries')
        self.assertEqual((library['mediaType'], library['provider']), ('book', 'audible.uk'))
        self.assertIn('   Audiobookshelf: admin account created.', lines)

    def test_a_working_key_changes_nothing(self):
        self.ensure()
        self.state['posts'].clear()
        key, lines = self.ensure(key='new-key', password='no longer the password')
        self.assertEqual(key, 'new-key')
        self.assertEqual(self.state['posts'], [], 'no new account, key or library')
        self.assertEqual(lines, [])

    def test_a_lost_key_is_replaced_by_signing_in(self):
        self.ensure()
        self.state['keys'].clear()
        key, _ = self.ensure(key='gone')
        self.assertEqual(key, 'new-key')

    def test_a_lost_key_with_a_changed_password_says_what_to_do(self):
        self.ensure()
        self.state['keys'].clear()
        with self.assertRaisesRegex(ValueError, 'Settings → API Keys'):
            self.ensure(key='gone', password='changed')

    def test_the_audible_store_follows_the_discover_country(self):
        self.assertEqual(configure.audible_store('us'), 'us')
        self.assertEqual(configure.audible_store('DE'), 'de')
        self.assertEqual(configure.audible_store('CZ'), 'us')
        self.ensure(region='US')
        library = next(body for path, body in self.state['posts'] if path == '/api/libraries')
        self.assertEqual(library['provider'], 'audible')


class FakeListenarr:
    """Listenarr's API as a dictionary of routes."""

    def __init__(self, **state):
        self.name = 'Listenarr'
        self.calls = []
        self.state = {'startup': {'ApiKey': 'k', 'AuthenticationRequired': None}, 'settings': {'version': 3},
                      'rootfolders': [], 'download-clients': [], 'remotepathmappings': [], 'indexers': [], **state}

    def request(self, route, payload=None, method=None):
        self.calls.append((method or ('POST' if payload is not None else 'GET'), route, payload))
        state = self.state
        if route == 'configuration/startupconfig':
            if payload is not None:
                state['startup'].update(payload)
            return state['startup']
        if route == 'configuration/settings':
            if payload is not None:
                state['settings'] = payload
            return dict(state['settings'])
        if route == 'indexers/prowlarr/import':
            added = [{'id': 10 + n, 'url': f'{payload["url"]}/{n}/api'} for n in (1, 2)
                     if not any(item['url'] == f'{payload["url"]}/{n}/api' for item in state['indexers'])]
            state['indexers'] += added
            return {'addedCount': len(added), 'total': 5}
        if route.startswith('indexers/') and method == 'DELETE':
            state['indexers'] = [item for item in state['indexers'] if str(item['id']) != route.split('/')[1]]
            return None
        if payload is not None:
            if route == 'download-clients':
                saved = {**payload, 'id': payload.get('id') or 'client-1'}
                state[route] = [item for item in state[route] if item.get('id') != saved['id']] + [saved]
                return {'id': saved['id']}
            state[route].append(payload)
            return payload
        return [dict(item) for item in state[route]]


class ListenarrTests(unittest.TestCase):
    def test_login_is_set_once(self):
        api = FakeListenarr()
        self.assertTrue(configure.ensure_listenarr_login(api, lambda: 'temporary'))
        self.assertEqual((api.state['settings']['adminUsername'], api.state['settings']['adminPassword']), ('admin', 'temporary'))
        self.assertEqual(api.state['settings']['version'], 3, 'posted with the version it was read at')
        self.assertEqual(api.state['startup']['AuthenticationRequired'], 'true')
        api.calls.clear()
        self.assertFalse(configure.ensure_listenarr_login(api, lambda: self.fail('not asked again')))
        self.assertEqual(api.calls, [('GET', 'configuration/startupconfig', None)])

    def test_root_folder_transmission_and_prowlarr_on_the_vpn_route(self):
        api = FakeListenarr()
        lines = []
        configure.configure_listenarr(api, root='/data/AudioBooks', region='GB', transmission_password='shared',
                                      downloads_behind_vpn=True, prowlarr_key='pkey', seed=True,
                                      log=lambda line, **_: lines.append(line))
        self.assertEqual(api.state['rootfolders'], [{'name': 'Audiobooks', 'path': '/data/AudioBooks', 'isDefault': True}])
        self.assertEqual(api.state['settings']['defaultSearchRegion'], 'uk')
        [client] = api.state['download-clients']
        self.assertEqual((client['name'], client['type'], client['host'], client['port']),
                         ('MartinFlix Transmission', 'transmission', 'gluetun', 9091))
        self.assertEqual((client['username'], client['password']), ('admin', 'shared'))
        self.assertEqual(client['downloadPath'], '/downloads/complete/listenarr')
        self.assertEqual(client['settings'], {'category': 'listenarr'})
        self.assertEqual(api.state['remotepathmappings'], [{
            'downloadClientId': 'client-1', 'name': 'MartinFlix downloads', 'remotePath': '/downloads/',
            'localPath': '/data/downloads/'}])
        self.assertEqual([item['url'] for item in api.state['indexers']],
                         ['http://gluetun:9696/1/api', 'http://gluetun:9696/2/api'])
        self.assertIn('   Listenarr: 2 of Prowlarr’s indexers added for audiobooks.', lines)

    def test_a_second_run_changes_only_what_moved(self):
        api = FakeListenarr()
        options = dict(root='/data/Audiobooks', region='GB', transmission_password='shared', prowlarr_key='pkey')
        configure.configure_listenarr(api, downloads_behind_vpn=True, log=lambda *a, **k: None, **options)
        configure.configure_listenarr(api, downloads_behind_vpn=False, log=lambda *a, **k: None, **options)
        self.assertEqual(len(api.state['rootfolders']), 1)
        self.assertEqual(len(api.state['remotepathmappings']), 1)
        [client] = api.state['download-clients']
        self.assertEqual(client['host'], 'transmission', 'follows the download route')
        self.assertEqual([item['url'] for item in api.state['indexers']],
                         ['http://prowlarr:9696/1/api', 'http://prowlarr:9696/2/api'],
                         'indexers at the old route are replaced')
        self.assertNotIn('defaultSearchRegion', api.state['settings'], 'the store is only set once')

    def test_no_audiobook_indexers_is_reported(self):
        api = FakeListenarr()
        api.request = (lambda original: lambda route, payload=None, method=None: {'addedCount': 0, 'total': 3}
                       if route == 'indexers/prowlarr/import' else original(route, payload, method))(api.request)
        lines = []
        configure.configure_listenarr(api, root='/data/Audiobooks', region='GB', prowlarr_key='pkey',
                                      log=lambda line, **_: lines.append(line))
        self.assertTrue(any('none of Prowlarr’s indexers lists audiobooks' in line for line in lines))


class FakeStack:
    def __init__(self, state):
        self.state = state
        self.calls = []

    def config(self):
        return {'stateDir': str(self.state), 'backupDir': str(self.state.parent / 'backups'), 'host': 'test'}

    def controllable(self):
        return {'audiobookshelf': {}, 'listenarr': {}}

    def compose(self, *args, timeout=600):
        self.calls.append(args)
        if args[:2] == ('exec', '-T') and 'audiobookshelf' in args:
            folder = self.state / 'audiobookshelf/metadata/backups'
            folder.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(folder / '1764000000000.audiobookshelf', 'w') as archive:
                archive.writestr('absdatabase.sqlite', 'SQLite format 3 restored')
                archive.writestr('metadata-items/li_1/cover.jpg', 'jpeg')
                archive.writestr('metadata-authors/au_1.jpg', 'jpeg')
                archive.writestr('details', '1\n2.38.0')
        return SimpleNamespace(returncode=0, stdout='', stderr='')


class BackupAndRestoreTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.state = self.root / 'state'
        (self.state / 'dashboard-secrets').mkdir(parents=True)
        (self.state / 'dashboard-secrets/audiobookshelf-key').write_text('abs-key\n')
        listenarr = self.state / 'listenarr'
        (listenarr / 'database').mkdir(parents=True)
        (listenarr / 'config.json').write_text('{"ApiKey": "k"}')
        (listenarr / 'database/listenarr.db').write_text('SQLite format 3 listenarr')
        (listenarr / 'cache/images').mkdir(parents=True)
        (listenarr / 'cache/images/big.jpg').write_text('left out')

    def test_both_are_backed_up_and_restored(self):
        stack = FakeStack(self.state)
        taker = backups.Backups(stack, clock=lambda: 0, sleep=lambda _: None)
        manifest = taker.run('manual')
        self.assertEqual(manifest['errors'], [])
        self.assertEqual(set(manifest['apps']), {'audiobookshelf', 'listenarr', 'martinflix'})
        exec_call = next(call for call in stack.calls if call[0] == 'exec')
        self.assertIn('MARTINFLIX_KEY=abs-key', exec_call, 'the key goes in as an environment variable')
        self.assertIn(('pause', 'listenarr'), stack.calls)
        self.assertIn(('unpause', 'listenarr'), stack.calls)
        folder = taker.root() / os.listdir(taker.root())[0]
        with tarfile.open(folder / 'listenarr.tar.gz') as archive:
            self.assertEqual(sorted(archive.getnames()), ['config.json', 'database', 'database/listenarr.db'])

        # A machine with newer data gets the backup's back; what it had goes aside.
        (self.state / 'listenarr/database/listenarr.db').write_text('newer')
        (self.state / 'listenarr/database/listenarr.db-wal').write_text('wal')
        items = self.state / 'audiobookshelf/metadata/items/li_9'
        items.mkdir(parents=True)
        (items / 'cover.jpg').write_text('newer book')
        backup = restore.Backup(folder)
        aside = self.root / 'aside'
        restore.Restorer(self.state, aside, log=lambda *_: None).restore(backup)
        self.assertEqual((self.state / 'listenarr/database/listenarr.db').read_text(), 'SQLite format 3 listenarr')
        self.assertFalse((self.state / 'listenarr/database/listenarr.db-wal').exists())
        self.assertEqual((self.state / 'audiobookshelf/config/absdatabase.sqlite').read_text(), 'SQLite format 3 restored')
        self.assertTrue((self.state / 'audiobookshelf/metadata/items/li_1/cover.jpg').is_file())
        self.assertFalse((self.state / 'audiobookshelf/metadata/items/li_9').exists())
        self.assertTrue((aside / 'audiobookshelf/metadata/items/li_9/cover.jpg').is_file())
        self.assertEqual(restore.LABELS['audiobookshelf'], 'Audiobookshelf')

    def test_listenarr_is_unpaused_even_if_copying_fails(self):
        stack = FakeStack(self.state)
        taker = backups.Backups(stack, clock=lambda: 0, sleep=lambda _: None)
        with patch.object(backups.tarfile, 'open', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                taker.capture_listenarr(self.state, self.root)
        self.assertEqual(stack.calls[-1], ('unpause', 'listenarr'))


class ControllerSettingsTests(unittest.TestCase):
    def test_audiobooks_can_only_be_turned_on(self):
        self.assertEqual(releases.setup_arguments({'audiobooks': 'on'}), ['--enable-audiobooks'])
        with self.assertRaises(releases.UpdateError):
            releases.setup_arguments({'audiobooks': 'off'})


class DashboardTests(unittest.TestCase):
    environment = {'MARTINFLIX_ARR_COMPOSE_NETWORK': '1', 'MARTINFLIX_AUDIOBOOKS': '1', 'MARTINFLIX_DOWNLOAD_APPS': '',
                   'MARTINFLIX_SONARR_KEY': 's', 'MARTINFLIX_RADARR_KEY': 'r', 'MARTINFLIX_AUDIOBOOKSHELF_KEY': 'abs-key',
                   'MARTINFLIX_LISTENARR_KEY': 'l-key', 'MARTINFLIX_SONARR_URL': 'http://sonarr:8989',
                   'MARTINFLIX_RADARR_URL': 'http://radarr:7878'}

    def test_both_apps_get_the_shared_password(self):
        arr_writes, requests = [], []

        def fake_arr(connection, endpoint, *, payload=None, method=None):
            if method == 'PUT':
                arr_writes.append(connection[0])
                body = b''
            else:
                body = json.dumps({'id': 1, 'username': 'admin'}).encode()
            return MagicMock(__enter__=lambda self: io.BytesIO(body), __exit__=lambda *args: False)

        def fake_open(request, timeout):
            requests.append((request.get_method(), request.full_url, request.headers.get('Authorization'),
                             request.headers.get('X-api-key'), json.loads(request.data) if request.data else None))
            body = {'http://audiobookshelf:13378/api/me': {'id': 'root-id', 'username': 'admin'},
                    'http://listenarr:4545/api/v1/configuration/settings': {'version': 4}}.get(request.full_url, {})
            return MagicMock(__enter__=lambda self: io.BytesIO(json.dumps(body).encode()), __exit__=lambda *args: False)

        with patch.dict('os.environ', self.environment, clear=True), patch.object(server, 'arr_open', side_effect=fake_arr), \
                patch.object(server.ARR_OPENER, 'open', side_effect=fake_open):
            self.assertEqual(server.synced_apps(), ['sonarr', 'radarr', 'audiobookshelf', 'listenarr'])
            self.assertEqual(server.app_list(), 'Sonarr, Radarr, Audiobookshelf and Listenarr')
            server.sync_app_passwords('new shared password')
        self.assertEqual(arr_writes, ['http://sonarr:8989', 'http://radarr:7878'])
        writes = [entry for entry in requests if entry[0] != 'GET']
        self.assertEqual(writes, [
            ('PATCH', 'http://audiobookshelf:13378/api/users/root-id', 'Bearer abs-key', None,
             {'username': 'admin', 'password': 'new shared password'}),
            ('POST', 'http://listenarr:4545/api/v1/configuration/settings', None, 'l-key',
             {'version': 4, 'adminUsername': 'admin', 'adminPassword': 'new shared password'})])

    def test_both_apps_are_probed_in_their_own_group(self):
        def fake_open(url, timeout):
            return MagicMock(status=200, __enter__=lambda self: self, __exit__=lambda *args: False)

        with patch.dict('os.environ', self.environment, clear=True), patch.object(server.ARR_OPENER, 'open', side_effect=fake_open):
            status = server.container_status()
        groups = {group['name']: [service['name'] for service in group['services']] for group in status['groups']}
        self.assertEqual(groups['Audiobooks'], ['audiobookshelf', 'listenarr'])
        self.assertNotIn('listenarr', groups['Library apps'])
        for name in ('audiobookshelf', 'listenarr'):
            self.assertTrue((ROOT / 'web/assets' / f'{server.CATALOG["apps"][name]["icon"]}.webp').is_file())


if __name__ == '__main__':
    unittest.main()


class LibraryAndAddTests(unittest.TestCase):
    """The dashboard's Library and Add pages, for audiobooks through Listenarr."""
    environment = {'MARTINFLIX_ARR_COMPOSE_NETWORK': '1', 'MARTINFLIX_AUDIOBOOKS': '1', 'MARTINFLIX_DISCOVER_REGION': 'GB',
                   'MARTINFLIX_AUDIOBOOKSHELF_WEB_PORT': '13378', 'MARTINFLIX_LISTENARR_KEY': 'l', 'MARTINFLIX_AUDIOBOOKSHELF_KEY': 'a'}
    library = [
        {'id': 1, 'title': 'Project Hail Mary', 'authors': ['Andy Weir'], 'narrators': ['Ray Porter'], 'asin': 'B08G9PRS1K',
         'publishYear': '2021', 'series': None, 'monitored': True, 'status': 'quality-match', 'fileCount': 1, 'fileSize': 900_000_000},
        {'id': 2, 'title': 'The Way of Kings', 'authors': ['Brandon Sanderson'], 'asin': 'B003ZWFO7E', 'series': 'The Stormlight Archive',
         'seriesNumber': '1', 'monitored': True, 'status': 'no-file', 'fileCount': 0},
        {'id': 3, 'title': 'Dune', 'authors': ['Frank Herbert'], 'asin': 'bad', 'monitored': True, 'status': 'downloading'},
        {'id': 4, 'title': 'Old Book', 'authors': [], 'monitored': False, 'status': 'no-file'},
    ]

    def test_wanted_and_all_views(self):
        shelf = {('asin', 'B08G9PRS1K'): 'li_abc'}
        with patch.dict('os.environ', self.environment, clear=True):
            wanted = server.audiobook_items(self.library, shelf=shelf)
            everything = server.audiobook_items(self.library, everything=True, shelf=shelf)
        self.assertEqual([item['title'] for item in wanted], ['Dune', 'The Way of Kings'], 'downloading first, then by title')
        self.assertEqual([item['title'] for item in everything], ['Dune', 'Old Book', 'Project Hail Mary', 'The Way of Kings'])
        book = next(item for item in everything if item['id'] == 1)
        self.assertEqual(book['listen'], {'port': 13378, 'path': '/audiobookshelf/item/li_abc'})
        self.assertEqual(book['links'], {'audible': 'https://www.audible.co.uk/pd/B08G9PRS1K'})
        self.assertEqual(book['poster'], '/api/posters/listenarr/B08G9PRS1K')
        self.assertEqual((book['downloaded'], book['size'], book['year']), (True, 900_000_000, 2021))
        kings = next(item for item in everything if item['id'] == 2)
        self.assertEqual(kings['series'], 'The Stormlight Archive #1')
        self.assertNotIn('listen', kings)
        dune = next(item for item in everything if item['id'] == 3)
        self.assertEqual((dune['poster'], dune['links']), (None, {}), 'no cover or link without a real ASIN')

    def test_add_sends_what_the_search_showed(self):
        calls = []
        search = {'results': [{'asin': 'B08G9PRS1K', 'title': 'Project Hail Mary', 'authors': [{'name': 'Andy Weir'}],
                               'narrators': [{'name': 'Ray Porter'}], 'imageUrl': 'https://m.media-amazon.com/images/I/a.jpg',
                               'series': [], 'runtimeLengthMin': 970, 'releaseDate': '2021-05-04', 'publisher': 'Audible Studios'},
                              {'asin': 'nope', 'title': 'Skipped'}]}

        def fake_json(kind, endpoint, payload=None, method=None, timeout=10):
            calls.append((endpoint, payload, method))
            if endpoint.startswith('/api/v1/search/audible'):
                return search
            if endpoint == '/api/v1/library':
                return [{'asin': 'B003ZWFO7E'}]
            return {'message': 'added'}

        with patch.dict('os.environ', self.environment, clear=True), patch.object(server, 'audiobook_json', side_effect=fake_json):
            results = server.search_audiobooks('hail mary')
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0]['poster'], 'https://m.media-amazon.com/images/I/a.jpg')
            self.assertEqual((results[0]['runtime'], results[0]['year'], results[0]['added']), (970, 2021, False))
            self.assertIn('region=uk', calls[0][0])
            added = server.add_audiobook({'id': 'B08G9PRS1K', 'search': True})
        self.assertEqual(added, {'added': True, 'title': 'Project Hail Mary', 'searching': True})
        endpoint, payload, method = calls[-1]
        self.assertEqual((endpoint, method), ('/api/v1/library/add', 'POST'))
        self.assertEqual(payload['metadata']['authors'], ['Andy Weir'])
        self.assertEqual((payload['metadata']['runtime'], payload['metadata']['publishYear']), (970, '2021'))
        self.assertTrue(payload['autoSearch'])
        with self.assertRaises(server.AddError):
            server.add_audiobook({'id': 'not-an-asin'})

    def test_adding_a_book_already_there_says_so(self):
        server.AUDIOBOOK_SEARCHES['B0000000AA'] = {'asin': 'B0000000AA', 'title': 'Here Already'}

        def conflict(kind, endpoint, payload=None, method=None, timeout=10):
            raise urllib.error.HTTPError(endpoint, 409, 'Conflict', {}, io.BytesIO(b''))

        with patch.dict('os.environ', self.environment, clear=True), patch.object(server, 'audiobook_json', side_effect=conflict):
            with self.assertRaisesRegex(server.AddError, 'Here Already is already in Listenarr'):
                server.add_audiobook({'id': 'B0000000AA'})

    def test_delete_with_and_without_files(self):
        calls = []

        def fake_json(kind, endpoint, payload=None, method=None, timeout=10):
            calls.append((endpoint, method))
            return {'id': 2, 'title': 'The Way of Kings'} if method is None else None

        with patch.dict('os.environ', self.environment, clear=True), patch.object(server, 'audiobook_json', side_effect=fake_json):
            self.assertEqual(server.delete_audiobook({'id': 2, 'files': True})['files'], True)
            server.delete_audiobook({'id': 2})
        self.assertEqual(calls[1], ('/api/v1/library/2?deleteFiles=true&deleteFolder=true', 'DELETE'))
        self.assertEqual(calls[3], ('/api/v1/library/2?deleteFiles=false&deleteFolder=false', 'DELETE'))

    def test_no_results_from_audible_is_an_empty_list(self):
        def not_found(kind, endpoint, payload=None, method=None, timeout=10):
            raise urllib.error.HTTPError(endpoint, 404, 'Not Found', {}, io.BytesIO(b''))

        with patch.dict('os.environ', self.environment, clear=True), patch.object(server, 'audiobook_json', side_effect=not_found):
            self.assertEqual(server.search_audiobooks('zzzz'), [])

    def test_audible_covers_are_allowed_in_search_results(self):
        self.assertEqual(server.remote_poster('https://m.media-amazon.com/images/I/a.jpg'), 'https://m.media-amazon.com/images/I/a.jpg')
        self.assertIsNone(server.remote_poster('https://example.com/a.jpg'))
