import json
import tempfile
import unittest
import base64
from datetime import datetime, timezone
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch
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

    def test_radarr_sorts_by_release_with_unknown_dates_last(self):
        now = datetime(2026, 11, 18, tzinfo=timezone.utc)
        missing = {'monitored': True, 'hasFile': False}
        raw = [
            dict(missing, title='Unknown'),
            dict(missing, title='Theatrical', inCinemas='2026-08-02T00:00:00Z',
                 digitalRelease='2026-10-01T00:00:00Z'),
            dict(missing, title='Digital', digitalRelease='2026-07-03T00:00:00Z'),
            dict(missing, title='Physical', inCinemas='invalid', physicalRelease='2026-09-01'),
        ]
        items = server.arr_items('radarr', raw, now)
        self.assertEqual([(item['title'], item['releaseDate']) for item in items], [
            ('Digital', '2026-07-03'), ('Theatrical', '2026-08-02'),
            ('Physical', '2026-09-01'), ('Unknown', None),
        ])

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
