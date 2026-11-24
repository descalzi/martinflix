"""The dashboard's own play log, merged with Plex's finished views."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server  # noqa: E402

DAY = 86400
FILM = {'account': 1, 'ratingKey': '500', 'title': 'Hamnet', 'detail': '2025', 'poster': None, 'user': 'Martin'}


class PlayLogTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name) / 'plays.json'
        self.log = server.PlayLog(self.path)

    def tearDown(self):
        self.folder.cleanup()

    def test_half_a_film_one_day_and_the_rest_the_next(self):
        start = 1_000_000
        for minute, percent in ((0, 2), (5, 10), (50, 48)):
            self.log.record([{**FILM, 'percent': percent}], now=start + minute * 60)
        for minute, percent in ((0, 50), (45, 96)):
            self.log.record([{**FILM, 'percent': percent}], now=start + DAY + minute * 60)
        # Plex only knows the second evening, once it was watched to the end.
        history = [{**FILM, 'watchedAt': start + DAY + 47 * 60}]
        rows = self.log.merged(history)
        self.assertEqual([(row['finished'], row['progress']) for row in rows], [(True, 100), (False, 48)])
        self.assertEqual(rows[0]['watchedAt'], start + DAY + 47 * 60)
        self.assertEqual(len(json.loads(self.path.read_text())), 2, 'the log is saved')
        self.assertEqual(len(server.PlayLog(self.path).plays), 2, 'and read back after a restart')

    def test_everyone_is_kept_apart_and_finished_views_the_log_missed_still_show(self):
        self.log.record([{**FILM, 'percent': 30}, {**FILM, 'account': 2, 'user': 'Sofia', 'percent': 70}], now=2_000_000)
        rows = self.log.merged([{'account': 3, 'ratingKey': '9', 'title': 'Taskmaster', 'detail': 'S19E04',
                                 'user': 'Kids', 'watchedAt': 1_000_000}])
        self.assertEqual([(row['user'], row['finished'], row['progress']) for row in rows],
                         [('Martin', False, 30), ('Sofia', False, 70), ('Kids', True, 100)])

    def test_a_pause_carries_on_but_starting_over_or_the_next_day_is_a_new_play(self):
        self.log.record([{**FILM, 'percent': 10}], now=0)
        self.log.record([{**FILM, 'percent': 20}], now=2 * 3600)
        self.assertEqual(len(self.log.plays), 1, 'paused for two hours, then on from where it was')
        self.log.record([{**FILM, 'percent': 1}], now=3 * 3600)
        self.assertEqual(len(self.log.plays), 2, 'started over')
        self.log.record([{**FILM, 'percent': 5}], now=DAY)
        self.assertEqual(len(self.log.plays), 3, 'the next day')



class TautulliImportTests(unittest.TestCase):
    rows = [
        {'media_type': 'episode', 'user_id': 1234567, 'friendly_name': 'Martin', 'rating_key': 900, 'grandparent_title': 'Slow Horses',
         'parent_media_index': '4', 'media_index': '6', 'title': 'Hello Goodbye', 'started': 1_000_000, 'stopped': 1_003_000,
         'percent_complete': 98, 'watched_status': 1, 'thumb': '/library/metadata/77/thumb/1700000000'},
        {'media_type': 'movie', 'user_id': 555, 'friendly_name': 'Sofia', 'rating_key': '500', 'title': 'Hamnet', 'year': 2025,
         'started': 2_000_000, 'stopped': 2_002_000, 'percent_complete': 41, 'watched_status': 0, 'thumb': '../../etc/passwd'},
        {'media_type': 'track', 'user_id': 1, 'rating_key': 3, 'started': 1, 'stopped': 2},
    ]

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name) / 'plays.json'

    def tearDown(self):
        self.folder.cleanup()

    def test_tautullis_rows_become_plays(self):
        plays = [play for play in map(server.tautulli_play, self.rows) if play]
        self.assertEqual([(play['title'], play['detail'], play['user'], play['progress'], play['finished']) for play in plays],
                         [('Slow Horses', 'S04E06 · Hello Goodbye', 'Martin', 100, True), ('Hamnet', '2025', 'Sofia', 41, False)])
        self.assertEqual(plays[0]['poster'], '/api/plex-art?path=%2Flibrary%2Fmetadata%2F77%2Fthumb%2F1700000000')
        self.assertIsNone(plays[1]['poster'], 'only library artwork paths')

    def test_importing_twice_adds_nothing_and_the_dashboard_keeps_what_was_imported(self):
        dashboard = server.PlayLog(self.path)
        dashboard.record([{**FILM, 'percent': 10}], now=3_000_000)
        importer = server.PlayLog(self.path)
        plays = [play for play in map(server.tautulli_play, self.rows) if play]
        self.assertEqual(importer.add(plays), 2)
        self.assertEqual(importer.add(plays), 0)
        dashboard.record([{**FILM, 'percent': 20}], now=3_000_300)
        self.assertEqual(len(server.PlayLog(self.path).plays), 3, 'the dashboard read the import before saving')

    def test_the_owners_imported_plays_are_not_doubled_by_plexs_history(self):
        log = server.PlayLog(self.path)
        log.add([server.tautulli_play(self.rows[0])])
        # Plex's history calls the server's owner account 1; Tautulli used their plex.tv account.
        rows = log.merged([{'account': 1, 'ratingKey': '900', 'title': 'Slow Horses', 'user': 'Martin', 'watchedAt': 1_002_900}])
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]['finished'])


if __name__ == '__main__':
    unittest.main()
