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
        {'media_type': 'episode', 'user_id': 1234567, 'user': 'MartinDesc', 'friendly_name': 'Martin', 'rating_key': 900, 'grandparent_title': 'Slow Horses',
         'parent_media_index': '4', 'media_index': '6', 'title': 'Hello Goodbye', 'started': 1_000_000, 'stopped': 1_003_000,
         'percent_complete': 98, 'watched_status': 1, 'thumb': '/library/metadata/77/thumb/1700000000'},
        {'media_type': 'movie', 'user_id': 555, 'user': 'sofia_m', 'friendly_name': 'Sofia', 'rating_key': '500', 'title': 'Hamnet', 'year': 2025,
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
                         [('Slow Horses', 'S04E06 · Hello Goodbye', 'MartinDesc', 100, True), ('Hamnet', '2025', 'sofia_m', 41, False)])
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

    def test_several_finished_views_the_log_never_saw(self):
        log = server.PlayLog(self.path)
        log.add([server.tautulli_play(self.rows[1])])
        views = [{'account': 1, 'ratingKey': str(number), 'title': f'T{number}', 'user': 'Martin', 'watchedAt': 1_000_000 + number}
                 for number in range(5)] + [{'account': 1, 'ratingKey': '7', 'title': 'No date', 'user': 'Martin', 'watchedAt': None}]
        rows = log.merged(views)
        self.assertEqual(len(rows), 7)

    def test_the_owners_imported_plays_are_not_doubled_by_plexs_history(self):
        log = server.PlayLog(self.path)
        log.add([server.tautulli_play(self.rows[0])])
        # Plex's history calls the server's owner account 1; Tautulli used their plex.tv account.
        rows = log.merged([{'account': 1, 'ratingKey': '900', 'title': 'Slow Horses', 'user': 'Martin', 'watchedAt': 1_002_900}])
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]['finished'])



class HistoryPageTests(unittest.TestCase):
    rows = [{'title': 'Slow Horses', 'detail': f'S04E0{n} · T', 'user': 'Martin', 'watchedAt': 100 - n, 'kind': 'series'} for n in range(1, 6)] + \
           [{'title': 'Hamnet', 'detail': '2025', 'user': 'Sofia', 'watchedAt': 50, 'kind': 'film'}] * 40

    def page(self, **query):
        return server.history_page(self.rows, {key: [str(value)] for key, value in query.items()})

    def test_filters_and_pages(self):
        everything = self.page()
        self.assertEqual((len(everything['history']), everything['total'], everything['more'], everything['users']), (30, 45, True, ['Martin', 'Sofia']))
        self.assertEqual(len(self.page(offset=30)['history']), 15)
        self.assertEqual(self.page(who='Martin')['total'], 5)
        self.assertEqual(self.page(kind='film')['total'], 40)
        self.assertEqual(self.page(q='slow')['total'], 5)
        self.assertEqual(self.page(q='S04E03')['total'], 1)
        self.assertEqual(self.page(who='Martin', kind='film')['total'], 0)
        self.assertEqual(self.page(offset='x')['offset'], 0)



class NameTests(unittest.TestCase):
    def test_one_person_one_name(self):
        with tempfile.TemporaryDirectory() as folder:
            log = server.PlayLog(Path(folder) / 'plays.json')
            old = server.tautulli_play({**TautulliImportTests.rows[1], 'user': None})
            self.assertEqual(old['user'], 'Sofia', 'an import from before used the alias')
            log.add([old])
            log.add([server.tautulli_play(TautulliImportTests.rows[1])])
            self.assertEqual(log.plays[0]['user'], 'sofia_m', 'importing again puts the username right')
            log.record([{**FILM, 'account': 555, 'user': 'sofia_m', 'percent': 20}], now=3_000_000)
            rows = log.merged([], names={555: 'sofia_m', 1: 'MartinDesc'})
            self.assertEqual({row['user'] for row in rows}, {'sofia_m'})


if __name__ == '__main__':
    unittest.main()
