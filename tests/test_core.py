import json
import tempfile
import unittest
import base64
from datetime import datetime, timezone
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import MagicMock, patch
import backup
import server

class CoreTests(unittest.TestCase):
    def test_snapshot_and_checksum(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'source'
            source.mkdir()
            (source / 'backup.zip').write_bytes(b'example')
            with patch.object(backup, 'SOURCES', {'sonarr': str(source)}):
                self.assertEqual(backup.create(root / 'dest'), 0)
            snapshot = next((root / 'dest').iterdir())
            self.assertEqual(backup.verify(snapshot), 0)
            (snapshot / 'sonarr' / 'backup.zip').write_bytes(b'altered')
            self.assertEqual(backup.verify(snapshot), 1)

    def test_missing_backup_is_incomplete(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'empty'
            source.mkdir()
            with patch.object(backup, 'SOURCES', {'sonarr': str(source)}):
                self.assertEqual(backup.create(root / 'dest'), 1)
            self.assertEqual(backup.verify(next((root / 'dest').iterdir())), 1)

    def test_arr_rejects_remote_url(self):
        with patch.dict('os.environ', {'MARTINFLIX_SONARR_KEY': 'test', 'MARTINFLIX_SONARR_URL': 'http://example.com'}):
            self.assertIn('Only local', server.arr_data('sonarr')['error'])

    def test_first_run_status_lists_both_apps_when_one_is_unreachable(self):
        response = MagicMock()
        response.__enter__.return_value.status = 200
        with patch.dict('os.environ', {'MARTINFLIX_ARR_COMPOSE_NETWORK': '1'}):
            with patch.object(server.ARR_OPENER, 'open', side_effect=[response, OSError()]):
                status = server.service_status()
        self.assertEqual(status['source'], 'app-probes')
        self.assertEqual([(app['name'], app['state']) for app in status['groups'][0]['services']],
                         [('sonarr', 'reachable'), ('radarr', 'unreachable')])

    def test_arr_compose_origin_requires_opt_in_and_correct_service(self):
        settings = {
            'MARTINFLIX_SONARR_KEY': 'test',
            'MARTINFLIX_SONARR_URL': 'http://sonarr:8989',
            'MARTINFLIX_ARR_COMPOSE_NETWORK': '0',
        }
        with patch.dict('os.environ', settings, clear=True):
            with self.assertRaises(ValueError):
                server.arr_connection('sonarr')
        settings['MARTINFLIX_ARR_COMPOSE_NETWORK'] = '1'
        with patch.dict('os.environ', settings, clear=True):
            self.assertEqual(server.arr_connection('sonarr'), ('http://sonarr:8989', 'test'))
        settings['MARTINFLIX_SONARR_URL'] = 'http://radarr:7878'
        with patch.dict('os.environ', settings, clear=True):
            with self.assertRaises(ValueError):
                server.arr_connection('sonarr')

    def test_sonarr_upcoming_window_and_poster(self):
        now = datetime(2024, 2, 29, 12, tzinfo=timezone.utc)
        series = {'id': 42, 'title': 'Series', 'monitored': True, 'status': 'continuing',
                  'images': [{'coverType': 'poster', 'url': 'https://example.com/unused.jpg'}]}
        raw = [
            dict(series, title='Boundary', nextAiring='2025-02-28T12:00:00Z'),
            dict(series, title='Sooner', status='upcoming', nextAiring='2024-03-01T10:00:00Z'),
            dict(series, nextAiring='2025-02-28T12:00:01Z'),
            dict(series, nextAiring='2024-02-29T11:59:59Z'),
            dict(series, status='ended', nextAiring='2024-03-01T00:00:00Z'),
            dict(series, monitored=False, nextAiring='2024-03-01T00:00:00Z'),
            dict(series, nextAiring='invalid'),
        ]
        items = server.arr_items('sonarr', raw, now)
        self.assertEqual([item['title'] for item in items], ['Sooner', 'Boundary'])
        self.assertEqual(items[0]['poster'], '/api/posters/sonarr/42')

    def test_radarr_missing_excludes_unmonitored_and_downloaded(self):
        now = datetime(2026, 11, 18, tzinfo=timezone.utc)
        raw = [
            {'title': 'Missing', 'monitored': True, 'hasFile': False},
            {'title': 'Present', 'monitored': True, 'hasFile': True},
            {'title': 'Ignored', 'monitored': False, 'hasFile': False},
            {'title': 'Unknown', 'monitored': True},
        ]
        self.assertEqual([item['title'] for item in server.arr_items('radarr', raw, now)], ['Missing'])

    def test_films_follow_radarrs_missing_list_by_their_minimum_availability(self):
        now = datetime(2026, 11, 18, tzinfo=timezone.utc)
        missing = {'monitored': True, 'hasFile': False}
        raw = [
            dict(missing, title='Unknown'),
            # In cinemas now, but Radarr waits for the digital release.
            dict(missing, title='Cinema run', minimumAvailability='released', inCinemas='2026-11-01T00:00:00Z',
                 digitalRelease='2026-12-20T00:00:00Z', physicalRelease='2027-01-10T00:00:00Z'),
            dict(missing, title='Out now', minimumAvailability='released', digitalRelease='2026-07-03T00:00:00Z'),
            dict(missing, title='Cinema wanted', minimumAvailability='inCinemas', inCinemas='2026-12-05T00:00:00Z',
                 digitalRelease='2027-02-01T00:00:00Z'),
            dict(missing, title='No home date', minimumAvailability='released', inCinemas='2026-10-01T00:00:00Z'),
            dict(missing, title='Radarr says so', minimumAvailability='released', physicalRelease='2027-03-01', isAvailable=True),
        ]
        items = {item['title']: item['availability'] for item in server.arr_items('radarr', raw, now)}
        self.assertEqual(items['Cinema run'], {'type': 'Digital', 'date': '2026-12-20', 'available': False})
        self.assertEqual(items['Out now'], {'type': 'Digital', 'date': '2026-07-03', 'available': True})
        self.assertEqual(items['Cinema wanted'], {'type': 'In cinemas', 'date': '2026-12-05', 'available': False})
        self.assertEqual(items['No home date'], {'type': 'Expected release', 'date': '2026-12-30', 'available': False})
        self.assertTrue(items['Radarr says so']['available'])
        order = [item['title'] for item in server.arr_items('radarr', raw, now)]
        self.assertEqual(order, ['Out now', 'Radarr says so', 'Cinema wanted', 'Cinema run', 'No home date', 'Unknown'])

    def test_plex_activity_projects_only_title_and_playback_state(self):
        payload = {'response': {'result': 'success', 'data': {'stream_count': '1', 'sessions': [
            {'full_title': 'Example title', 'state': 'playing', 'user': 'private-viewer', 'ip_address': '192.0.2.2'},
        ]}}}
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(payload)
        with patch.object(server, 'read_secret', return_value='test-key'):
            with patch.object(server.ARR_OPENER, 'open', return_value=response):
                activity = server.plex_activity()
        self.assertEqual(activity, {'configured': True, 'streams': 1,
                                    'sessions': [{'title': 'Example title', 'state': 'playing'}]})

    def test_dashboard_authentication(self):
        handler = object.__new__(server.Handler)
        handler.server = SimpleNamespace(admin_user='admin', admin_password='long test password')
        handler.headers = {}
        self.assertFalse(handler.authorized())
        handler.headers = {'Authorization': 'Basic ' + base64.b64encode(b'admin:wrong').decode()}
        self.assertFalse(handler.authorized())
        handler.headers = {'Authorization': 'Basic ' + base64.b64encode(b'admin:long test password').decode()}
        self.assertTrue(handler.authorized())

if __name__ == '__main__':
    unittest.main()
