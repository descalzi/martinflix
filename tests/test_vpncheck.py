import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'deploy/controller'))
import vpncheck  # noqa: E402

LOOKUP = json.dumps({'ip': '185.242.6.10', 'city': 'Prague', 'region': 'Prague', 'country': 'CZ', 'org': 'AS212238 Datacamp',
                     'loc': '50.0880,14.4208', 'postal': '110 00'})


class FakeStack:
    def __init__(self, gluetun='running', network='service:gluetun', answer=(0, LOOKUP), logs=''):
        self.specs = {'gluetun': {}, 'transmission': {'network_mode': network}, 'sonarr': {}}
        self.state = {'gluetun': {'id': 'g1', 'state': gluetun, 'health': 'healthy'},
                      'transmission': {'id': 't1', 'state': 'running'}}
        self.answer, self.logs, self.calls = answer, logs, []

    def services(self):
        return self.specs

    def containers(self):
        return self.state

    def compose(self, *args, timeout=0):
        self.calls.append(args)
        return SimpleNamespace(returncode=self.answer[0], stdout=self.answer[1], stderr='curl: (28) timed out')

    def run(self, args, timeout=0):
        self.calls.append(tuple(args))
        if args[0] == 'inspect':
            return SimpleNamespace(returncode=0, stdout='/martinflix-x-gluetun-1\n', stderr='')
        return SimpleNamespace(returncode=0, stdout='', stderr=self.logs)


class VpnCheckTests(unittest.TestCase):
    def test_the_download_app_itself_reports_where_it_goes_out(self):
        stack = FakeStack()
        result = vpncheck.check(stack)
        self.assertEqual(stack.calls[0], ('exec', '-T', 'transmission', 'curl', '-fsS', '--max-time', '15', vpncheck.LOOKUP))
        self.assertEqual(result['probe'], 'transmission')
        self.assertEqual(result['exit'], {'ip': '185.242.6.10', 'city': 'Prague', 'region': 'Prague', 'country': 'CZ',
                                          'org': 'AS212238 Datacamp'})
        self.assertIsNone(result['problem'])

    def test_a_rejected_login_is_named(self):
        stack = FakeStack(answer=(28, ''), logs='2026-11-23 ERROR [openvpn] AUTH: Received control message: AUTH_FAILED')
        result = vpncheck.check(stack)
        self.assertIsNone(result['exit'])
        self.assertIn('rejected the login', result['problem'])
        self.assertIn(('logs', '--since', '15m', 'martinflix-x-gluetun-1'), stack.calls)

    def test_downloads_on_the_direct_route_and_a_stopped_gluetun(self):
        result = vpncheck.check(FakeStack(network=None))
        self.assertEqual((result['probe'], result['direct']), ('gluetun', ['transmission']))
        stopped = vpncheck.check(FakeStack(gluetun='exited'))
        self.assertIn('isn’t running', stopped['problem'])
        self.assertEqual(vpncheck.check(SimpleNamespace(services=lambda: {'sonarr': {}})), {'installed': False})


if __name__ == '__main__':
    unittest.main()
