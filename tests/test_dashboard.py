import io
import shutil
import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import server

ROOT = Path(__file__).resolve().parents[1]


class CatalogTests(unittest.TestCase):
    def test_catalog_matches_the_reference_stack_registry(self):
        registry = set()
        for line in (ROOT / 'deploy/hosts/martinflix/stacks.conf').read_text().splitlines():
            if not line.strip() or line.lstrip().startswith('#'):
                continue
            columns = [column.strip() for column in line.split('|')]
            registry.update(name for name in columns[4].split(',') if name)
        catalogued = {name for group in server.CATALOG['groups'] for name in group['services']}
        self.assertEqual(catalogued, registry)

    def test_every_catalogued_service_has_app_details(self):
        apps = server.CATALOG['apps']
        names = {name for group in server.CATALOG['groups'] for name in group['services']}
        self.assertFalse(names - apps.keys())
        for name, branch in server.CATALOG['branches'].items():
            self.assertIn(name, apps)
            self.assertFalse(set(branch['members']) - apps.keys())
        for stage in server.CATALOG['pipeline']:
            self.assertFalse(set(stage['services']) - apps.keys())
        for app in apps.values():
            if app.get('icon'):
                self.assertTrue((ROOT / 'web/assets' / (app['icon'] + '.webp')).exists(), app['icon'])

    def test_web_links_come_from_the_catalog(self):
        status = {'available': True, 'groups': [{'name': 'Web services', 'services': [
            {'name': 'seerr', 'state': 'running', 'status': 'Up'},
            {'name': 'portainer', 'state': 'running', 'status': 'Up'},
            {'name': 'tailscale', 'state': 'running', 'status': 'Up'},
        ]}]}
        with patch.object(server, 'container_status', return_value=status):
            services = server.service_status()['groups'][0]['services']
        self.assertEqual(services[0]['web'], {'port': 5055, 'scheme': 'http', 'path': '/', 'localOnly': False})
        self.assertEqual(services[1]['web']['scheme'], 'https')
        self.assertNotIn('web', services[2])


class RouteTests(unittest.TestCase):
    def request(self, path):
        handler = object.__new__(server.Handler)
        handler.server = SimpleNamespace(admin_state_file=None, admin_user='admin', admin_password='long test password',
                                         password_setup_required=False)
        handler.path = path
        handler.headers = {'Authorization': 'Basic YWRtaW46bG9uZyB0ZXN0IHBhc3N3b3Jk'}
        handler.wfile = io.BytesIO()
        sent = {}
        handler.send_response = lambda code, *args: sent.setdefault('status', code)
        handler.send_header = lambda *args: None
        handler.end_headers = lambda: None
        handler.send_error = lambda code, *args: sent.setdefault('status', code)
        handler.do_GET()
        return sent.get('status'), handler.wfile.getvalue()

    def test_each_page_has_a_route(self):
        for page in ('library', 'services', 'backups'):
            status, body = self.request('/' + page)
            self.assertEqual(status, 200)
            self.assertIn(f'data-page="{page}"'.encode(), body)
        status, body = self.request('/')
        self.assertIn(b'data-page="overview"', body)

    def test_fonts_are_served_and_unknown_paths_are_not(self):
        self.assertEqual(self.request('/fonts/figtree.woff2')[0], 200)
        self.assertEqual(self.request('/fonts/../server.py')[0], 404)
        self.assertEqual(self.request('/nope')[0], 404)


@unittest.skipUnless(shutil.which('node'), 'Node.js is needed to test the setup wizard')
class WizardTests(unittest.TestCase):
    def test_wizard_command_builder(self):
        result = subprocess.run(['node', '--test', str(ROOT / 'tests/wizard.test.js')], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
