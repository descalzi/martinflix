"""Telegram: Sonarr's and Radarr's webhook events become one kind of message, sent by the dashboard."""
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'deploy/bootstrap'))
import configure  # noqa: E402
import server  # noqa: E402

POSTER = [{'coverType': 'fanart', 'remoteUrl': 'https://example.org/fanart.jpg'},
          {'coverType': 'poster', 'remoteUrl': 'https://example.org/poster.jpg'}]


class ManualTimer:
    """Stands in for threading.Timer, so a test decides when gathered episodes are sent."""
    made = []

    def __init__(self, delay, function, args=()):
        self.function, self.args, self.cancelled = function, args, False
        ManualTimer.made.append(self)

    def start(self):
        pass

    def cancel(self):
        self.cancelled = True

    def fire(self):
        if not self.cancelled:
            self.function(*self.args)


class MessageTests(unittest.TestCase):
    def setUp(self):
        self.sent = []
        ManualTimer.made = []
        self.hooks = server.HookMessages(send=lambda text, photo: self.sent.append((text, photo)), timer=ManualTimer)

    def test_a_film_and_a_series_look_alike_with_their_posters(self):
        self.hooks.handle('radarr', {'eventType': 'Download', 'isUpgrade': False, 'movieFile': {'quality': 'Bluray-2160p'},
                                     'movie': {'title': 'The Wild Robot', 'year': 2024, 'images': POSTER}})
        self.hooks.handle('sonarr', {'eventType': 'Download', 'episodeFile': {'quality': 'WEBDL-1080p'},
                                     'series': {'id': 1, 'title': 'Slow Horses', 'year': 2022, 'images': POSTER},
                                     'episodes': [{'seasonNumber': 4, 'episodeNumber': 6, 'title': 'Hello Goodbye'}]})
        ManualTimer.made[-1].fire()
        self.assertEqual(self.sent, [
            ('<b>The Wild Robot</b> (2024)\nReady to watch · Bluray-2160p', 'https://example.org/poster.jpg'),
            ('<b>Slow Horses</b> (2022)\nS04E06 · Hello Goodbye\nReady to watch · WEBDL-1080p', 'https://example.org/poster.jpg')])

    def test_episodes_imported_together_are_one_message(self):
        for number in (1, 2, 3, 5):
            self.hooks.handle('sonarr', {'eventType': 'Download', 'isUpgrade': True, 'episodeFile': {'quality': 'Bluray-1080p'},
                                         'series': {'id': 7, 'title': 'Severance & Co', 'year': 2022},
                                         'episodes': [{'seasonNumber': 2, 'episodeNumber': number, 'title': f'E{number}'}]})
        self.assertEqual(sum(not timer.cancelled for timer in ManualTimer.made), 1, 'each import restarts the wait')
        ManualTimer.made[-1].fire()
        self.assertEqual(self.sent, [('<b>Severance &amp; Co</b> (2022)\nS02E01–E03, S02E05 · 4 episodes\nUpgraded · Bluray-1080p', None)])

    def test_additions_and_tests(self):
        self.hooks.handle('sonarr', {'eventType': 'SeriesAdd', 'series': {'title': 'Taskmaster', 'year': 2015, 'images': POSTER}})
        self.hooks.handle('radarr', {'eventType': 'MovieAdded', 'movie': {'title': 'Hamnet', 'year': 2025}})
        self.hooks.handle('radarr', {'eventType': 'Test'})
        self.hooks.handle('radarr', {'eventType': 'Grab', 'movie': {'title': 'Ignored'}})
        self.assertEqual([text for text, _ in self.sent], ['<b>Taskmaster</b> (2015)\nAdded · series',
                                                           '<b>Hamnet</b> (2025)\nAdded · film',
                                                           'Radarr can reach MartinFlix: downloads and new films will be posted here.'])

    def test_episode_codes(self):
        self.assertEqual(server.episode_codes([(1, 1)]), 'S01E01')
        self.assertEqual(server.episode_codes([(1, 3), (1, 1), (1, 2), (2, 1)]), 'S01E01–E03, S02E01')

    def test_a_poster_telegram_cant_fetch_falls_back_to_words(self):
        calls = []

        def call(token, method, payload, timeout=20):
            calls.append(method)
            if method == 'sendPhoto':
                raise server.TelegramError('wrong file identifier')
        with patch.object(server, 'telegram_call', side_effect=call):
            server.telegram_send('<b>X</b>', 'https://example.org/p.jpg', settings=('1:a', '5'))
        self.assertEqual(calls, ['sendPhoto', 'sendMessage'])


class WebhookTests(unittest.TestCase):
    def test_setup_adds_a_webhook_for_imports_and_additions_only(self):
        class Api:
            def __init__(self, existing):
                self.existing, self.calls = existing, []

            def request(self, route, payload=None, method=None):
                self.calls.append((method or ('POST' if payload is not None else 'GET'), route, payload))
                if route == 'notification':
                    return self.existing if payload is None else payload
                if route == 'notification/schema':
                    return [{'implementation': 'Webhook', 'id': 0, 'onGrab': True, 'onDownload': False, 'onUpgrade': False,
                             'onSeriesAdd': False, 'onMovieAdded': False, 'onHealthIssue': True, 'supportsOnGrab': True,
                             'fields': [{'name': 'url'}, {'name': 'method', 'value': 1}, {'name': 'username'}, {'name': 'password'}]}]
                return payload
        apis = {'Sonarr': Api([]), 'Radarr': Api([{'id': 9, 'name': 'MartinFlix'}])}
        with patch.object(configure, 'AppApi', side_effect=lambda name, *a, **k: apis[name]):
            configure.configure_webhooks({'sonarr': 's', 'radarr': 'r'}, {'sonarr': 8989, 'radarr': 7878}, password='p' * 30, enabled=True)
        method, route, item = apis['Sonarr'].calls[-1]
        self.assertEqual((method, route), ('POST', 'notification?forceSave=true'))
        self.assertEqual({key for key, value in item.items() if key.startswith('on') and value}, {'onDownload', 'onUpgrade', 'onSeriesAdd'})
        self.assertTrue(item['supportsOnGrab'], 'only the on… switches change')
        self.assertEqual({field['name']: field.get('value') for field in item['fields']},
                         {'url': 'http://dashboard:8765/api/hook/sonarr', 'method': 1, 'username': 'martinflix', 'password': 'p' * 30})
        self.assertEqual(apis['Radarr'].calls[-1][:2], ('PUT', 'notification/9?forceSave=true'))
        with patch.object(configure, 'AppApi', side_effect=lambda name, *a, **k: apis[name]):
            configure.configure_webhooks({'sonarr': 's', 'radarr': 'r'}, {'sonarr': 8989, 'radarr': 7878}, password='p', enabled=False)
        self.assertEqual(apis['Radarr'].calls[-1][:2], ('DELETE', 'notification/9'))


if __name__ == '__main__':
    unittest.main()
