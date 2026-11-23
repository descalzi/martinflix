import argparse
import io
import json
import urllib.error
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'deploy/bootstrap'))
import configure  # noqa: E402
import controller as install_controller  # noqa: E402
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

    def test_the_controller_always_joins_and_runs_from_this_image(self):
        with patch.object(install, 'docker_socket', return_value=(Path('/var/run/docker.sock'), 998)):
            plan = self.plan()
        self.assertTrue(plan['control'])
        self.assertNotIn('--profile', plan['compose'])
        self.assertEqual(plan['environment']['MARTINFLIX_DOCKER_GID'], '998')
        self.assertEqual(plan['environment']['MARTINFLIX_IMAGE'], install.OWN_IMAGE)
        self.assertNotIn('MARTINFLIX_REPO_DIR', plan['environment'])
        with patch.object(install, 'docker_socket', return_value=None), self.assertRaisesRegex(ValueError, 'socket'):
            self.plan()

    def test_the_socket_path_is_this_machines_not_the_containers(self):
        socket_file = self.root / 'docker.sock'
        socket_file.touch()
        with patch.dict(os.environ, {'DOCKER_HOST': f'unix://{socket_file}', 'MARTINFLIX_DOCKER_SOCKET': '/run/user/1000/docker.sock'}):
            self.assertEqual(install.docker_socket(), (Path('/run/user/1000/docker.sock'), socket_file.stat().st_gid))

    def test_the_release_channel_comes_from_the_image_then_stays_as_chosen(self):
        with patch.dict(os.environ, {'MARTINFLIX_CHANNEL': 'stable'}):
            self.assertEqual(self.plan()['options']['channel'], 'stable')
            self.assertEqual(self.plan(previous={'channel': 'edge'})['options']['channel'], 'edge')
            self.assertEqual(self.plan(previous={'channel': 'edge'}, channel='stable')['options']['channel'], 'stable')
        with patch.dict(os.environ, {'MARTINFLIX_CHANNEL': 'dev'}):
            self.assertEqual(self.plan()['options']['channel'], 'edge')
        self.assertIn('--channel', install.keep_flags({'channel': 'stable'}))

    def test_the_tunnel_and_gpu_are_looked_for_on_this_machine(self):
        media = self.root / 'media'
        media.mkdir()
        host_dev = self.root / 'dev'
        (host_dev / 'dri').mkdir(parents=True)
        with patch.object(install, 'HOST_DEV', host_dev), self.assertRaisesRegex(ValueError, '/dev/net/tun'):
            self.plan(enable_vpn=True)
        with patch.object(install, 'HOST_DEV', host_dev):
            plan = self.plan(enable_plex=True, media_path=str(media))
        self.assertTrue(any(item.endswith('plex-gpu-compose.json') for item in plan['compose']))

    def test_the_pin_is_asked_twice_and_stored_as_a_hash(self):
        folder = self.state / 'controller'
        answers = iter(['12', '2468', '2468'])
        with patch.object(install.sys.stdin, 'isatty', return_value=True), \
                patch.object(install.getpass, 'getpass', side_effect=lambda prompt: next(answers)), \
                redirect_stdout(io.StringIO()):
            install.prepare_control_pin(folder)
        stored = (folder / 'pin-hash').read_text()
        self.assertNotIn('2468', stored)
        self.assertTrue(install_controller.pin_matches('2468', stored))
        install.prepare_control_pin(folder)  # Already set: no prompt.

    def test_discover_region_defaults_to_gb_and_is_remembered(self):
        self.assertEqual(self.plan()['environment']['MARTINFLIX_DISCOVER_REGION'], 'GB')
        plan = self.plan(discover_region='us')
        self.assertEqual((plan['options']['discover_region'], plan['environment']['MARTINFLIX_DISCOVER_REGION']), ('US', 'US'))
        self.assertEqual(self.plan({'discover_region': 'IE'})['environment']['MARTINFLIX_DISCOVER_REGION'], 'IE')
        with self.assertRaisesRegex(ValueError, 'two-letter'):
            self.plan(discover_region='United Kingdom')
        self.assertEqual(self.plan()['environment']['MARTINFLIX_DISCOVER'], 'on')
        self.assertEqual(self.plan(discover='off')['options']['discover'], 'off')
        self.assertEqual(self.plan({'discover': 'off'})['environment']['MARTINFLIX_DISCOVER'], 'off', 'the choice is kept')

    def test_plex_is_installed_with_the_media_directory(self):
        with self.assertRaisesRegex(ValueError, 'media directory'):
            self.plan(enable_plex=True)
        media = self.root / 'media'
        media.mkdir()
        plan = self.plan(enable_plex=True, media_path=str(media))
        self.assertTrue(plan['plex'] and plan['options']['plex'])
        self.assertIn(str(install.COMPOSE_FILE.with_name('compose.plex.yaml')), plan['compose'])
        self.assertIn(('plex', '0.0.0.0', install.PLEX_PORT), plan['bindings'])
        self.assertTrue(self.plan({'plex': True, 'media_path': str(media)})['plex'], 'an update keeps Plex')
        with self.assertRaisesRegex(ValueError, 'its own Plex'):
            self.plan(enable_plex=True, media_path=str(media), plex_url='http://elsewhere:32400')
        self.assertEqual(plan['options']['plex_name'], 'martinflix')
        self.assertEqual(self.plan(enable_plex=True, media_path=str(media), plex_name='martinflix-test')['options']['plex_name'],
                         'martinflix-test')
        with self.assertRaisesRegex(ValueError, 'plex-name'):
            self.plan(enable_plex=True, media_path=str(media), plex_name='<script>')

    def test_backups_go_to_a_chosen_folder_that_must_exist(self):
        plan = self.plan()
        self.assertEqual(plan['options']['backup_dir'], str(install.DEFAULT_BACKUP_DIR))
        self.assertEqual(plan['environment']['MARTINFLIX_BACKUP_HOST_DIR'],
                         str(install.DEFAULT_BACKUP_DIR / install.socket.gethostname()))
        nas = self.root / 'nas'
        with self.assertRaisesRegex(ValueError, 'NAS'):
            self.plan(backup_dir=str(nas))
        nas.mkdir()
        self.assertEqual(self.plan(backup_dir=str(nas))['environment']['MARTINFLIX_BACKUP_DIR'], str(nas))
        self.assertEqual(self.plan({'backup_dir': str(nas)})['options']['backup_dir'], str(nas), 'remembered')
        with self.assertRaisesRegex(ValueError, 'absolute'):
            self.plan(backup_dir='Backups')

    def test_plex_address_is_checked_and_remembered(self):
        self.assertEqual(self.plan()['environment']['MARTINFLIX_PLEX_URL'], '')
        plan = self.plan(plex_url='http://192.168.5.204:32400/')
        self.assertEqual(plan['options']['plex_url'], 'http://192.168.5.204:32400')
        self.assertEqual(self.plan({'plex_url': 'http://plex.lan:32400'})['environment']['MARTINFLIX_PLEX_URL'], 'http://plex.lan:32400')
        self.assertEqual(self.plan({'plex_url': 'http://plex.lan:32400'}, plex_url='')['options']['plex_url'], '', 'empty disconnects')
        with self.assertRaisesRegex(ValueError, 'Plex'):
            self.plan(plex_url='192.168.5.204; rm -rf /')

    def test_the_tmdb_key_is_stored_privately_and_blank_removes_it(self):
        path = self.state / 'tmdb-key'
        self.state.mkdir(parents=True, exist_ok=True)
        for answer, expected in (('a' * 32, 'a' * 32), ('', None)):
            with patch.object(install.sys.stdin, 'isatty', return_value=True), \
                    patch.object(install.getpass, 'getpass', return_value=answer), redirect_stdout(io.StringIO()):
                install.prepare_tmdb_key(path)
            self.assertEqual(path.read_text().strip() if path.exists() else None, expected)
        with patch.object(install.sys.stdin, 'isatty', return_value=True), \
                patch.object(install.getpass, 'getpass', return_value='not a key!'), redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(ValueError, 'TMDB'):
                install.prepare_tmdb_key(path)

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

    def test_prowlarr_gets_transmission_on_its_own_localhost_behind_the_vpn(self):
        schema = [{'implementation': 'Transmission', 'fields': [{'name': name, 'value': None} for name in (
            'host', 'port', 'useSsl', 'urlBase', 'username', 'password', 'category', 'priority', 'addPaused')]}]
        api = self.api({'downloadclient': [], 'downloadclient/schema': schema})
        configure.ensure_connection(api, 'downloadclient', 'MartinFlix Transmission', 'Transmission', 'host', {
            'host': '127.0.0.1', 'port': 9091, 'username': 'admin', 'password': 'shared'}, optional={'category': 'prowlarr', 'unknown': 1})
        route, payload = self.posts[-1]
        self.assertEqual(route, 'downloadclient')
        fields = {field['name']: field['value'] for field in payload['fields']}
        self.assertEqual((fields['host'], fields['port'], fields['category']), ('127.0.0.1', 9091, 'prowlarr'))
        self.assertNotIn('unknown', fields, 'settings this version lacks are left out')
        self.assertEqual(self.posts[0][0], 'downloadclient/test')

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


    def prowlarr(self, records):
        api = self.api(records)
        request = api.request
        def with_ids(route, payload=None, method=None):
            result = request(route, payload, method)
            return {**payload, 'id': 9} if route == 'tag' and payload else result
        api.request = with_ids
        return api

    IDS = {'movies': 2, 'series': 4}

    def test_seeded_indexers_are_saved_enabled_and_tagged_for_their_apps(self):
        schema = [{'definitionName': name, 'privacy': 'public', 'implementation': 'Cardigann', 'id': 0, 'fields': []}
                  for name in configure.PROWLARR_INDEXERS]
        prowlarr = self.prowlarr({'indexer/schema': schema, 'appprofile': [{'id': 1, 'name': 'Standard'}]})
        with redirect_stdout(io.StringIO()):
            configure.seed_prowlarr_indexers(prowlarr, self.IDS)
        self.assertEqual({route for route, _ in self.posts}, {'indexer?forceSave=true'})
        self.assertTrue(all(entry['enable'] for _, entry in self.posts))
        tags = {entry['definitionName']: entry['tags'] for _, entry in self.posts}
        self.assertEqual(tags['eztv'], [2, 4])
        self.assertEqual(tags['yts'], [2], 'films only, as on the reference host')
        self.assertEqual(tags['torrentdownloads'], [], 'books only there, and this stack has no books app')

    def test_tags_are_made_once_and_found_after(self):
        prowlarr = self.prowlarr({'tag': [{'id': 5, 'label': 'Movies'}]})
        self.assertEqual(configure.ensure_tags(prowlarr, ('movies', 'series')), {'movies': 5, 'series': 9})
        self.assertEqual(self.posts, [('tag', {'label': 'series'})])

    def test_flaresolverr_serves_the_tagged_indexers(self):
        prowlarr = self.prowlarr({'indexerproxy/schema': [
            {'implementation': 'FlareSolverr', 'fields': [{'name': 'host', 'value': ''}, {'name': 'requestTimeout', 'value': 60}]}]})
        with redirect_stdout(io.StringIO()):
            configure.ensure_flaresolverr_proxy(prowlarr, True, self.IDS)
        route, proxy = self.posts[0]
        self.assertEqual((route, proxy['tags'], proxy['fields'][0]['value']), ('indexerproxy?forceSave=true', [2, 4], 'http://127.0.0.1:8191/'))

    def test_an_earlier_install_moves_from_the_flaresolverr_tag_to_movies_and_series(self):
        prowlarr = self.prowlarr({
            'tag': [{'id': 7, 'label': 'flaresolverr'}],
            'indexerproxy': [{'id': 3, 'implementation': 'FlareSolverr', 'tags': [7], 'fields': []}],
            'applications': [{'id': 1, 'name': 'Sonarr', 'implementation': 'Sonarr', 'tags': []},
                             {'id': 2, 'name': 'Radarr', 'implementation': 'Radarr', 'tags': []},
                             {'id': 3, 'name': 'Readarr', 'implementation': 'Readarr', 'tags': [1]}],
            'indexer': [{'id': 1, 'name': '1337x', 'definitionName': '1337x', 'enable': True, 'tags': [7]},
                        {'id': 2, 'name': 'YTS', 'definitionName': 'yts', 'enable': True, 'tags': [7, 11]},
                        {'id': 3, 'name': 'Mine', 'definitionName': 'someprivate', 'enable': True, 'tags': [7]}]})
        with redirect_stdout(io.StringIO()):
            configure.ensure_flaresolverr_proxy(prowlarr, True, self.IDS, retag=True)
            configure.tag_prowlarr_apps(prowlarr, self.IDS)
            configure.update_seeded_prowlarr_indexers(prowlarr, enable=False, ids=self.IDS, drop=7)
        puts = {route: entry['tags'] for route, entry in self.puts}
        self.assertEqual(puts, {'indexerproxy/3?forceSave=true': [2, 4], 'applications/1?forceSave=true': [2, 4],
                                'applications/2?forceSave=true': [2], 'indexer/1?forceSave=true': [2],
                                'indexer/2?forceSave=true': [2, 11]}, 'your own indexers and tags are left alone')

    def test_indexers_get_a_site_address_when_they_have_none(self):
        prowlarr = self.prowlarr({'indexer': [
            {'id': 1, 'name': 'YTS', 'definitionName': 'yts', 'enable': True, 'tags': [],
             'indexerUrls': ['https://yts.mx/', 'https://yts.lt/'], 'fields': [{'name': 'baseUrl', 'value': None}]},
            {'id': 2, 'name': 'EZTV', 'definitionName': 'eztv', 'enable': False, 'tags': [],
             'indexerUrls': ['https://eztvx.to/'], 'fields': [{'name': 'baseUrl', 'value': 'https://eztvx.to/'}]}]})
        with patch.dict(configure.PROWLARR_BASE_URLS, {'yts': 'https://yts.lt/'}), redirect_stdout(io.StringIO()):
            configure.update_seeded_prowlarr_indexers(prowlarr, enable=False)
        self.assertEqual(len(self.puts), 1, 'one already set, and disabled indexers stay disabled')
        route, entry = self.puts[0]
        self.assertEqual((route, entry['fields'][0]['value'], entry['enable']), ('indexer/1?forceSave=true', 'https://yts.lt/', True))
        self.assertEqual(configure.chosen_base_url({'definitionName': 'eztv', 'indexerUrls': ['https://a/', 'https://b/']}), 'https://a/')

    def test_earlier_disabled_indexers_are_turned_on_once(self):
        prowlarr = self.prowlarr({'indexer': [
            {'id': 1, 'name': '1337x', 'definitionName': '1337x', 'enable': False, 'tags': []},
            {'id': 2, 'name': 'YTS', 'definitionName': 'yts', 'enable': True, 'tags': []},
            {'id': 3, 'name': 'Private', 'definitionName': 'someprivate', 'enable': False, 'tags': []}]})
        with redirect_stdout(io.StringIO()):
            configure.update_seeded_prowlarr_indexers(prowlarr, enable=True)
        self.assertEqual([(route, entry['enable']) for route, entry in self.puts], [('indexer/1?forceSave=true', True)],
                         'your own indexers are left alone')


class PullTests(unittest.TestCase):
    """docker pull is replaced by scripted replies; image IDs show whether anything changed."""

    SPECS = {'services': {'sonarr': {'image': 'lscr.io/linuxserver/sonarr:latest'},
                          'radarr': {'image': 'lscr.io/linuxserver/radarr:latest'},
                          'gluetun': {'image': 'qmcgaw/gluetun:v3.41.3'}}}

    def run_pull(self, scripts, ids_before, ids_after=None, services=('sonarr', 'radarr', 'gluetun')):
        ids_after = ids_after if ids_after is not None else ids_before
        pulled, pauses = set(), []
        attempts = {}

        def run(command, **kwargs):
            if 'config' in command:
                return SimpleNamespace(returncode=0, stdout=json.dumps(self.SPECS), stderr='')
            image = command[-1]
            current = (ids_after if image in pulled else ids_before).get(image)
            return SimpleNamespace(returncode=0 if current else 1, stdout=current or '', stderr='')

        def popen(command, **kwargs):
            image = command[-1]
            attempt = attempts.get(image, 0)
            attempts[image] = attempt + 1
            code, text = scripts[image][min(attempt, len(scripts[image]) - 1)]
            if code == 0:
                pulled.add(image)
            return SimpleNamespace(stdout=io.StringIO(text), wait=lambda: code)

        output = io.StringIO()
        with patch.object(install.time, 'sleep'):
            result = install.pull_images(['docker', 'compose'], {}, list(services), sleep=pauses.append,
                                         popen=popen, run=run, output=output)
        return result, pauses, output.getvalue()

    def test_each_app_says_whether_it_changed(self):
        layers = 'latest: Pulling from linuxserver/radarr\n3f4ca61aafcd: Pulling fs layer\n3f4ca61aafcd: Pull complete\n'
        result, _, output = self.run_pull(
            {'lscr.io/linuxserver/sonarr:latest': [(0, 'Status: Image is up to date\n')],
             'lscr.io/linuxserver/radarr:latest': [(0, layers)],
             'qmcgaw/gluetun:v3.41.3': [(0, '')]},
            ids_before={'lscr.io/linuxserver/sonarr:latest': 'sha256:a', 'lscr.io/linuxserver/radarr:latest': 'sha256:b'},
            ids_after={'lscr.io/linuxserver/sonarr:latest': 'sha256:a', 'lscr.io/linuxserver/radarr:latest': 'sha256:c',
                       'qmcgaw/gluetun:v3.41.3': 'sha256:d'})
        self.assertIsNone(result)
        lines = {line.split()[0]: ' '.join(line.split()[1:]) for line in output.splitlines()}
        self.assertEqual(lines, {'Sonarr': 'up to date', 'Radarr': 'updated', 'Gluetun': '(VPN) downloaded'})

    def test_a_busy_registry_is_retried(self):
        busy = (1, 'toomanyrequests: retry-after: 1.133859ms, allowed: 44000/minute')
        result, pauses, output = self.run_pull({'lscr.io/linuxserver/sonarr:latest': [busy, busy, (0, '')]},
                                               ids_before={'lscr.io/linuxserver/sonarr:latest': 'sha256:a'},
                                               services=('sonarr',))
        self.assertIsNone(result)
        self.assertEqual(pauses, [10, 30])
        self.assertIn('up to date', output)

    def test_other_failures_are_not_retried(self):
        result, pauses, _ = self.run_pull({'qmcgaw/gluetun:v3.41.3': [(1, 'manifest unknown')]}, ids_before={},
                                          services=('gluetun',))
        self.assertIn('manifest unknown', result)
        self.assertEqual(pauses, [])

    def test_an_update_carries_on_with_images_already_here(self):
        busy = (1, 'toomanyrequests: retry-after: 1ms')
        result, pauses, output = self.run_pull(
            {'lscr.io/linuxserver/sonarr:latest': [busy], 'qmcgaw/gluetun:v3.41.3': [busy]},
            ids_before={'lscr.io/linuxserver/sonarr:latest': 'sha256:a'}, services=('sonarr', 'gluetun'))
        self.assertIn('kept the version already here', output)
        self.assertIn('Gluetun (VPN):\ntoomanyrequests', result, 'an image never downloaded still stops setup')
        self.assertEqual(sorted(pauses), [10, 10, 30, 30, 60, 60])



class RestartReportTests(unittest.TestCase):
    def test_each_app_says_what_the_step_did(self):
        running = ('id-1', True, '2026-11-22T10:00:00Z')
        self.assertEqual(install.restart_outcome(running, running, False), 'running, unchanged')
        self.assertEqual(install.restart_outcome(running, ('id-2', True, 'later'), True), 'restarted with the new image')
        self.assertEqual(install.restart_outcome(running, ('id-2', True, 'later'), False), 'restarted with new settings')
        self.assertEqual(install.restart_outcome(running, ('id-1', True, 'later'), False), 'restarted',
                         'same container, new start: it followed Gluetun')
        self.assertEqual(install.restart_outcome(None, running, False), 'started')
        self.assertEqual(install.restart_outcome(('id-1', False, 'old'), running, False), 'started')
        self.assertIsNone(install.restart_outcome(running, None, False))

    def test_container_states_are_read_by_service(self):
        def run(command, **kwargs):
            if command[:2] == ['docker', 'ps']:
                return SimpleNamespace(returncode=0, stdout='abc\ndef\n')
            return SimpleNamespace(returncode=0, stdout=json.dumps([
                {'Id': 'abc', 'Config': {'Labels': {'com.docker.compose.service': 'sonarr'}},
                 'State': {'Running': True, 'StartedAt': 't1'}},
                {'Id': 'def', 'Config': {'Labels': {'com.docker.compose.service': 'gluetun'}},
                 'State': {'Running': False, 'StartedAt': 't0'}}]))
        self.assertEqual(install.container_states('martinflix-x', {}, run=run),
                         {'sonarr': ('abc', True, 't1'), 'gluetun': ('def', False, 't0')})


class PlexTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.config = Path(folder.name)

    def test_the_token_is_read_once_the_server_is_claimed(self):
        self.assertEqual(install.plex_token(self.config), '')
        preferences = self.config / install.PLEX_PREFERENCES
        preferences.parent.mkdir(parents=True)
        preferences.write_text('<?xml version="1.0"?><Preferences MachineIdentifier="x" PlexOnlineToken="abcDEF123-_x"/>')
        self.assertEqual(install.plex_token(self.config), 'abcDEF123-_x')

    def test_claim_codes_are_checked(self):
        with patch.object(install.sys.stdin, 'isatty', return_value=True), redirect_stdout(io.StringIO()):
            with patch('builtins.input', return_value='claim-AbCdEf123456_-xy'):
                self.assertEqual(install.plex_claim_code(), 'claim-AbCdEf123456_-xy')
            with patch('builtins.input', return_value=''):
                self.assertEqual(install.plex_claim_code(), '')
            with patch('builtins.input', return_value='not a claim'), self.assertRaisesRegex(ValueError, 'plex.tv/claim'):
                install.plex_claim_code()

    def opener(self, sections):
        requests = []
        def open_(request, timeout):
            requests.append((request.get_method(), request.full_url, request.get_header('X-plex-token')))
            body = json.dumps({'MediaContainer': {'Directory': sections}}).encode() if request.get_method() == 'GET' else b''
            response = MagicMock()
            response.__enter__.return_value.read.return_value = body
            return response
        return open_, requests

    def test_new_servers_get_tv_and_film_libraries_over_the_media_folders(self):
        open_, requests = self.opener([])
        with redirect_stdout(io.StringIO()):
            install.seed_plex_libraries('token', 'en-GB', opener=open_)
        posts = [url for method, url, _ in requests if method == 'POST']
        self.assertEqual(len(posts), 2)
        self.assertIn('name=Films', posts[0])
        self.assertIn('location=%2Fdata%2FMovies', posts[0])
        self.assertIn('prefs%5Bcountry%5D=GB', posts[0])
        self.assertIn('name=TV+programmes', posts[1])
        self.assertIn('prefs%5BshowOrdering%5D=aired', posts[1])
        self.assertTrue(all(token == 'token' for _, _, token in requests))

    def test_server_settings_never_include_identity_or_account(self):
        open_, requests = self.opener([])
        with redirect_stdout(io.StringIO()):
            install.apply_plex_settings('token', opener=open_)
        (method, url, _), = requests
        self.assertEqual(method, 'PUT')
        self.assertIn('ButlerStartHour=2', url)
        self.assertIn('DlnaEnabled=0', url)
        self.assertIn('PublishServerOnPlexOnlineKey=1', url)
        for private in ('FriendlyName', 'PlexOnlineToken', 'PlexOnlineMail', 'MachineIdentifier', 'ManualPortMappingPort'):
            self.assertNotIn(private, url)

    def test_pre_rolls_are_every_clip_in_the_folder(self):
        for name in ('pre-roll-yarn-city.mp4', 'pre-roll-darcy-rain.mp4', 'README.md'):
            (self.config / name).write_text('x')
        self.assertEqual(install.preroll_list(self.config), '/preroll/pre-roll-darcy-rain.mp4;/preroll/pre-roll-yarn-city.mp4')
        self.assertEqual(install.plex_tracked_settings('martinflix-test', self.config)['FriendlyName'], 'martinflix-test')
        self.assertEqual(install.preroll_list(self.config / 'missing'), '')

    def test_setup_waits_while_plex_is_still_starting(self):
        replies = iter([503, 503, 200])
        pauses = []
        def open_(request, timeout):
            code = next(replies)
            if code != 200:
                raise urllib.error.HTTPError(request.full_url, code, 'Service Unavailable', {}, io.BytesIO())
            response = MagicMock()
            response.__enter__.return_value.read.return_value = b'{"MediaContainer": {}}'
            return response
        with redirect_stdout(io.StringIO()) as output:
            self.assertTrue(install.wait_for_plex_ready('token', opener=open_, sleep=pauses.append))
        self.assertEqual(pauses, [5, 5])
        self.assertIn('still starting up', output.getvalue())

    def test_setup_gives_up_waiting_after_a_while(self):
        def open_(request, timeout):
            raise urllib.error.HTTPError(request.full_url, 503, 'Service Unavailable', {}, io.BytesIO())
        with redirect_stdout(io.StringIO()):
            self.assertFalse(install.wait_for_plex_ready('token', seconds=15, opener=open_, sleep=lambda _: None))

    def test_existing_libraries_are_left_alone(self):
        open_, requests = self.opener([{'key': '1', 'title': 'Films'}])
        with redirect_stdout(io.StringIO()):
            install.seed_plex_libraries('token', 'en-GB', opener=open_)
        self.assertEqual([method for method, _, _ in requests], ['GET'])

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

    def test_new_credentials_are_reported_so_gluetun_is_recreated(self):
        with tempfile.TemporaryDirectory() as folder:
            secrets = Path(folder) / 'download-secrets'
            with patch.object(install.sys.stdin, 'isatty', return_value=True), patch('builtins.print'), \
                    patch('builtins.input', return_value=' service-user '), \
                    patch.object(install.getpass, 'getpass', return_value='service-pass'):
                self.assertTrue(install.prepare_vpn_secrets(secrets))
                self.assertEqual((secrets / 'openvpn_user').read_text(), 'service-user')
                self.assertFalse(install.prepare_vpn_secrets(secrets), 'saved credentials are reused as they are')

    def test_server_list_refresh_is_best_effort(self):
        with tempfile.TemporaryDirectory() as folder:
            calls, printed = [], []
            with patch.object(install.subprocess, 'run', side_effect=lambda command, **kwargs: calls.append(command) or
                              SimpleNamespace(returncode=1)), \
                    patch('builtins.print', side_effect=lambda *args, **kwargs: printed.append(' '.join(map(str, args)))):
                install.refresh_vpn_servers(['docker', 'compose'], {}, Path(folder) / 'gluetun')
            self.assertEqual(calls[0][-5:], ['gluetun', 'update', '-enduser', '-providers', 'nordvpn'])
            self.assertIn('list built into its image', printed[-1])
            self.assertTrue((Path(folder) / 'gluetun').is_dir())

    def test_unreachable_servers_suggest_tcp(self):
        _, output = self.diagnose('gluetun-1  | WARN [openvpn] TLS Error: TLS key negotiation failed to occur within 60 seconds')
        self.assertIn('--vpn-protocol tcp', output)

    def test_rejected_login_keeps_the_credentials_and_names_the_servers(self):
        secrets, output = self.diagnose(
            'gluetun-1  | INFO [openvpn] [cz160.nordvpn.com] Peer Connection Initiated with [AF_INET]178.249.209.11:1194\n'
            'gluetun-1  | ERROR [openvpn] AUTH: Received control message: AUTH_FAILED\n'
            'gluetun-1  | INFO [openvpn] [cz170.nordvpn.com] Peer Connection Initiated with [AF_INET]178.249.209.21:1194\n'
            'gluetun-1  | ERROR [openvpn] AUTH: Received control message: AUTH_FAILED')
        self.assertIn('(cz160.nordvpn.com, cz170.nordvpn.com)', output)
        self.assertIn('device limit', output)
        self.assertTrue((secrets / 'openvpn_user').exists(), 'a rejection does not prove the credentials are wrong')

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
        self.media = self.home / 'media'
        self.media.mkdir()

    def tearDown(self):
        patch.stopall()
        self.folder.cleanup()

    def call(self, **overrides):
        overrides.setdefault('state_dir', self.state)
        if overrides.get('reset') or overrides.pop('new', False):
            overrides.setdefault('media_path', str(self.media))
        with patch('builtins.print'):
            install.install(arguments(**overrides), self.home / 'overlay.json')

    def test_invalid_reset_options_fail_before_anything_is_archived(self):
        with self.assertRaisesRegex(ValueError, 'enable-prowlarr'):
            self.call(reset=True, enable_flaresolverr=True)
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

    def test_resetting_an_old_style_folder_moves_to_the_new_default(self):
        home = self.home / 'home'
        legacy = home / '.local/state/martinflix-first-run'
        legacy.mkdir(parents=True, mode=0o700)
        (legacy / 'install-options.json').write_text('{}')
        os.chmod(legacy / 'install-options.json', 0o600)
        with patch.dict('os.environ', {'HOME': str(home)}):
            self.call(reset=True, state_dir=None)
            self.call(reset=True, state_dir=legacy)
        (first_reset, _), (explicit_reset, _) = self.reset.call_args_list
        self.assertEqual(first_reset[0], legacy.resolve(), 'the old folder is archived')
        first_target = self.run.call_args_list[0].args[1]
        self.assertEqual(first_target, (home / '.local/state/martinflix').resolve())
        self.assertEqual(self.run.call_args_list[1].args[1], legacy.resolve(), 'an explicit --state-dir is respected')

    def test_new_installs_and_resets_need_a_media_directory(self):
        with self.assertRaisesRegex(ValueError, 'need a media directory'):
            self.call(reset=True, media_path=None)
        with self.assertRaisesRegex(ValueError, 'need a media directory'):
            self.call(state_dir=self.home / 'new-install')
        self.call(update=True)
        self.run.assert_called_once()

    def test_update_requires_an_existing_installation(self):
        with self.assertRaisesRegex(ValueError, 'No installer-managed'):
            self.call(update=True, state_dir=self.home / 'missing')
        self.run.assert_not_called()


class StepProgressTests(unittest.TestCase):
    def progress(self, tty, states):
        output = io.StringIO()
        output.isatty = lambda: tty
        lines = iter(['', *states])  # first, the containers before the step: none
        run = lambda command, **kwargs: SimpleNamespace(returncode=0, stdout=next(lines))
        clock = iter([0, 1, 75, 80, 90]).__next__
        return install.StepProgress('martinflix-test', ['transmission', 'prowlarr'], {}, follows='gluetun',
                                    output=output, run=run, clock=clock), output

    def test_without_a_terminal_each_change_is_a_line(self):
        progress, output = self.progress(False, ['transmission\tcreated\tCreated\nprowlarr\tcreated\tCreated\n',
                                                 'transmission\tcreated\tCreated\nprowlarr\trunning\tUp 4 seconds (health: starting)\n',
                                                 'transmission\trunning\tUp 9 seconds (healthy)\nprowlarr\trunning\tUp 9 seconds (healthy)\n'])
        progress.update()
        self.assertEqual(output.getvalue(), '', 'nothing in the first seconds')
        for _ in range(3):
            progress.update()
        self.assertEqual(output.getvalue().splitlines(), [
            '   Prowlarr             waiting for Gluetun (VPN)', '   Transmission         waiting for Gluetun (VPN)',
            '   Prowlarr             starting…',
            '   Prowlarr             ready', '   Transmission         ready'])

    def test_on_a_terminal_the_lines_update_in_place_with_the_time_waited(self):
        progress, output = self.progress(True, ['prowlarr\trunning\tUp 4 seconds (health: starting)\n',
                                                'prowlarr\trunning\tUp 9 seconds (healthy)\n'])
        progress.update()
        progress.update()
        self.assertIn('Prowlarr             starting… 1:15', output.getvalue())
        self.assertIn('Transmission         waiting to be created 1:15', output.getvalue())
        progress.update()
        self.assertIn('\x1b[2F\x1b[0J', output.getvalue())
        progress.clear()
        self.assertEqual(progress.shown, 0)


class OverlayTests(unittest.TestCase):
    def test_generated_compose_files_outlive_setup(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        temporary, state = root / 'tmp', root / 'state'
        temporary.mkdir()
        state.mkdir(mode=0o700)
        (temporary / 'downloads.json').write_text('{"services": {}}')
        (temporary / 'plex-gpu-compose.json').write_text('{"services": {"plex": {}}}')
        compose = ['docker', 'compose', '-f', '/app/compose.yaml', '-f', str(temporary / 'downloads.json'),
                   '-f', str(temporary / 'plex-gpu-compose.json'), '--profile', 'plex']
        install.keep_overlays(compose, state, temporary)
        self.assertEqual(compose[5], str(state / 'downloads-compose.json'))
        self.assertEqual(compose[7], str(state / 'plex-gpu-compose.json'))
        self.assertEqual(json.loads((state / 'plex-gpu-compose.json').read_text()), {'services': {'plex': {}}})
        self.assertFalse(any(str(temporary) in item for item in compose))


class ProjectNameTests(unittest.TestCase):
    def test_the_old_project_name_only_when_its_containers_exist(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        state = Path(folder.name)
        (state / 'dashboard-secrets').mkdir()
        (state / 'dashboard-secrets/admin-password').write_text('x' * 20)
        with patch.object(install, 'project_exists', return_value=True):
            self.assertTrue(install.compose_project(state, {}).startswith('martinflix-first-run-'))
        with patch.object(install, 'project_exists', return_value=False):
            self.assertFalse(install.compose_project(state, {}).startswith('martinflix-first-run-'),
                             'a first run that stopped part-way keeps the current name')


class ReplacedContainerTests(unittest.TestCase):
    def test_a_container_about_to_be_replaced_is_running_not_ready(self):
        output = io.StringIO()
        output.isatty = lambda: False
        lines = iter(['sonarr\trunning\tUp 2 hours (healthy)\told1\n',
                      'sonarr\trunning\tUp 2 hours (healthy)\told1\n',
                      'sonarr\trunning\tUp 9 seconds (healthy)\tnew2\n'])
        progress = install.StepProgress('p', ['sonarr'], {}, output=output, clock=iter([0, 5, 9]).__next__,
                                        run=lambda command, **kwargs: SimpleNamespace(returncode=0, stdout=next(lines)))
        progress.update()
        progress.update()
        self.assertEqual(output.getvalue().splitlines(), ['   Sonarr               running', '   Sonarr               ready'])


class SummaryTests(unittest.TestCase):
    def test_a_finished_run_says_done_and_lists_every_app(self):
        plan = {'enabled': {'transmission', 'prowlarr'}, 'downloads': ['transmission', 'prowlarr'], 'plex': True, 'vpn': True,
                'vpn_city': 'Prague', 'vpn_country': '', 'download_network': 'vpn', 'bind': '192.168.4.166',
                'ports': {'dashboard': 8765, 'sonarr': 8989, 'radarr': 7878, 'prowlarr': 9696, 'transmission': 9091},
                'app_dirs': ('sonarr', 'radarr', 'transmission', 'prowlarr')}
        output = io.StringIO()
        install.print_summary('update', plan, Path('/home/m/state/dashboard-secrets/admin-password'), True, True, output)
        text = output.getvalue()
        self.assertIn('|____/', text)
        self.assertIn('is running, with Sonarr, Radarr, Prowlarr, Transmission, Plex and Gluetun (VPN) connected.', text)
        self.assertIn('Plex          http://192.168.4.166:32400/web', text)
        self.assertIn('NordVPN exit   Prague', text)
        self.assertNotIn('cat ', text)
        output = io.StringIO()
        install.print_summary('install', plan, Path('/home/m/admin-password'), False, False, output)
        self.assertIn('cat /home/m/admin-password', output.getvalue())


class ImageSetupTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)

    def test_secrets_from_the_dashboard_are_read_once_and_the_file_removed(self):
        path = self.root / 'settings-1.json'
        path.write_text(json.dumps({'tmdbKey': 'k' * 32, 'vpnUser': 'user', 'vpnPassword': 'pass', 'other': 'x'}))
        self.assertEqual(install.read_settings_file(path), {'tmdbKey': 'k' * 32, 'vpnUser': 'user', 'vpnPassword': 'pass'})
        self.assertFalse(path.exists())
        for bad in ({'tmdbKey': 'short'}, {'plexToken': 'has spaces in it'}, {'vpnUser': 'only-user'}, ['list']):
            path.write_text(json.dumps(bad))
            with self.assertRaises(ValueError, msg=bad):
                install.read_settings_file(path)
            self.assertFalse(path.exists(), 'even an invalid file is removed')
        self.assertEqual(install.read_settings_file(None), {})

    def test_secrets_from_the_dashboard_are_stored_or_removed(self):
        secrets_dir, vpn_dir = self.root / 'dashboard-secrets', self.root / 'download-secrets'
        install.private_directory(secrets_dir)
        (secrets_dir / 'plex-token').write_text('old-token-value\n')
        (secrets_dir / 'plex-token').chmod(0o600)
        with redirect_stdout(io.StringIO()):
            changed = install.apply_settings_secrets({'tmdbKey': 'k' * 32, 'plexToken': ''}, secrets_dir, vpn_dir)
        self.assertFalse(changed)
        self.assertEqual((secrets_dir / 'tmdb-key').read_text(), 'k' * 32 + '\n')
        self.assertFalse((secrets_dir / 'plex-token').exists())
        with redirect_stdout(io.StringIO()):
            self.assertTrue(install.apply_settings_secrets({'vpnUser': 'user', 'vpnPassword': 'pass'}, secrets_dir, vpn_dir))
        self.assertEqual((vpn_dir / 'openvpn_password').read_text(), 'pass')

    def test_prerolls_are_kept_in_step_with_the_image(self):
        source, target = self.root / 'image', self.root / 'state/preroll'
        source.mkdir()
        (source / 'a.mp4').write_bytes(b'clip a')
        (source / 'b.mp4').write_bytes(b'clip b')
        install.sync_prerolls(target, source)
        self.assertEqual(sorted(path.name for path in target.iterdir()), ['a.mp4', 'b.mp4'])
        (source / 'b.mp4').unlink()
        (source / 'a.mp4').write_bytes(b'clip A, re-cut')
        install.sync_prerolls(target, source)
        self.assertEqual([path.name for path in target.iterdir()], ['a.mp4'])
        self.assertEqual((target / 'a.mp4').read_bytes(), b'clip A, re-cut')

    def test_setup_outside_the_image_explains_how_to_start_it(self):
        with patch.dict(os.environ, {'MARTINFLIX_STAGE': ''}), redirect_stdout(io.StringIO()), \
                patch('sys.stderr', new=io.StringIO()) as errors, self.assertRaises(SystemExit):
            install.main(['--check'])
        self.assertIn('docker run --rm -it', errors.getvalue())


if __name__ == '__main__':
    unittest.main()
