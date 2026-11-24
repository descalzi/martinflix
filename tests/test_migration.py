"""Moving a stack setup didn't install onto MartinFlix: the export, restoring it, and adopting its data."""
import io
import json
import os
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'deploy/bootstrap'))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'deploy/controller'))
import configure  # noqa: E402
import export  # noqa: E402
import install  # noqa: E402
import restore  # noqa: E402


def arguments(**overrides):
    import argparse
    values = vars(install.build_parser().parse_args([]))
    values.update(overrides)
    return argparse.Namespace(**values)

SERVER = restore.PLEX_SERVER


class Folder(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)

    def tearDown(self):
        self.folder.cleanup()


class ExportTests(Folder):
    container = {'Mounts': [{'Source': '/var/lib/sonarr', 'Destination': '/config'},
                            {'Source': '/srv/nas', 'Destination': '/mnt/martinflix'},
                            {'Source': '/srv/nas/Movies', 'Destination': '/films'}]}

    def test_folders_are_found_through_the_containers_mounts(self):
        self.assertEqual(str(export.mount_source(self.container, '/config')), '/var/lib/sonarr')
        self.assertEqual(str(export.mount_source(self.container, '/mnt/martinflix/TV Shows')), '/srv/nas/TV Shows')
        self.assertIsNone(export.mount_source(self.container, '/elsewhere'))
        self.assertEqual(str(export.container_path(self.container, '/srv/nas')), '/mnt/martinflix')
        self.assertEqual(str(export.container_path(self.container, '/srv/nas/Movies/X')), '/films/X',
                         'the closest mount wins')

    def test_links_must_stay_inside(self):
        self.assertTrue(export.safe_link('Metadata/a/b/link', '../c/poster.jpg'))
        self.assertTrue(export.safe_link('Metadata/a/b/link', '../../../poster.jpg'))
        self.assertFalse(export.safe_link('Metadata/a/b/link', '../../../../escape'))
        self.assertFalse(export.safe_link('Metadata/link', '/etc/passwd'))

    def test_transmission_keeps_preferences_not_the_old_machines_folders(self):
        config = self.root / 'transmission'
        config.mkdir()
        (config / 'settings.json').write_text(json.dumps({
            'download-dir': '/var/lib/transmission-daemon/downloads', 'speed-limit-up': 99, 'rpc-whitelist': '172.17.*'}))
        exporter = export.Exporter({'transmission': ('t', {}, config)}, self.root)
        with tarfile.open(exporter.capture_transmission(None, self.root)) as archive:
            settings = json.load(archive.extractfile('settings.json'))
        self.assertEqual(settings['speed-limit-up'], 99)
        self.assertEqual(settings['download-dir'], '/downloads/complete')
        self.assertEqual(settings['rpc-whitelist'], json.loads(export.TRANSMISSION_SETTINGS.read_text())['rpc-whitelist'])

    def test_the_options_say_what_to_adopt_and_that_no_defaults_are_needed(self):
        plex = self.root / 'plex'
        (plex / export.PLEX_SERVER).mkdir(parents=True)
        (plex / export.PLEX_SERVER / 'Preferences.xml').write_text('<Preferences FriendlyName="Home Server"/>')
        exporter = export.Exporter({'plex': ('plex', {}, plex)}, self.root)
        exporter.adoption = {'media': '/mnt/martinflix', 'mediaHost': '/srv/nas', 'plexMedia': '/mnt/martinflix',
                             'images': {'radarr': 'nightly'}}
        with tarfile.open(exporter.capture_martinflix(None, self.root)) as archive:
            options = json.load(archive.extractfile('install-options.json'))
        self.assertEqual(options['legacy_media'], '/mnt/martinflix')
        self.assertEqual(options['plex_media_alias'], '/mnt/martinflix')
        self.assertEqual(options['image_tags'], {'radarr': 'nightly'})
        self.assertEqual(options['plex_name'], 'Home Server')
        self.assertTrue(options['plex_libraries_applied'] and options['bazarr_defaults_applied'])

    def test_the_old_apps_own_address_is_used(self):
        (self.root / 'config.xml').write_text('<Config><Port>8990</Port><UrlBase>/sonarr</UrlBase></Config>')
        self.assertEqual(export.arr_address(self.root, 8989), 'http://127.0.0.1:8990/sonarr')
        self.assertEqual(export.arr_address(self.root / 'none', 8989), 'http://127.0.0.1:8989')

    def test_the_launcher_shares_the_old_config_folders_read_only(self):
        import launch
        found = [(Path('/srv/backups'), False), (Path('/var/lib/sonarr'), True)]
        with patch.object(export, 'folders', return_value=found):
            self.assertEqual(launch.export_folders(['--backup-dir', '/srv/backups']), found)


class RestoreExportTests(Folder):
    def plex_archive(self, path, preferences):
        with tarfile.open(path, 'w') as archive:
            for name, body in (('Preferences.xml', preferences), ('Databases/com.plexapp.plugins.library.db', 'db'),
                               ('Data/Metadata/a/poster.jpg', 'poster'), ('Data/Plug-in Support/Data/x', 'x')):
                info = tarfile.TarInfo(name)
                info.size = len(body.encode())
                archive.addfile(info, io.BytesIO(body.encode()))
            for name, target in (('Data/Metadata/a/link', 'poster.jpg'), ('Data/Metadata/a/bad', '../../../../x')):
                info = tarfile.TarInfo(name)
                info.type, info.linkname = tarfile.SYMTYPE, target
                archive.addfile(info)

    def test_plex_data_comes_back_and_this_machines_identity_can_stay(self):
        state, aside = self.root / 'state', self.root / 'aside'
        server = state / 'plex' / SERVER
        (server / 'Metadata/old').mkdir(parents=True)
        (server / 'Plug-in Support/Databases').mkdir(parents=True)
        (server / 'Preferences.xml').write_text('<Preferences MachineIdentifier="test-vm" PlexOnlineToken="vm-token" '
                                                'FriendlyName="Test"/>')
        archive = self.root / 'plex.tar'
        self.plex_archive(archive, '<Preferences MachineIdentifier="prod" PlexOnlineToken="prod-token" '
                                   'FriendlyName="Prod" CinemaTrailersType="1"/>')
        restore.Restorer(state, aside, log=lambda *a: None, keep_plex_identity=True).plex(archive)
        preferences = (server / 'Preferences.xml').read_text()
        self.assertIn('MachineIdentifier="test-vm"', preferences)
        self.assertIn('PlexOnlineToken="vm-token"', preferences)
        self.assertIn('CinemaTrailersType="1"', preferences, 'the rest of the settings are the backup’s')
        self.assertNotIn('prod', preferences)
        self.assertEqual((server / 'Metadata/a/poster.jpg').read_text(), 'poster')
        self.assertEqual(os.readlink(server / 'Metadata/a/link'), 'poster.jpg')
        self.assertFalse((server / 'Metadata/a/bad').is_symlink(), 'a link out of the folder is skipped')
        self.assertTrue((aside / 'plex' / SERVER / 'Metadata/old').is_dir(), 'replaced artwork is kept aside')
        self.assertEqual((server / 'Plug-in Support/Databases/com.plexapp.plugins.library.db').read_text(), 'db',
                         'agent data never moves the databases aside')

    def test_keeping_an_identity_needs_one(self):
        with self.assertRaisesRegex(restore.RestoreError, 'no identity'):
            missing = self.root / 'Preferences.xml'
            missing.write_text('<Preferences/>')
            restore.plex_identity(missing)

    def test_an_export_adds_to_this_installations_options(self):
        current = {'project': 'here', 'media_path': '/srv/nas', 'downloads': True, 'download_apps': ['transmission'],
                   'plex': True, 'sonarr_indexer_defaults_applied': False, 'legacy_adopted': True}
        exported = {'legacy_media': '/mnt/martinflix', 'sonarr_indexer_defaults_applied': True, 'project': 'old'}
        merged = restore.merge_exported(exported, current)
        self.assertEqual(merged['download_apps'], ['transmission'])
        self.assertTrue(merged['plex'] and merged['sonarr_indexer_defaults_applied'])
        self.assertEqual((merged['project'], merged['legacy_media']), ('here', '/mnt/martinflix'))
        self.assertFalse(merged['legacy_adopted'], 'a second restore of an export adopts it again')

    def test_an_export_keeps_this_machines_secrets(self):
        state, aside = self.root / 'state', self.root / 'aside'
        (state / 'dashboard-secrets').mkdir(parents=True)
        (state / 'dashboard-secrets/admin-password').write_text('keep me')
        archive = self.root / 'martinflix.tar.gz'
        with tarfile.open(archive, 'w:gz') as tar:
            info = tarfile.TarInfo('install-options.json')
            info.size = 2
            tar.addfile(info, io.BytesIO(b'{}'))
        restore.Restorer(state, aside).martinflix(archive)
        self.assertEqual((state / 'dashboard-secrets/admin-password').read_text(), 'keep me')
        self.assertEqual((state / 'install-options.json').read_text(), '{}')


class OverlayTests(Folder):
    def plan(self, previous, **overrides):
        state = self.root / 'state'
        with patch.object(install, 'HOST_DEV', self.root / 'dev'):
            return install.plan_installation(arguments(**overrides), state, previous, 'p', self.root / 'downloads.json')

    def test_adopted_image_tags_and_plex_folder_reach_compose(self):
        media = self.root / 'media'
        media.mkdir()
        plan = self.plan({'image_tags': {'radarr': 'nightly', 'bazarr': 'development'}, 'plex': True,
                          'plex_media_alias': '/mnt/martinflix'}, media_path=str(media))
        tags = json.loads((self.root / 'image-tags-compose.json').read_text())
        self.assertEqual(tags, {'services': {'radarr': {'image': 'lscr.io/linuxserver/radarr:nightly'}}},
                         'only apps this installation runs')
        alias = json.loads((self.root / 'plex-media-compose.json').read_text())['services']['plex']['volumes'][0]
        self.assertEqual((alias['source'], alias['target'], alias['read_only']), (str(media), '/mnt/martinflix', True))
        self.assertIn(str(self.root / 'plex-media-compose.json'), plan['compose'])

    def test_bad_saved_values_are_refused(self):
        media = self.root / 'media'
        media.mkdir()
        for previous in ({'image_tags': {'radarr': 'night ly'}}, {'image_tags': {'gluetun': 'x'}},
                         {'plex': True, 'plex_media_alias': '/data'}, {'plex': True, 'plex_media_alias': '/a/../etc'}):
            with self.assertRaises(ValueError, msg=previous):
                self.plan(previous, media_path=str(media))


class FakeApi:
    """Enough of Sonarr, Radarr or Prowlarr for the adopt step, recording what it changes."""

    def __init__(self, data):
        self.data = data
        self.calls = []

    def request(self, route, payload=None, method=None):
        self.calls.append((method or ('POST' if payload is not None else 'GET'), route, payload))
        if method in ('PUT', 'DELETE') or payload is not None:
            return payload
        return self.data.get(route, [])


class AdoptTests(Folder):
    def test_titles_move_folders_and_old_links_give_way(self):
        media = self.root / 'media'
        sonarr = FakeApi({
            'series': [{'id': 1, 'path': '/mnt/martinflix/TV Shows/Slow Horses', 'rootFolderPath': '/mnt/martinflix/TV Shows'},
                       {'id': 2, 'path': '/elsewhere/Show', 'rootFolderPath': '/elsewhere'}],
            'rootfolder': [{'id': 7, 'path': '/mnt/martinflix/TV Shows'}],
            'downloadclient': [{'id': 3, 'name': 'Transmission', 'implementation': 'Transmission'},
                               {'id': 4, 'name': 'MartinFlix Transmission', 'implementation': 'Transmission'}],
            'remotepathmapping': [{'id': 5, 'localPath': '/var/lib/transmission-daemon/downloads/'}],
            'notification': [{'id': 6, 'implementation': 'PlexServer',
                              'fields': [{'name': 'mapFrom', 'value': ''}, {'name': 'mapTo', 'value': ''}]}]})
        radarr = FakeApi({'movie': [], 'rootfolder': []})
        prowlarr = FakeApi({'applications': [{'id': 1, 'implementation': 'Sonarr', 'fields': [
            {'name': 'baseUrl', 'value': 'http://172.17.0.1:8989'}, {'name': 'prowlarrUrl', 'value': 'http://172.17.0.5:9696'},
            {'name': 'apiKey', 'value': 'k'}]}], 'downloadclient': []})
        apis = {'Sonarr': sonarr, 'Radarr': radarr, 'Prowlarr': prowlarr}
        lines = []
        with patch.object(configure, 'AppApi', side_effect=lambda name, *a, **k: apis[name]):
            configure.adopt_legacy({'sonarr': 'k', 'radarr': 'r'}, {'sonarr': 8989, 'radarr': 7878, 'prowlarr': 9696},
                                   '/mnt/martinflix', media, prowlarr_key='p', transmission=True,
                                   plex_alias='/mnt/martinflix', log=lambda line, **k: lines.append(line))
        self.assertTrue((media / 'TV Shows').is_dir())
        self.assertIn(('PUT', 'series/editor', {'seriesIds': [1], 'rootFolderPath': '/data/TV Shows', 'moveFiles': False}),
                      sonarr.calls)
        self.assertIn(('POST', 'rootfolder', {'path': '/data/TV Shows'}), sonarr.calls)
        deleted = [route for method, route, _ in sonarr.calls if method == 'DELETE']
        self.assertEqual(deleted, ['rootfolder/7', 'downloadclient/3', 'remotepathmapping/5'])
        mapping = next(payload for method, route, payload in sonarr.calls if route.startswith('notification/6'))
        self.assertEqual({field['name']: field['value'] for field in mapping['fields']},
                         {'mapFrom': '/data/', 'mapTo': '/mnt/martinflix/'})
        application = next(payload for method, route, payload in prowlarr.calls if route.startswith('applications/1'))
        self.assertEqual({field['name']: field['value'] for field in application['fields']}['baseUrl'], 'http://sonarr:8989')
        self.assertEqual(lines[:2], ['   Sonarr: 1 series now in /data/TV Shows; 1 elsewhere left as they were.',
                                     '   Radarr: no films to move.'])


if __name__ == '__main__':
    unittest.main()


class ContainerNameTests(unittest.TestCase):
    def test_the_usual_installation_has_plain_names_and_another_keeps_its_project(self):
        with tempfile.TemporaryDirectory() as folder:
            usual, legacy, other = Path(folder) / 'state', Path(folder) / 'old', Path(folder) / 'other'
            with patch.object(install, 'default_state_directories', return_value=(usual, legacy)):
                self.assertEqual(install.container_prefix(usual, 'martinflix-abc'), 'martinflix')
                self.assertEqual(install.container_prefix(legacy, 'martinflix-first-run-abc'), 'martinflix')
                self.assertEqual(install.container_prefix(other, 'martinflix-def'), 'martinflix-def')
