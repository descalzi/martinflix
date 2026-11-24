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


if __name__ == '__main__':
    unittest.main()
