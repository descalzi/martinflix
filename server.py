#!/usr/bin/env python3
"""Local, read-only MartinFlix control room. No third-party dependencies."""
import json
import base64
import binascii
import calendar
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
import hashlib
import hmac
import html
import os
import re
import secrets
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WEB = ROOT / 'web'
SESSION_COOKIE = 'martinflix_session'
SESSION_SECONDS = 12 * 60 * 60
MIN_PERSONAL_PASSWORD = 8
ARR_COMPOSE_ADDRESSES = {'sonarr': 'http://sonarr:8989', 'radarr': 'http://radarr:7878'}
ARR_HEALTH_ENDPOINTS = {
    'sonarr': (8989, '/api/v3/health'),
    'radarr': (7878, '/api/v3/health'),
    'prowlarr': (9696, '/api/v1/health'),
    'bazarr': (6767, '/api/system/health'),
}
STACKS = {
    'Core': ['tailscale', 'portainer'],
    'Media server': ['plex'],
    'Downloads & automation': ['nordvpn', 'transmission-daemon', 'flaresolverr', 'prowlarr', 'bazarr', 'sonarr', 'radarr'],
    'Books': ['bookorbit-db', 'bookorbit-app'],
    'Web services': ['homepage', 'seerr', 'tautulli'],
    'Bookshelf (optional)': ['bookshelf'],
}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


ARR_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())


def read_secret(name):
    path = os.environ.get(name + '_FILE')
    if path:
        try:
            return Path(path).read_text().rstrip('\r\n')
        except FileNotFoundError:
            return ''
    return os.environ.get(name, '')


def docker_status():
    try:
        snapshot = os.environ.get('MARTINFLIX_DOCKER_STATUS_FILE')
        if snapshot:
            path = Path(snapshot)
            with path.open() as source:
                if time.time() - os.fstat(source.fileno()).st_mtime > 180:
                    return {'available': False, 'error': 'Container status snapshot is stale.', 'groups': []}
                lines = source.readlines()
        else:
            process = subprocess.run(
                ['docker', 'ps', '-a', '--format', '{{json .}}'],
                capture_output=True, text=True, timeout=8, check=True,
            )
            lines = process.stdout.splitlines()
        containers = {}
        for line in lines:
            item = json.loads(line)
            containers[item['Names']] = {'name': item['Names'], 'state': item.get('State', 'unknown'), 'status': item.get('Status', '')}
        return {'available': True, 'groups': [{'name': group, 'services': [containers.get(name, {'name': name, 'state': 'missing', 'status': 'Not found'}) for name in names]} for group, names in STACKS.items()]}
    except (OSError, subprocess.SubprocessError, ValueError, TypeError, KeyError):
        return {'available': False, 'error': 'Docker is unavailable or access was denied.', 'groups': []}


def container_status():
    if os.environ.get('MARTINFLIX_ARR_COMPOSE_NETWORK') != '1':
        return docker_status()

    probes = {name: (address + '/ping', 200) for name, address in ARR_COMPOSE_ADDRESSES.items()}
    enabled = os.environ.get('MARTINFLIX_DOWNLOAD_APPS')
    if enabled is None:
        enabled = 'transmission,prowlarr,flaresolverr' if os.environ.get('MARTINFLIX_DOWNLOAD_STACK') == '1' else ''
    downloads = set(enabled.split(',')) - {''}
    gateway = os.environ.get('MARTINFLIX_DOWNLOAD_GATEWAY')
    vpn = os.environ.get('MARTINFLIX_VPN_ENABLED') == '1' or (bool(downloads) and gateway is None)
    behind_vpn = gateway == 'gluetun' or (gateway is None and vpn)
    if vpn:
        probes['nordvpn'] = ('http://gluetun:9999/', 200)
    if 'transmission' in downloads:
        probes['transmission-daemon'] = (f'http://{"gluetun" if behind_vpn else "transmission"}:9091/transmission/rpc', 401)
    if 'prowlarr' in downloads:
        probes['prowlarr'] = (f'http://{"gluetun" if behind_vpn else "prowlarr"}:9696/ping', 200)
    if 'flaresolverr' in downloads:
        probes['flaresolverr'] = (f'http://{"gluetun" if behind_vpn else "flaresolverr"}:8191/', 200)
    services = []
    for name, (url, expected) in probes.items():
        try:
            with ARR_OPENER.open(url, timeout=2) as response:
                reachable = response.status == expected
        except urllib.error.HTTPError as exc:
            reachable = exc.code == expected
            exc.close()
        except OSError:
            reachable = False
        services.append({
            'name': name,
            'state': 'reachable' if reachable else 'unreachable',
            'status': 'Responded to health check' if reachable else 'Health check did not respond',
        })
    return {'available': True, 'source': 'app-probes', 'downloadsBehindVpn': bool(downloads) and behind_vpn,
            'groups': [{'name': 'Downloads & automation' if downloads else 'Library apps', 'services': services}]}


def arr_connection(kind, *, prowlarr_via_vpn=False):
    key = read_secret('MARTINFLIX_' + kind.upper() + '_KEY')
    if not key:
        return None
    default = f'http://127.0.0.1:{ARR_HEALTH_ENDPOINTS[kind][0]}'
    compose_base = ARR_COMPOSE_ADDRESSES.get(kind)
    if kind == 'prowlarr' and os.environ.get('MARTINFLIX_ARR_COMPOSE_NETWORK') == '1':
        host = 'gluetun' if prowlarr_via_vpn else 'prowlarr'
        compose_base = default = f'http://{host}:9696'
    base = os.environ.get('MARTINFLIX_' + kind.upper() + '_URL', default)
    # The first-run Compose project has fixed Arr service names on its private bridge.
    if os.environ.get('MARTINFLIX_ARR_COMPOSE_NETWORK') == '1' and base == compose_base:
        return base, key
    parsed = urllib.parse.urlparse(base)
    if (parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1', 'localhost', '::1')
            or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment):
        raise ValueError('Only local HTTP service URLs are supported.')
    return base.rstrip('/'), key


def arr_open(connection, endpoint):
    base, key = connection
    request = urllib.request.Request(base + endpoint, headers={'X-Api-Key': key})
    return ARR_OPENER.open(request, timeout=5)


def arr_health(kind, *, prowlarr_via_vpn=False):
    try:
        connection = arr_connection(kind, prowlarr_via_vpn=prowlarr_via_vpn)
        if connection is None:
            return {'state': 'unconfigured'}
        with arr_open(connection, ARR_HEALTH_ENDPOINTS[kind][1]) as response:
            items = json.load(response)
        if kind == 'bazarr' and isinstance(items, dict):
            items = items.get('data')
        if not isinstance(items, list):
            raise ValueError('Invalid health response')
        alerts = []
        for item in items:
            field = 'issue' if kind == 'bazarr' else 'message'
            if not isinstance(item, dict) or not isinstance(item.get(field), str) or not item[field].strip():
                raise ValueError('Invalid health alert')
            severity = str(item.get('type', '')).lower()
            if severity not in ('info', 'warning', 'error'):
                severity = 'warning'
            message = item[field]
            if kind == 'bazarr' and item.get('object'):
                message = f'{item["object"]} · {message}'
            alerts.append({'severity': severity, 'message': message})
        return {'state': 'available', 'alerts': alerts}
    except urllib.error.HTTPError as exc:
        exc.close()
    except (OSError, ValueError):
        pass
    return {'state': 'unavailable'}


def service_status():
    status = container_status()
    services = [service for group in status['groups'] for service in group['services']
                if service['name'] in ARR_HEALTH_ENDPOINTS and service['state'] in ('reachable', 'running')]
    if services:
        with ThreadPoolExecutor(max_workers=len(services)) as pool:
            checks = [pool.submit(arr_health, service['name'], prowlarr_via_vpn=status.get('downloadsBehindVpn', False))
                      for service in services]
            for service, check in zip(services, checks):
                service['health'] = check.result()
    return status


def next_airing(value):
    if not isinstance(value, str):
        return None
    try:
        airing = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return airing.astimezone(timezone.utc) if airing.tzinfo else None
    except ValueError:
        return None


def movie_release(item):
    for field in ('inCinemas', 'digitalRelease', 'physicalRelease'):
        value = item.get(field)
        if isinstance(value, str):
            try:
                return date.fromisoformat(value[:10]).isoformat()
            except ValueError:
                continue
    return None


def arr_items(kind, raw, now):
    last_day = calendar.monthrange(now.year + 1, now.month)[1]
    deadline = now.replace(year=now.year + 1, day=min(now.day, last_day))
    items = []
    for item in raw:
        if not item.get('monitored'):
            continue
        airing = None
        status = item.get('status') or ''
        if kind == 'sonarr':
            airing = next_airing(item.get('nextAiring'))
            if status.casefold() not in ('upcoming', 'continuing') or not airing or not now <= airing <= deadline:
                continue
        elif item.get('hasFile') is not False:
            continue

        media_id = item.get('id')
        has_poster = any(image.get('coverType') == 'poster' for image in (item.get('images') or []))
        poster = (f'/api/posters/{kind}/{media_id}'
                  if type(media_id) is int and 0 < media_id <= 2147483647 and has_poster else None)
        items.append({
            'title': item.get('title') or 'Untitled',
            'year': item.get('year'),
            'status': status,
            'nextAiring': airing.isoformat() if airing else None,
            'releaseDate': movie_release(item) if kind == 'radarr' else None,
            'poster': poster,
        })
    sort_field = 'nextAiring' if kind == 'sonarr' else 'releaseDate'
    return sorted(items, key=lambda item: (item[sort_field] is None, item[sort_field] or '', item['title'].casefold()))


def arr_data(kind):
    try:
        connection = arr_connection(kind)
    except ValueError as exc:
        return {'configured': True, 'error': str(exc), 'items': []}
    if not connection:
        return {'configured': False, 'items': []}
    endpoint = '/api/v3/series' if kind == 'sonarr' else '/api/v3/movie'
    try:
        with arr_open(connection, endpoint) as response:
            raw = json.load(response)
        return {'configured': True, 'items': arr_items(kind, raw, datetime.now(timezone.utc))}
    except (OSError, ValueError, TypeError, AttributeError):
        return {'configured': True, 'error': 'Could not reach the service or its API rejected the key.', 'items': []}


def arr_poster(kind, media_id):
    if int(media_id) > 2147483647:
        return None
    limit = 1024 * 1024
    try:
        connection = arr_connection(kind)
        if connection:
            with arr_open(connection, f'/api/v3/mediacover/{media_id}/poster-250.jpg') as response:
                if response.headers.get_content_type() == 'image/jpeg':
                    body = response.read(limit + 1)
                    if body.startswith(b'\xff\xd8\xff') and len(body) <= limit:
                        return body
    except (OSError, ValueError):
        pass
    return None


def plex_activity():
    try:
        key = read_secret('MARTINFLIX_TAUTULLI_KEY')
        if not key:
            return {'configured': False}
        query = urllib.parse.urlencode({'cmd': 'get_activity', 'apikey': key})
        with ARR_OPENER.open('http://127.0.0.1:8181/api/v2?' + query, timeout=3) as response:
            result = json.load(response)['response']
        if result['result'] != 'success':
            raise ValueError('Tautulli rejected the request')
        data = result['data']
        sessions = [
            {'title': item.get('full_title') or item.get('title') or 'Untitled',
             'state': item.get('state', 'unknown')}
            for item in data['sessions']
        ]
        return {'configured': True, 'streams': int(data['stream_count']), 'sessions': sessions}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {'configured': True, 'error': 'Plex activity is unavailable.'}


def queued_downloads(kind):
    try:
        connection = arr_connection(kind)
        if connection:
            with arr_open(connection, '/api/v3/queue/status') as response:
                return int(json.load(response)['totalCount'])
    except (OSError, ValueError, TypeError, KeyError):
        pass
    return None


def dashboard_activity():
    with ThreadPoolExecutor(max_workers=3) as pool:
        plex = pool.submit(plex_activity)
        sonarr = pool.submit(queued_downloads, 'sonarr')
        radarr = pool.submit(queued_downloads, 'radarr')
        return {'plex': plex.result(), 'queues': {'sonarr': sonarr.result(), 'radarr': radarr.result()}}


def backup_status():
    directory = Path(os.environ.get('MARTINFLIX_BACKUP_DIR', str(Path.home() / '.local/share/martinflix/backups'))).expanduser()
    try:
        manifests = sorted(directory.glob('*/manifest.json'), reverse=True)[:12]
        result = []
        for path in manifests:
            try:
                data = json.loads(path.read_text())
                result.append({'id': path.parent.name, 'created': data.get('created'), 'files': len(data.get('files', [])), 'missing': data.get('missing', []), 'errors': data.get('errors', [])})
            except (OSError, ValueError):
                result.append({'id': path.parent.name, 'errors': ['Manifest unreadable']})
        return {'directory': str(directory), 'snapshots': result}
    except OSError:
        return {'directory': str(directory), 'snapshots': [], 'error': 'Cannot read backup directory.'}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        path = url.path
        form_login = self.server.admin_state_file is not None
        if form_login and path in ('/style.css', '/auth.js'):
            self.send_web_file(path[1:], 'text/css' if path == '/style.css' else 'text/javascript')
            return
        if form_login and path == '/login':
            if self.authorized():
                self.redirect('/setup-password' if self.server.password_setup_required else '/')
            else:
                self.send_auth_page('login.html', password_set=(url.query == 'password-set=1'
                                                               and not self.server.password_setup_required))
            return
        if not self.authorized():
            self.require_login()
            return
        if path == '/setup-password':
            if self.server.password_setup_required:
                self.send_auth_page('setup-password.html')
            else:
                self.redirect('/')
            return
        if self.server.password_setup_required:
            self.redirect('/setup-password')
            return
        if path == '/change-password' and form_login:
            self.send_auth_page('setup-password.html', changing=True)
            return
        if path == '/api/status':
            data = {'docker': service_status(), 'backups': backup_status(), 'time': int(time.time()), 'canChangePassword': form_login}
            self.send_json(data)
        elif path == '/api/activity':
            self.send_json(dashboard_activity())
        elif path in ('/api/sonarr', '/api/radarr'):
            self.send_json(arr_data(path.rsplit('/', 1)[1]))
        elif poster := re.fullmatch(r'/api/posters/(sonarr|radarr)/([1-9][0-9]{0,9})', path):
            body = arr_poster(*poster.groups())
            if body is None:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header('Content-Type', 'image/jpeg')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'private, max-age=3600')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers()
            self.wfile.write(body)
        elif path in ('/', '/services', '/app.js', '/style.css'):
            static = {
                '/': ('index.html', 'text/html'),
                '/services': ('index.html', 'text/html'),
                '/app.js': ('app.js', 'text/javascript'),
                '/style.css': ('style.css', 'text/css'),
            }
            if path == '/services':
                page = (WEB / 'index.html').read_bytes().replace(b'data-page="overview"', b'data-page="services"', 1)
                self.send_web_file(*static[path], body=page)
            else:
                self.send_web_file(*static[path])
        elif re.fullmatch(r'/assets/[a-z0-9-]+\.webp', path):
            try:
                body = (WEB / path[1:]).read_bytes()
            except FileNotFoundError:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header('Content-Type', 'image/webp')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'private, max-age=3600')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)

    def do_POST(self):
        path = urllib.parse.urlsplit(self.path).path
        if self.server.admin_state_file is None or path not in ('/login', '/setup-password', '/change-password'):
            self.send_error(404)
            return
        try:
            origin = urllib.parse.urlsplit(self.headers.get('Origin', ''))
        except ValueError:
            origin = None
        if not origin or origin.scheme not in ('http', 'https') or origin.netloc != self.headers.get('Host'):
            self.send_error(403, 'Open the dashboard and submit the form again.')
            return
        if path != '/login' and not self.authorized():
            self.require_login()
            return
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 4096 or self.headers.get_content_type() != 'application/x-www-form-urlencoded':
                raise ValueError
            fields = urllib.parse.parse_qs(self.rfile.read(length).decode('utf-8'), max_num_fields=4)
        except (OSError, ValueError):
            self.send_error(400, 'Invalid sign-in form')
            return
        if path == '/login':
            self.sign_in(fields)
            return
        changing = path == '/change-password'
        if changing == self.server.password_setup_required:
            self.redirect('/setup-password' if self.server.password_setup_required else '/')
            return
        self.change_password(fields, changing)

    def sign_in(self, fields):
        user = fields.get('username', [''])[0]
        password = fields.get('password', [''])[0]
        with self.server.password_setup_lock:
            if not self.valid_credentials(user, password):
                self.send_auth_page('login.html', 'Check your username and password and try again.', 401)
                return
            destination = '/setup-password' if self.server.password_setup_required else '/'
            expiry = int(time.time()) + SESSION_SECONDS
            value = f'{expiry}.{secrets.token_urlsafe(24)}'
            signature = hmac.new(self.server.session_key, value.encode(), 'sha256').hexdigest()
        self.redirect(destination, f'{SESSION_COOKIE}={value}.{signature}; Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSION_SECONDS}')

    def change_password(self, fields, changing):
        token = fields.get('token', [''])[0]
        if not hmac.compare_digest(token.encode(), self.server.password_setup_token.encode()):
            self.send_error(403, 'Reopen the password setup page and try again.')
            return
        password = fields.get('password', [''])[0]
        confirmation = fields.get('confirm', [''])[0]
        if not MIN_PERSONAL_PASSWORD <= len(password) <= 128 or not password.isprintable() or not password.strip():
            self.send_auth_page('setup-password.html', f'Use {MIN_PERSONAL_PASSWORD} to 128 printable characters.', 400, changing=changing)
            return
        if password != confirmation:
            self.send_auth_page('setup-password.html', 'The passwords don’t match. Enter the same password twice.', 400, changing=changing)
            return
        with self.server.password_setup_lock:
            if changing == self.server.password_setup_required or not self.authorized():
                self.require_login()
                return
            if changing and not self.valid_credentials(self.server.admin_user, fields.get('current', [''])[0]):
                self.send_auth_page('setup-password.html', 'The current password is incorrect.', 400, changing=True)
                return
            if self.valid_credentials(self.server.admin_user, password):
                self.send_auth_page('setup-password.html', 'Choose a different password.', 400, changing=changing)
                return
            try:
                verifier = PasswordVerifier.create(password)
                save_admin_password(self.server.admin_state_file, verifier.encoded)
            except OSError:
                self.send_auth_page('setup-password.html', 'Could not save the password. Check the dashboard’s storage and try again.', 500, changing=changing)
                return
            self.server.password_verifier = verifier
            self.server.admin_password = ''
            self.server.password_setup_required = False
            self.server.session_key = secrets.token_bytes(32)
        self.redirect('/login?password-set=1', f'{SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0')

    def require_login(self):
        form_login = self.server.admin_state_file is not None
        if form_login and not self.path.startswith('/api/'):
            self.redirect('/login')
            return
        self.send_response(401)
        if form_login:
            self.send_header('X-MartinFlix-Login', 'required')
        else:
            self.send_header('WWW-Authenticate', 'Basic realm="MartinFlix admin", charset="UTF-8"')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', '0')
        self.end_headers()

    def redirect(self, location, cookie=None):
        self.send_response(303)
        self.send_header('Location', location)
        if cookie:
            self.send_header('Set-Cookie', cookie)
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', '0')
        self.end_headers()

    def send_auth_page(self, name, message='', status=200, password_set=False, changing=False):
        template = (WEB / name).read_text()
        error = f'<p class="setup-error" role="alert">{html.escape(message)}</p>' if message else ''
        notice = '<p class="setup-success" role="status">Password saved. Sign in with your new password.</p>' if password_set else ''
        current = ('<label for="current">Current password</label><div class="password-field">'
                   '<input type="password" id="current" name="current" autocomplete="current-password" required>'
                   '<button type="button" class="password-toggle" aria-controls="current" aria-label="Show current password" '
                   'aria-pressed="false" hidden><span class="password-eye" aria-hidden="true"></span></button></div>')
        body = (template.replace('{{ACTION}}', '/change-password' if changing else '/setup-password')
                .replace('{{HEADING}}', 'Change your dashboard password' if changing else 'Set your dashboard password')
                .replace('{{CURRENT}}', current if changing else '')
                .replace('{{PASSWORD_SCOPE}}', '<p class="setup-note">This changes only the MartinFlix dashboard password.</p>'
                         '<a href="/">Back to dashboard</a>' if changing else '')
                .replace('{{TOKEN}}', html.escape(self.server.password_setup_token))
                .replace('{{MIN_PASSWORD}}', str(MIN_PERSONAL_PASSWORD))
                .replace('{{NOTICE}}', notice).replace('{{ERROR}}', error).encode())
        self.send_web_file(name, 'text/html', status, body)

    def send_web_file(self, name, content_type, status=200, body=None):
        if body is None:
            body = (WEB / name).read_bytes()
        self.send_response(status)
        self.send_header('Content-Type', content_type + '; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        self.end_headers()
        self.wfile.write(body)

    def session_authorized(self):
        try:
            cookie = SimpleCookie(self.headers.get('Cookie', ''))[SESSION_COOKIE].value
            match = re.fullmatch(r'([0-9]{1,12})\.([A-Za-z0-9_-]{32})\.([a-f0-9]{64})', cookie)
            if not match:
                return False
            expiry, nonce, signature = match.groups()
        except (KeyError, CookieError):
            return False
        if int(expiry) <= time.time():
            return False
        expected = hmac.new(self.server.session_key, f'{expiry}.{nonce}'.encode(), 'sha256').hexdigest()
        return hmac.compare_digest(signature, expected)

    def authorized(self):
        if getattr(self.server, 'admin_state_file', None) is not None:
            return self.session_authorized()
        header = self.headers.get('Authorization', '')
        try:
            scheme, encoded = header.split(' ', 1)
            if scheme.lower() != 'basic':
                return False
            supplied = base64.b64decode(encoded, validate=True).decode('utf-8')
            user, password = supplied.split(':', 1)
        except (ValueError, UnicodeDecodeError, binascii.Error):
            return False
        return self.valid_credentials(user, password)

    def valid_credentials(self, user, password):
        if not hmac.compare_digest(user.encode(), self.server.admin_user.encode()):
            return False
        verifier = getattr(self.server, 'password_verifier', None)
        if verifier:
            return verifier.matches(password)
        return hmac.compare_digest(password.encode(), self.server.admin_password.encode())


    def send_json(self, data):
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)


class PasswordVerifier:
    def __init__(self, encoded):
        salt, digest = encoded.split(':', 1)
        self.salt = bytes.fromhex(salt)
        self.digest = bytes.fromhex(digest)
        if len(self.salt) != 16 or len(self.digest) != 32:
            raise ValueError('Invalid password verifier.')
        self.encoded = encoded
        self.cache_key = secrets.token_bytes(32)
        self.cached_match = b''
        self.lock = threading.Lock()

    @staticmethod
    def derive(password, salt):
        return hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1, dklen=32)

    @classmethod
    def create(cls, password):
        salt = secrets.token_bytes(16)
        return cls(f'{salt.hex()}:{cls.derive(password, salt).hex()}')

    def matches(self, password):
        if len(password) > 128:
            return False
        signature = hmac.digest(self.cache_key, password.encode(), 'sha256')
        if hmac.compare_digest(signature, self.cached_match):
            return True
        # Limit concurrent scrypt operations and cache the last successful password check.
        with self.lock:
            if hmac.compare_digest(signature, self.cached_match):
                return True
            if not hmac.compare_digest(self.derive(password, self.salt), self.digest):
                return False
            self.cached_match = signature
            return True


def save_admin_password(path, encoded):
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, delete=False) as output:
        temporary = Path(output.name)
        output.write(encoded + '\n')
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


if __name__ == '__main__':
    user = os.environ.get('MARTINFLIX_ADMIN_USER', 'admin')
    state = os.environ.get('MARTINFLIX_ADMIN_STATE_FILE')
    state_file = Path(state) if state else None
    setup_required = False
    verifier = None
    if state_file is None:
        password = read_secret('MARTINFLIX_ADMIN_PASSWORD')
    else:
        try:
            verifier = PasswordVerifier(state_file.read_text(encoding='utf-8').strip())
            password = ''
        except FileNotFoundError:
            password = read_secret('MARTINFLIX_ADMIN_PASSWORD')
            setup_required = True
    if not user or ':' in user or (verifier is None and len(password) < 16):
        raise SystemExit('Configure an admin username and a password of at least 16 characters.')
    port = int(os.environ.get('MARTINFLIX_PORT', '8765'))
    host = os.environ.get('MARTINFLIX_BIND', '127.0.0.1')
    server = ThreadingHTTPServer((host, port), Handler)
    server.admin_user = user
    server.admin_password = password
    server.password_verifier = verifier
    server.admin_state_file = state_file
    server.password_setup_required = setup_required
    server.password_setup_token = secrets.token_urlsafe(32)
    server.password_setup_lock = threading.Lock()
    server.session_key = secrets.token_bytes(32)
    print('MartinFlix listening on %s:%d' % (host, port), flush=True)
    server.serve_forever()
