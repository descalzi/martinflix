#!/usr/bin/env python3
"""Local, read-only MartinFlix control room. No third-party dependencies."""
import json
import base64
import binascii
import calendar
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


def service_status():
    if os.environ.get('MARTINFLIX_ARR_COMPOSE_NETWORK') != '1':
        return docker_status()

    probes = {name: (address + '/ping', 200) for name, address in ARR_COMPOSE_ADDRESSES.items()}
    downloads = os.environ.get('MARTINFLIX_DOWNLOAD_STACK') == '1'
    if downloads:
        probes.update({
            'nordvpn': ('http://gluetun:9999/', 200),
            'transmission-daemon': ('http://gluetun:9091/transmission/rpc', 401),
            'prowlarr': ('http://gluetun:9696/ping', 200),
        })
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
    return {'available': True, 'source': 'app-probes',
            'groups': [{'name': 'Downloads & automation' if downloads else 'Library apps', 'services': services}]}


def arr_connection(kind):
    key = read_secret('MARTINFLIX_' + kind.upper() + '_KEY')
    base = os.environ.get('MARTINFLIX_' + kind.upper() + '_URL', 'http://127.0.0.1:' + ('8989' if kind == 'sonarr' else '7878'))
    if not key:
        return None
    # The first-run Compose project has fixed Arr service names on its private bridge.
    if os.environ.get('MARTINFLIX_ARR_COMPOSE_NETWORK') == '1' and base == ARR_COMPOSE_ADDRESSES.get(kind):
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
        if path == '/api/status':
            data = {'docker': service_status(), 'backups': backup_status(), 'time': int(time.time())}
            self.send_json(data)
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
        elif path in ('/', '/app.js', '/style.css'):
            static = {
                '/': ('index.html', 'text/html'),
                '/app.js': ('app.js', 'text/javascript'),
                '/style.css': ('style.css', 'text/css'),
            }
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
        if self.server.admin_state_file is None or path not in ('/login', '/setup-password'):
            self.send_error(404)
            return
        try:
            origin = urllib.parse.urlsplit(self.headers.get('Origin', ''))
        except ValueError:
            origin = None
        if not origin or origin.scheme not in ('http', 'https') or origin.netloc != self.headers.get('Host'):
            self.send_error(403, 'Open the dashboard and submit the form again.')
            return
        if path == '/setup-password' and not self.authorized():
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
        if not self.server.password_setup_required:
            self.redirect('/')
            return
        self.change_initial_password(fields)

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

    def change_initial_password(self, fields):
        token = fields.get('token', [''])[0]
        if not hmac.compare_digest(token.encode(), self.server.password_setup_token.encode()):
            self.send_error(403, 'Reopen the password setup page and try again.')
            return
        password = fields.get('password', [''])[0]
        confirmation = fields.get('confirm', [''])[0]
        if not MIN_PERSONAL_PASSWORD <= len(password) <= 128 or not password.isprintable() or not password.strip():
            self.send_auth_page('setup-password.html', f'Use {MIN_PERSONAL_PASSWORD} to 128 printable characters.', 400)
            return
        if password != confirmation:
            self.send_auth_page('setup-password.html', 'The passwords don’t match. Enter the same password twice.', 400)
            return
        with self.server.password_setup_lock:
            if not self.server.password_setup_required:
                self.require_login()
                return
            if hmac.compare_digest(password.encode(), self.server.admin_password.encode()):
                self.send_auth_page('setup-password.html', 'Choose a password different from the temporary password.', 400)
                return
            try:
                verifier = PasswordVerifier.create(password)
                save_admin_password(self.server.admin_state_file, verifier.encoded)
            except OSError:
                self.send_auth_page('setup-password.html', 'Could not save the password. Check the dashboard’s storage and try again.', 500)
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

    def send_auth_page(self, name, message='', status=200, password_set=False):
        template = (WEB / name).read_text()
        error = f'<p class="setup-error" role="alert">{html.escape(message)}</p>' if message else ''
        notice = '<p class="setup-success" role="status">Password saved. Sign in with your new password.</p>' if password_set else ''
        body = (template.replace('{{TOKEN}}', html.escape(self.server.password_setup_token))
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
    server.password_setup_token = secrets.token_urlsafe(32) if setup_required else ''
    server.password_setup_lock = threading.Lock()
    server.session_key = secrets.token_bytes(32)
    print('MartinFlix listening on %s:%d' % (host, port), flush=True)
    server.serve_forever()
