#!/usr/bin/env python3
"""Local, read-only MartinFlix control room. No third-party dependencies."""
import json
import base64
import binascii
import calendar
from datetime import date, datetime, timezone
import hmac
import os
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WEB = ROOT / 'web'
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
    if os.environ.get('MARTINFLIX_DOCKER_STATUS_DISABLED') == '1':
        return {'available': False, 'error': 'Docker status collection is not configured for this installation.', 'groups': []}
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


def arr_connection(kind):
    key = read_secret('MARTINFLIX_' + kind.upper() + '_KEY')
    base = os.environ.get('MARTINFLIX_' + kind.upper() + '_URL', 'http://127.0.0.1:' + ('8989' if kind == 'sonarr' else '7878'))
    if not key:
        return None
    # The first-run Compose project has fixed Arr service names on its private bridge.
    compose_address = {'sonarr': 'http://sonarr:8989', 'radarr': 'http://radarr:7878'}
    if os.environ.get('MARTINFLIX_ARR_COMPOSE_NETWORK') == '1' and base == compose_address.get(kind):
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
        if not self.authorized():
            self.send_response(401)
            self.send_header('WWW-Authenticate', 'Basic realm="MartinFlix admin", charset="UTF-8"')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        path = urllib.parse.urlsplit(self.path).path
        if path == '/api/status':
            data = {'docker': docker_status(), 'backups': backup_status(), 'time': int(time.time())}
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
            name = 'index.html' if path == '/' else path[1:]
            body = (WEB / name).read_bytes()
            self.send_response(200)
            self.send_header('Content-Type', {'index.html': 'text/html; charset=utf-8', 'app.js': 'text/javascript; charset=utf-8', 'style.css': 'text/css; charset=utf-8'}[name])
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'self'; base-uri 'none'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(body)
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

    def authorized(self):
        header = self.headers.get('Authorization', '')
        try:
            scheme, encoded = header.split(' ', 1)
            if scheme.lower() != 'basic':
                return False
            supplied = base64.b64decode(encoded, validate=True).decode('utf-8')
            user, password = supplied.split(':', 1)
        except (ValueError, UnicodeDecodeError, binascii.Error):
            return False
        return (hmac.compare_digest(user.encode(), self.server.admin_user.encode()) and
                hmac.compare_digest(password.encode(), self.server.admin_password.encode()))

    def send_json(self, data):
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)


if __name__ == '__main__':
    user = os.environ.get('MARTINFLIX_ADMIN_USER', 'admin')
    password = read_secret('MARTINFLIX_ADMIN_PASSWORD')
    if not user or ':' in user or len(password) < 16:
        raise SystemExit('Configure an admin username and a password of at least 16 characters.')
    port = int(os.environ.get('MARTINFLIX_PORT', '8765'))
    host = os.environ.get('MARTINFLIX_BIND', '127.0.0.1')
    server = ThreadingHTTPServer((host, port), Handler)
    server.admin_user = user
    server.admin_password = password
    print('MartinFlix listening on %s:%d' % (host, port), flush=True)
    server.serve_forever()
