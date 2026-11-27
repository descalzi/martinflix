"""The controller's daily check for newer app images, and how the dashboard shows it."""
import io
import json
import sys
import unittest
import urllib.error
from email.message import Message
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'deploy/controller'))
sys.path.insert(0, str(ROOT))
import controller  # noqa: E402
import registry  # noqa: E402
import server  # noqa: E402

SONARR, PLEX, LISTENARR = 'lscr.io/linuxserver/sonarr:latest', 'plexinc/pms-docker:latest', 'ghcr.io/listenarrs/listenarr:canary-1.10.6'
D = {name: 'sha256:' + name * 64 for name in 'abcdef'}


class FakeStack(controller.Stack):
    def __init__(self, remote, running):
        self.remote, self.running = remote, running
        self.repo_digests = {'img-s1': [f'lscr.io/linuxserver/sonarr@{D["a"]}'], 'img-p1': [f'plexinc/pms-docker@{D["c"]}'],
                             'img-l1': [f'ghcr.io/listenarrs/listenarr@{D["e"]}'], 'img-built': []}
        self.calls = []
        self.app_updates = None

    def config(self):
        return {'env': {'MARTINFLIX_IMAGE': 'ghcr.io/descalzi/martinflix:edge'}}

    def controllable(self):
        return {'sonarr': {'image': SONARR}, 'plex': {'image': PLEX}, 'listenarr': {'image': LISTENARR},
                'dashboard': {'image': 'ghcr.io/descalzi/martinflix:edge'}}

    def compose(self, *args, timeout=900):
        self.calls.append(('compose', *args))
        if args[0] == 'up':
            self.running[args[-1]] = 'img-new'
        return SimpleNamespace(returncode=0, stdout='', stderr='')

    def run(self, args, timeout=900):
        self.calls.append(tuple(args))
        if args[0] == 'run':
            references = args[args.index('/app/deploy/controller/registry.py') + 1:]
            return SimpleNamespace(returncode=0, stdout=json.dumps({ref: self.remote.get(ref) for ref in references}), stderr='')
        return SimpleNamespace(returncode=0, stdout=json.dumps(self.repo_digests.get(args[-1], [])), stderr='')

    def containers(self):
        return {name: {'state': 'running', 'imageId': image, 'id': image} for name, image in self.running.items()}


class AppUpdateTests(unittest.TestCase):
    def stack(self, remote=None):
        stack = FakeStack(remote or {SONARR: D['b'], PLEX: D['c'], LISTENARR: D['e']},
                          {'sonarr': 'img-s1', 'plex': 'img-p1', 'listenarr': 'img-l1', 'dashboard': 'img-m'})
        stack.app_updates = controller.AppUpdates(stack, clock=lambda: 0)
        return stack

    def test_only_apps_whose_tag_moved_are_flagged_and_nothing_is_downloaded(self):
        stack = self.stack()
        stack.app_updates.check()
        self.assertFalse(any(call[:2] == ('compose', 'pull') or call[0] == 'pull' for call in stack.calls))
        run = next(call for call in stack.calls if call[0] == 'run')
        self.assertIn('--rm', run)
        self.assertEqual(run[run.index('--network') + 1], 'bridge', 'the controller itself has no internet')
        status = stack.status()['services']
        self.assertEqual({name: item['updateAvailable'] for name, item in status.items()},
                         {'sonarr': True, 'plex': False, 'listenarr': False, 'dashboard': False})

    def test_unknown_digests_flag_nothing(self):
        stack = self.stack({SONARR: None, PLEX: D['d']})
        stack.running['plex'] = 'img-built'
        stack.app_updates.check()
        self.assertEqual(stack.app_updates.available, {}, 'no answer, or an image built here, is left alone')

    def test_the_flag_goes_once_the_app_runs_another_image(self):
        stack = self.stack()
        stack.app_updates.check()
        stack.running['sonarr'] = 'img-recreated'
        self.assertFalse(stack.status()['services']['sonarr']['updateAvailable'], 'recreated by setup, say')
        stack.running['sonarr'] = 'img-s1'
        stack.update('sonarr')
        self.assertNotIn('sonarr', stack.app_updates.available)

    def test_nothing_is_checked_while_setup_runs(self):
        stack = self.stack()
        stack.app_updates.busy = lambda: True
        stack.app_updates.check()
        self.assertEqual(stack.calls, [])

    def test_the_dashboard_passes_the_flag_on(self):
        services = [{'name': 'sonarr', 'state': 'running'}]
        server.apply_control_status(services, {'sonarr': {'state': 'running', 'updateAvailable': True}})
        self.assertTrue(services[0]['control']['updateAvailable'])

    def test_a_sign_in_lasts_a_month(self):
        self.assertEqual(server.SESSION_SECONDS, 30 * 24 * 3600)


class FakeResponse:
    def __init__(self, headers=None, body=b''):
        self.headers = Message()
        for key, value in (headers or {}).items():
            self.headers[key] = value
        self.body = body

    def read(self, limit=-1):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FakeOpener:
    """A registry that wants an anonymous token, like Docker Hub, GHCR and lscr.io."""

    def __init__(self):
        self.requests = []

    def open(self, request, timeout):
        url = request if isinstance(request, str) else request.full_url
        self.requests.append((getattr(request, 'method', 'GET'), url, dict(getattr(request, 'headers', {}))))
        if url.startswith('https://auth.example/token'):
            return FakeResponse(body=b'{"token": "anon"}')
        if request.headers.get('Authorization') != 'Bearer anon':
            headers = Message()
            headers['WWW-Authenticate'] = 'Bearer realm="https://auth.example/token",service="registry.example",scope="repository:linuxserver/sonarr:pull"'
            raise urllib.error.HTTPError(url, 401, 'Unauthorized', headers, io.BytesIO(b''))
        return FakeResponse({'Docker-Content-Digest': D['f']})


class RegistryTests(unittest.TestCase):
    def test_references_are_read_like_docker_does(self):
        self.assertEqual(registry.parse('lscr.io/linuxserver/sonarr:latest'), ('lscr.io', 'linuxserver/sonarr', 'latest'))
        self.assertEqual(registry.parse('plexinc/pms-docker'), ('registry-1.docker.io', 'plexinc/pms-docker', 'latest'))
        self.assertEqual(registry.parse('nginx:1.27'), ('registry-1.docker.io', 'library/nginx', '1.27'))
        self.assertEqual(registry.parse('localhost:5000/app:v1'), ('localhost:5000', 'app', 'v1'))
        self.assertIsNone(registry.parse(f'nginx@{D["a"]}'))

    def test_a_head_request_with_an_anonymous_token_gives_the_digest(self):
        opener = FakeOpener()
        self.assertEqual(registry.remote_digest('lscr.io/linuxserver/sonarr:latest', opener), D['f'])
        methods = [method for method, url, _ in opener.requests if '/v2/' in url]
        self.assertEqual(methods, ['HEAD', 'HEAD'], 'no manifest or layer is downloaded')
        self.assertIn('application/vnd.oci.image.index.v1+json', opener.requests[0][2]['Accept'])


if __name__ == '__main__':
    unittest.main()
