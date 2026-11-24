#!/usr/bin/env python3
"""Local MartinFlix dashboard and installer-managed password setup."""
import json
import base64
import collections
import binascii
import calendar
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
import hashlib
import hmac
import html
import os
import re
import secrets
import subprocess
import sys
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
CATALOG = json.loads((WEB / 'catalog.json').read_text(encoding='utf-8'))
STACKS = {group['name']: group['services'] for group in CATALOG['groups']}
PAGES = ('overview', 'live', 'library', 'discover', 'add', 'services', 'backups', 'settings')


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


# The controller names services as Compose does; the dashboard's catalog uses the names from the reference host.
CONTROL_NAMES = {'nordvpn': 'gluetun', 'transmission-daemon': 'transmission'}
CATALOG_NAMES = {value: key for key, value in CONTROL_NAMES.items()}


class ControllerError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def controller_request(path, payload=None, timeout=5):
    """Call the controller container, the only part of the stack with Docker access. None when it isn't installed."""
    url = os.environ.get('MARTINFLIX_CONTROLLER_URL', '')
    token = read_secret('MARTINFLIX_CONTROLLER_TOKEN')
    if not url or not token:
        return None
    body = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(url.rstrip('/') + path, data=body, method='POST' if body else 'GET', headers={
        'Authorization': f'Bearer {token}', 'Content-Type': 'application/json', 'Accept': 'application/json'})
    try:
        with ARR_OPENER.open(request, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        try:
            message = json.load(exc).get('error')
        except (ValueError, AttributeError, OSError):
            message = None
        finally:
            exc.close()
        raise ControllerError(exc.code, message or 'The controller refused the request.') from None
    except (OSError, ValueError) as exc:
        raise ControllerError(502, 'The controller is not responding. Check that its container is running.') from exc


def controller_configured():
    return bool(os.environ.get('MARTINFLIX_CONTROLLER_URL') and read_secret('MARTINFLIX_CONTROLLER_TOKEN'))


def controller_status():
    try:
        result = controller_request('/status', timeout=4)
    except ControllerError:
        return None
    return result.get('services') if isinstance(result, dict) else None


def apply_control_status(services, control):
    """Fill in what only Docker knows: uptime for apps that do not report it, stopped containers, and what can be
    restarted from the dashboard along with the apps that follow it."""
    for service in services:
        info = control.get(CONTROL_NAMES.get(service['name'], service['name']))
        if not info or info.get('state') == 'missing':
            continue
        followers = [CATALOG_NAMES.get(name, name) for name in info.get('dependents', [])]
        service['control'] = {'followers': [name for name in followers if name in CATALOG['apps'] and name not in HELPERS],
                              'canUpdate': info.get('canUpdate', True)}
        if info.get('state') != 'running':
            service['state'] = 'exited' if info.get('state') in ('exited', 'dead') else info.get('state', 'unknown')
            code = info.get('exitCode')
            service['status'] = f'{info.get("state", "unknown").capitalize()}' + (f' (exit code {code})' if code else '')
            service.pop('uptime', None)
            continue
        if not service.get('startedAt') and info.get('startedAt'):
            service['startedAt'] = info['startedAt']
        if info.get('health') == 'starting':
            service['status'] = 'Up (health: starting)'
        elif info.get('health') == 'unhealthy':
            service['status'] = 'Up (unhealthy)'


# Containers that keep MartinFlix itself running, listed in their own section of the Services page.
HELPERS = ('dashboard', 'controller')


def helper_services(control):
    services = [{'name': name, 'state': 'running', 'status': 'Running'} for name in HELPERS
                if control.get(name, {}).get('state', 'missing') != 'missing']
    apply_control_status(services, control)
    return services


def control_message(result):
    def label(name):
        return CATALOG['apps'].get(CATALOG_NAMES.get(name, name), {}).get('name')
    name = label(result.get('service')) or 'The app'
    followed = [label(item) for item in result.get('followed', []) if CATALOG_NAMES.get(item, item) not in HELPERS]
    followed = [item for item in followed if item]
    listed = ', '.join(followed[:-1]) + (' and ' if len(followed) > 1 else '') + (followed[-1] if followed else '')
    if result.get('deferred'):
        return f'{name} is restarting. This can take a few seconds.'
    if result.get('action') == 'restart':
        return f'{name} restarted' + (f', then {listed}.' if followed else '.')
    if not result.get('updated'):
        return f'{name} already runs the newest image.'
    return f'{name} updated' + (f', and {listed} restarted with it.' if followed else '.')


GITHUB_API = 'https://api.github.com'
CHANGES_CACHE = {}
CHANGES_CACHE_SECONDS = 3600


HOME_ADDRESS = {'at': 0.0, 'value': None}


def home_address():
    """This network's own public address, as everything outside the VPN uses it. The dashboard isn't behind the VPN,
    so it sees the address the VPN should be hiding."""
    if time.time() - HOME_ADDRESS['at'] < 600 and HOME_ADDRESS['value']:
        return HOME_ADDRESS['value']
    try:
        request = urllib.request.Request('https://ipinfo.io/json', headers={'Accept': 'application/json',
                                                                            'User-Agent': 'MartinFlix dashboard'})
        with ARR_OPENER.open(request, timeout=10) as response:
            data = json.load(response)
        value = {key: data.get(key) for key in ('ip', 'city', 'country', 'org')} if isinstance(data.get('ip'), str) else None
    except (OSError, ValueError, AttributeError):
        value = None
    HOME_ADDRESS.update(at=time.time(), value=value)
    return value


def vpn_verdict(check, home):
    """Protected, exposed or down, from the controller's check and this network's own address."""
    exit_point = check.get('exit')
    if not exit_point:
        return 'down'
    if home and home.get('ip') == exit_point.get('ip'):
        return 'exposed'
    return 'protected'


def release_brief():
    """This MartinFlix version and any newer one on its channel, as the controller last saw them."""
    try:
        result = controller_request('/release/brief', timeout=4)
    except ControllerError:
        return None
    return result if isinstance(result, dict) else None


def release_changes(base, head):
    """What changed between two MartinFlix versions: the commits on GitHub, newest first. None if unknown."""
    commit = re.compile(r'[0-9a-f]{7,40}')
    if not (isinstance(base, str) and isinstance(head, str) and commit.fullmatch(base) and commit.fullmatch(head)):
        return None
    hit = CHANGES_CACHE.get((base, head))
    if hit and time.time() - hit[0] < CHANGES_CACHE_SECONDS:
        return hit[1]
    repository = os.environ.get('MARTINFLIX_REPOSITORY', 'descalzi/martinflix')
    request = urllib.request.Request(f'{GITHUB_API}/repos/{repository}/compare/{base}...{head}', headers={
        'Accept': 'application/vnd.github+json', 'User-Agent': 'MartinFlix dashboard'})
    try:
        with ARR_OPENER.open(request, timeout=10) as response:
            data = json.load(response)
        commits = [{'sha': item['sha'][:7], 'title': item['commit']['message'].splitlines()[0][:200],
                    'date': item['commit']['committer']['date']} for item in data.get('commits', [])]
        result = {'commits': commits[::-1][:40], 'total': data.get('total_commits', len(commits)),
                  'url': data.get('html_url'), 'older': data.get('status') == 'behind'}
    except (OSError, ValueError, KeyError, TypeError, AttributeError, IndexError):
        return None
    CHANGES_CACHE[(base, head)] = (time.time(), result)
    return result


def installer_downloads():
    """The download apps the installer enabled, whether they share Gluetun's network, and whether a VPN runs."""
    enabled = os.environ.get('MARTINFLIX_DOWNLOAD_APPS')
    if enabled is None:
        enabled = 'transmission,prowlarr,flaresolverr' if os.environ.get('MARTINFLIX_DOWNLOAD_STACK') == '1' else ''
    downloads = set(enabled.split(',')) - {''}
    gateway = os.environ.get('MARTINFLIX_DOWNLOAD_GATEWAY')
    vpn = os.environ.get('MARTINFLIX_VPN_ENABLED') == '1' or (bool(downloads) and gateway is None)
    behind_vpn = gateway == 'gluetun' or (gateway is None and vpn)
    return downloads, behind_vpn, vpn


def container_status():
    if os.environ.get('MARTINFLIX_ARR_COMPOSE_NETWORK') != '1':
        return docker_status()

    probes = {name: (address + '/ping', 200) for name, address in ARR_COMPOSE_ADDRESSES.items()}
    downloads, behind_vpn, vpn = installer_downloads()
    if vpn:
        probes['nordvpn'] = ('http://gluetun:9999/', 200)
    if 'transmission' in downloads:
        probes['transmission-daemon'] = (f'http://{"gluetun" if behind_vpn else "transmission"}:9091/transmission/rpc', 401)
    if 'prowlarr' in downloads:
        probes['prowlarr'] = (f'http://{"gluetun" if behind_vpn else "prowlarr"}:9696/ping', 200)
    if 'flaresolverr' in downloads:
        probes['flaresolverr'] = (f'http://{"gluetun" if behind_vpn else "flaresolverr"}:8191/', 200)
    if 'bazarr' in downloads:
        probes['bazarr'] = (f'http://{"gluetun" if behind_vpn else "bazarr"}:6767/api/system/ping', 200)
    # Plex runs on the host's network, so it is checked at its host address. /identity needs no token.
    if os.environ.get('MARTINFLIX_PLEX_INSTALLED') == '1' and os.environ.get('MARTINFLIX_PLEX_URL'):
        probes['plex'] = (os.environ['MARTINFLIX_PLEX_URL'].rstrip('/') + '/identity', 200)
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
    media_server = [service for service in services if service['name'] == 'plex']
    others = [service for service in services if service['name'] != 'plex']
    groups = [{'name': 'Media server', 'services': media_server}] if media_server else []
    groups.append({'name': 'Downloads & automation' if downloads else 'Library apps', 'services': others})
    return {'available': True, 'source': 'app-probes', 'downloadsBehindVpn': bool(downloads) and behind_vpn, 'groups': groups}


def arr_connection(kind, *, prowlarr_via_vpn=False):
    key = read_secret('MARTINFLIX_' + kind.upper() + '_KEY')
    if not key:
        return None
    default = f'http://127.0.0.1:{ARR_HEALTH_ENDPOINTS[kind][0]}'
    compose_base = ARR_COMPOSE_ADDRESSES.get(kind)
    if kind in ('prowlarr', 'bazarr') and os.environ.get('MARTINFLIX_ARR_COMPOSE_NETWORK') == '1':
        # Download apps share Gluetun's network when they route through the VPN.
        host = 'gluetun' if prowlarr_via_vpn else kind
        compose_base = default = f'http://{host}:{ARR_HEALTH_ENDPOINTS[kind][0]}'
    base = os.environ.get('MARTINFLIX_' + kind.upper() + '_URL', default)
    # The first-run Compose project has fixed Arr service names on its private bridge.
    if os.environ.get('MARTINFLIX_ARR_COMPOSE_NETWORK') == '1' and base == compose_base:
        return base, key
    parsed = urllib.parse.urlparse(base)
    if (parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1', 'localhost', '::1')
            or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment):
        raise ValueError('Only local HTTP service URLs are supported.')
    return base.rstrip('/'), key


def arr_open(connection, endpoint, *, payload=None, method=None, timeout=5):
    base, key = connection
    headers = {'X-Api-Key': key}
    body = None
    if payload is not None:
        headers['Content-Type'] = 'application/json'
        body = json.dumps(payload).encode()
    request = urllib.request.Request(base + endpoint, data=body, headers=headers, method=method)
    return ARR_OPENER.open(request, timeout=timeout)


ADMIN_APP_USER = 'admin'
SHARED_TRANSMISSION_CLIENT = 'MartinFlix Transmission'


def synced_apps():
    """The installer-managed apps that share the dashboard password, in the order they are updated."""
    if os.environ.get('MARTINFLIX_ARR_COMPOSE_NETWORK') != '1':
        return []
    downloads, _, _ = installer_downloads()
    apps = ['sonarr', 'radarr']
    if 'prowlarr' in downloads:
        apps.append('prowlarr')
    if 'bazarr' in downloads:
        apps.append('bazarr')
    if 'transmission' in downloads and os.environ.get('MARTINFLIX_TRANSMISSION_PASSWORD_FILE'):
        apps.append('transmission')
    return apps


def sync_app_passwords(password):
    """Give every installer-managed app the dashboard password.

    Everything is read and checked before anything is written, and every write is idempotent, so submitting the same
    password again after a partial failure finishes the job.
    """
    apps = synced_apps()
    _, behind_vpn, _ = installer_downloads()
    hosts = []
    for kind in (app for app in apps if app not in ('transmission', 'bazarr')):
        connection = arr_connection(kind, prowlarr_via_vpn=behind_vpn)
        if connection is None:
            raise ValueError('Missing application API key')
        version = 'v1' if kind == 'prowlarr' else 'v3'
        with arr_open(connection, f'/api/{version}/config/host') as response:
            config = json.load(response)
        if not isinstance(config, dict) or type(config.get('id')) is not int or config['id'] < 1:
            raise ValueError('Invalid application host settings')
        if not isinstance(config.get('username'), str) or not config['username'].strip():
            config.update(authenticationMethod='forms', authenticationRequired='enabled')
        # One login for every app: an app adopted from another stack keeps its own username until now.
        config.update(username=ADMIN_APP_USER, password=password, passwordConfirmation=password)
        hosts.append((connection, f'/api/{version}/config/host/{config["id"]}', config))
    bazarr = None
    if 'bazarr' in apps:
        bazarr = arr_connection('bazarr', prowlarr_via_vpn=behind_vpn)
        if bazarr is None:
            raise ValueError('Missing application API key')
        with arr_open(bazarr, '/api/system/settings') as response:
            if not isinstance(json.load(response), dict):
                raise ValueError('Invalid Bazarr settings')
    clients = []
    if 'transmission' in apps:
        # Sonarr, Radarr and Prowlarr each keep the Transmission login, to send releases to it.
        for kind in (*ARR_COMPOSE_ADDRESSES, *(['prowlarr'] if 'prowlarr' in apps else [])):
            connection = arr_connection(kind, prowlarr_via_vpn=behind_vpn) if kind == 'prowlarr' else arr_connection(kind)
            version = 'v1' if kind == 'prowlarr' else 'v3'
            with arr_open(connection, f'/api/{version}/downloadclient') as response:
                entries = json.load(response)
            for entry in entries if isinstance(entries, list) else []:
                if entry.get('implementation') == 'Transmission' and entry.get('name') == SHARED_TRANSMISSION_CLIENT:
                    for field in entry.get('fields', []):
                        if field.get('name') == 'username':
                            field['value'] = ADMIN_APP_USER
                        elif field.get('name') == 'password':
                            field['value'] = password
                    clients.append((connection, f'/api/{version}/downloadclient/{entry["id"]}?forceSave=true', entry))
    for connection, endpoint, payload in hosts:
        with arr_open(connection, endpoint, payload=payload, method='PUT'):
            pass
    if bazarr:
        # Bazarr's settings API takes form fields; it stores the password as its own MD5 hash.
        fields = urllib.parse.urlencode({'settings-auth-type': 'form', 'settings-auth-username': ADMIN_APP_USER,
                                         'settings-auth-password': password}).encode()
        base, key = bazarr
        request = urllib.request.Request(base + '/api/system/settings', data=fields, method='POST', headers={
            'X-Api-Key': key, 'Content-Type': 'application/x-www-form-urlencoded'})
        with ARR_OPENER.open(request, timeout=10):
            pass
    if 'transmission' in apps:
        # Transmission reads this bind-mounted file when it starts, so it is rewritten in place to stay mounted.
        with open(os.environ['MARTINFLIX_TRANSMISSION_PASSWORD_FILE'], 'r+', encoding='utf-8') as secret:
            secret.write(password)
            secret.truncate()
        # The controller restarts Transmission to apply it. Hosts without one run a helper that applies it instead.
        try:
            controller_request('/transmission-password', {}, timeout=300)
        except ControllerError as exc:
            raise ValueError(f'Transmission: {exc}') from None
    for connection, endpoint, payload in clients:
        with arr_open(connection, endpoint, payload=payload, method='PUT'):
            pass


def app_list():
    names = [CATALOG['apps'][{'transmission': 'transmission-daemon'}.get(app, app)]['name'] for app in synced_apps()]
    return ', '.join(names[:-1]) + ' and ' + names[-1] if len(names) > 1 else ''.join(names)


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
    all_services = [service for group in status['groups'] for service in group['services']]
    for service in all_services:
        app = CATALOG['apps'].get(service['name'], {})
        web = app.get('web')
        if web is None or service['state'] == 'missing':
            continue
        port = web['port']
        local_only = False
        installer = app.get('installer')
        if status.get('source') == 'app-probes' and installer:
            port = int(os.environ.get(f'MARTINFLIX_{installer["env"]}_WEB_PORT', port))
            # Download apps follow the installer's LAN address; older installs kept them on loopback.
            local_only = installer.get('localOnly', False) and \
                os.environ.get('MARTINFLIX_DOWNLOAD_BIND', '127.0.0.1') in ('', '127.0.0.1')
        service['web'] = {'port': port, 'scheme': web.get('scheme', 'http'),
                          'path': web.get('path', '/'), 'localOnly': local_only}
    behind_vpn = status.get('downloadsBehindVpn', False)
    up = [service for service in all_services if service['state'] in ('reachable', 'running')]
    for service in up:
        uptime = docker_uptime(service.get('status', ''))
        if uptime:
            service['uptime'] = uptime
    checked = [service for service in up if service['name'] in ARR_HEALTH_ENDPOINTS]
    timed = [service for service in up if service['name'] in START_TIME_SOURCES]
    if checked or timed:
        with ThreadPoolExecutor(max_workers=len(checked) + len(timed)) as pool:
            checks = [pool.submit(arr_health, service['name'], prowlarr_via_vpn=behind_vpn) for service in checked]
            starts = [pool.submit(app_started_at, service['name'], behind_vpn) for service in timed]
            for service, check in zip(checked, checks):
                service['health'] = check.result()
            for service, start in zip(timed, starts):
                if started := start.result():
                    service['startedAt'] = started
    control = controller_status()
    if control is not None:
        status['control'] = True
        apply_control_status(all_services, control)
        if helpers := helper_services(control):
            status['groups'].append({'name': 'MartinFlix', 'services': helpers})
    return status


UPTIME_PATTERN = re.compile(r'Up (.+?)(?: \((?:healthy|unhealthy|health: starting)\))?$')
START_TIME_SOURCES = ('sonarr', 'radarr', 'prowlarr', 'bazarr', 'transmission-daemon')


def docker_uptime(status):
    """Docker's own wording for how long a container has run, such as "3 days" or "About an hour"."""
    match = UPTIME_PATTERN.match(status)
    return match.group(1) if match else None


def app_started_at(name, behind_vpn):
    """When the app says it started, as UTC ISO time. Containers restart apps with them, so this is their uptime."""
    try:
        if name == 'transmission-daemon':
            seconds = transmission_seconds_active(behind_vpn)
            if seconds is None:
                return None
            return datetime.fromtimestamp(time.time() - seconds, timezone.utc).isoformat()
        connection = arr_connection(name, prowlarr_via_vpn=behind_vpn)
        if connection is None:
            return None
        if name == 'bazarr':
            with arr_open(connection, '/api/system/status') as response:
                started = json.load(response)['data']['start_time']
            if not isinstance(started, (int, float)) or isinstance(started, bool):
                return None
            return datetime.fromtimestamp(started, timezone.utc).isoformat()
        version = 'v1' if name == 'prowlarr' else 'v3'
        with arr_open(connection, f'/api/{version}/system/status') as response:
            started = next_airing(json.load(response).get('startTime'))
        return started.isoformat() if started else None
    except urllib.error.HTTPError as exc:
        exc.close()
    except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError):
        pass
    return None


TRANSMISSION_SESSION = {'id': ''}


def transmission_url(behind_vpn=None):
    """Where Transmission's RPC answers: set directly, or found on the installer's Compose network."""
    if url := os.environ.get('MARTINFLIX_TRANSMISSION_URL'):
        return url
    if os.environ.get('MARTINFLIX_ARR_COMPOSE_NETWORK') != '1' or not os.environ.get('MARTINFLIX_TRANSMISSION_PASSWORD_FILE'):
        return None
    if behind_vpn is None:
        downloads, behind_vpn, *_ = installer_downloads()
        if 'transmission' not in downloads:
            return None
    return f'http://{"gluetun" if behind_vpn else "transmission"}:9091/transmission/rpc'


def transmission_rpc(method, arguments=None, behind_vpn=None):
    """One Transmission RPC call with the shared login, renewing Transmission's anti-forgery session ID once."""
    url = transmission_url(behind_vpn)
    if not url:
        return None
    headers = {'Content-Type': 'application/json'}
    password = read_secret('MARTINFLIX_TRANSMISSION_PASSWORD')
    if password:
        user = os.environ.get('MARTINFLIX_TRANSMISSION_USER', ADMIN_APP_USER)
        headers['Authorization'] = 'Basic ' + base64.b64encode(f'{user}:{password}'.encode()).decode()
    body = json.dumps({'method': method, **({'arguments': arguments} if arguments else {})}).encode()
    for _ in range(2):
        if TRANSMISSION_SESSION['id']:
            headers['X-Transmission-Session-Id'] = TRANSMISSION_SESSION['id']
        try:
            with ARR_OPENER.open(urllib.request.Request(url, data=body, headers=headers, method='POST'), timeout=4) as response:
                result = json.load(response)
            if result.get('result') != 'success':
                raise ValueError(result.get('result') or 'Transmission refused the request')
            return result.get('arguments') or {}
        except urllib.error.HTTPError as exc:
            session = exc.headers.get('X-Transmission-Session-Id')
            exc.close()
            if exc.code != 409 or not session:
                raise
            TRANSMISSION_SESSION['id'] = session
    raise ValueError('Transmission kept asking for a new session ID')


def transmission_seconds_active(behind_vpn):
    """Transmission's session age, read with the shared login the installer gives it."""
    try:
        stats = transmission_rpc('session-stats', behind_vpn=behind_vpn)
        seconds = stats['current-stats']['secondsActive'] if stats else None
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return seconds if type(seconds) is int and seconds >= 0 else None


# Live transfer speeds: one sample every five seconds, kept for an hour, so the chart has history when opened.
SPEED_INTERVAL = 5
SPEED_SAMPLES = collections.deque(maxlen=720)
SPEED_LOCK = threading.Lock()


def sample_speeds_once():
    try:
        stats = transmission_rpc('session-stats')
    except (OSError, ValueError, TypeError):
        stats = None
    down = stats.get('downloadSpeed') if stats else None
    up = stats.get('uploadSpeed') if stats else None
    sample = [int(time.time()), down if type(down) is int else None, up if type(up) is int else None]
    with SPEED_LOCK:
        SPEED_SAMPLES.append(sample)
    return stats


def sample_speeds_forever():
    while True:
        if transmission_url():
            sample_speeds_once()
        time.sleep(SPEED_INTERVAL)


# Transmission's torrent states, from its RPC specification.
TORRENT_STATES = {0: 'paused', 1: 'checking', 2: 'checking', 3: 'queued', 4: 'downloading', 5: 'seeding', 6: 'seeding'}
QUEUE_CACHE = {'at': 0.0, 'value': {}}


def queued_titles():
    """Which torrent belongs to which episode or film, from Sonarr's and Radarr's queues, keyed by torrent hash."""
    if time.time() - QUEUE_CACHE['at'] < 15:
        return QUEUE_CACHE['value']
    titles = {}
    for kind, route in (('sonarr', '/api/v3/queue?pageSize=200&includeSeries=true&includeEpisode=true'),
                        ('radarr', '/api/v3/queue?pageSize=200&includeMovie=true')):
        connection = arr_connection(kind)
        if connection is None:
            continue
        try:
            records = (arr_json(connection, route) or {}).get('records') or []
        except (OSError, ValueError, AttributeError):
            continue
        for record in records:
            if not isinstance(record, dict) or not isinstance(record.get('downloadId'), str):
                continue
            key = record['downloadId'].lower()
            if kind == 'sonarr':
                series = record.get('series') or {}
                episode = record.get('episode') or {}
                entry = titles.setdefault(key, {'kind': kind, 'title': series.get('title'), 'mediaId': record.get('seriesId'),
                                                'episodes': []})
                season, number = episode.get('seasonNumber'), episode.get('episodeNumber')
                if type(season) is int and type(number) is int:
                    entry['episodes'].append((season, number, episode.get('title')))
            else:
                movie = record.get('movie') or {}
                titles[key] = {'kind': kind, 'title': movie.get('title'), 'year': movie.get('year'),
                               'mediaId': record.get('movieId'), 'episodes': []}
    QUEUE_CACHE.update(at=time.time(), value=titles)
    return titles


def episode_label(episodes):
    if not episodes:
        return ''
    episodes = sorted(set(episodes))
    if len(episodes) == 1:
        season, number, name = episodes[0]
        return f'S{season:02d}E{number:02d}' + (f' · {short_text(name, 60)}' if name else '')
    seasons = sorted({season for season, _, _ in episodes})
    span = f'Season {seasons[0]}' if len(seasons) == 1 else f'Seasons {seasons[0]}–{seasons[-1]}'
    return f'{span} · {len(episodes)} episodes'


def live_transfer():
    """Speeds, counts and the torrents still downloading, named after the titles they belong to."""
    if not transmission_url():
        return {'available': False}
    try:
        stats = transmission_rpc('session-stats') or {}
        torrents = (transmission_rpc('torrent-get', {'fields': [
            'hashString', 'name', 'status', 'percentDone', 'rateDownload', 'rateUpload', 'eta', 'sizeWhenDone', 'error']})
            or {}).get('torrents') or []
    except (OSError, ValueError, TypeError):
        return {'available': True, 'error': 'Transmission is not answering right now.'}
    counts = collections.Counter(TORRENT_STATES.get(item.get('status'), 'paused') for item in torrents if isinstance(item, dict))
    total = stats.get('cumulative-stats') or {}
    downloaded, uploaded = total.get('downloadedBytes'), total.get('uploadedBytes')
    titles = queued_titles()
    downloads = []
    for item in torrents:
        if not isinstance(item, dict) or TORRENT_STATES.get(item.get('status')) not in ('downloading', 'queued', 'checking'):
            continue
        match = titles.get(str(item.get('hashString', '')).lower())
        media = match.get('mediaId') if match else None
        detail = (episode_label(match['episodes']) if match and match['kind'] == 'sonarr'
                  else str(match.get('year') or '') if match else '')
        percent = item.get('percentDone')
        eta = item.get('eta')
        downloads.append({
            'title': short_text((match or {}).get('title') or item.get('name') or 'Unnamed download', 120),
            'detail': detail, 'matched': bool(match), 'state': TORRENT_STATES.get(item.get('status')),
            'percent': round(percent * 100, 1) if isinstance(percent, (int, float)) else 0,
            'down': item.get('rateDownload') if type(item.get('rateDownload')) is int else 0,
            'eta': eta if type(eta) is int and eta >= 0 else None,
            'size': item.get('sizeWhenDone') if type(item.get('sizeWhenDone')) is int else None,
            'poster': f'/api/posters/{match["kind"]}/{media}' if match and type(media) is int and media > 0 else None,
            'problem': bool(item.get('error')),
        })
    downloads.sort(key=lambda entry: (entry['state'] != 'downloading', -entry['down'], entry['title']))
    with SPEED_LOCK:
        samples = list(SPEED_SAMPLES)
    return {
        'available': True,
        'down': stats.get('downloadSpeed') if type(stats.get('downloadSpeed')) is int else 0,
        'up': stats.get('uploadSpeed') if type(stats.get('uploadSpeed')) is int else 0,
        'downloading': counts['downloading'], 'queued': counts['queued'] + counts['checking'],
        'seeding': counts['seeding'], 'paused': counts['paused'],
        'ratio': round(uploaded / downloaded, 2) if type(downloaded) is int and downloaded > 0 and type(uploaded) is int else None,
        'samples': samples, 'interval': SPEED_INTERVAL, 'downloads': downloads[:30],
    }


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


RELEASE_EVENTS = (('inCinemas', 'In cinemas'), ('digitalRelease', 'Digital'), ('physicalRelease', 'Physical'))


def next_release(item, today):
    """The earliest release event still ahead, such as a digital release after a cinema run."""
    upcoming = []
    for field, label in RELEASE_EVENTS:
        value = item.get(field)
        if isinstance(value, str):
            try:
                when = date.fromisoformat(value[:10])
            except ValueError:
                continue
            if when >= today:
                upcoming.append((when, label))
    if not upcoming:
        return None
    when, label = min(upcoming)
    return {'type': label, 'date': when.isoformat()}


RELEASE_LABELS = dict(RELEASE_EVENTS)


def film_availability(item, today):
    """When Radarr can download a film, which follows the film's minimum availability, as Radarr's Missing list
    does: announced (straight away), in cinemas, or released (the digital or physical release, whichever comes first;
    without either, 90 days after the cinema release, as Radarr assumes). Radarr's own isAvailable has the last say."""
    dates = {}
    for field, _ in RELEASE_EVENTS:
        value = item.get(field)
        if isinstance(value, str):
            try:
                dates[field] = date.fromisoformat(value[:10])
            except ValueError:
                continue
    minimum = item.get('minimumAvailability')
    if minimum == 'announced':
        candidates = []
    elif minimum == 'inCinemas':
        candidates = [(dates['inCinemas'], 'In cinemas')] if 'inCinemas' in dates else []
    else:
        candidates = [(dates[field], RELEASE_LABELS[field]) for field in ('digitalRelease', 'physicalRelease') if field in dates]
        if not candidates and 'inCinemas' in dates:
            candidates = [(dates['inCinemas'] + timedelta(days=90), 'Expected release')]
    when, label = min(candidates) if candidates else (None, None)
    available = item.get('isAvailable')
    if type(available) is not bool:
        available = minimum == 'announced' or (when is not None and when <= today)
    return {'type': label, 'date': when.isoformat() if when else None, 'available': available}


def next_episodes(episodes, now):
    """The first monitored episode still to air for each series, keyed by series ID."""
    result = {}
    for episode in episodes if isinstance(episodes, list) else []:
        if not isinstance(episode, dict) or type(episode.get('seriesId')) is not int:
            continue
        airing = next_airing(episode.get('airDateUtc'))
        if not airing or airing < now or episode.get('monitored') is False:
            continue
        best = result.get(episode['seriesId'])
        if best is None or airing < best[0]:
            result[episode['seriesId']] = (airing, {
                'season': episode.get('seasonNumber'), 'number': episode.get('episodeNumber'),
                'title': episode.get('title') if isinstance(episode.get('title'), str) else None,
            })
    return {series: details for series, (_, details) in result.items()}


# How long after an episode airs, or a film becomes available, before its absence counts as a problem. Releases
# take a while to appear and download, and Radarr waits a day after availability as setup configures it.
MISSING_GRACE = timedelta(days=1)


def arr_problems(kind, connection, now):
    """What Sonarr or Radarr should have by now but doesn't, leaving out what it is downloading. For Sonarr, the
    missing episodes of each series, by series ID; for Radarr, the IDs of the films it is downloading."""
    queue = (arr_json(connection, '/api/v3/queue?pageSize=500', timeout=10) or {}).get('records') or []
    if kind == 'radarr':
        return {'downloading': {record.get('movieId') for record in queue if isinstance(record, dict)}}
    queued = {record.get('episodeId') for record in queue if isinstance(record, dict)}
    wanted = arr_json(connection, '/api/v3/wanted/missing?' + urllib.parse.urlencode(
        {'page': 1, 'pageSize': 1000, 'monitored': 'true'}), timeout=10) or {}
    missing = {}
    for record in wanted.get('records') or []:
        if not isinstance(record, dict) or type(record.get('seriesId')) is not int or record.get('id') in queued:
            continue
        aired = next_airing(record.get('airDateUtc'))
        if aired is None or now - aired < MISSING_GRACE:
            continue
        entry = missing.setdefault(record['seriesId'], {'count': 0, 'since': aired, 'first': None})
        entry['count'] += 1
        if aired <= entry['since']:
            entry['since'] = aired
            entry['first'] = {'season': record.get('seasonNumber'), 'number': record.get('episodeNumber')}
    for entry in missing.values():
        entry['since'] = entry['since'].isoformat()
    return {'missing': missing}


IMDB_ID = re.compile(r'tt[0-9]{5,10}')


def title_links(kind, item):
    """Where to read about a series or film: IMDb and TMDB, or TheTVDB for a series TMDB doesn't know."""
    links = {}
    imdb, tmdb, tvdb = item.get('imdbId'), item.get('tmdbId'), item.get('tvdbId')
    if isinstance(imdb, str) and IMDB_ID.fullmatch(imdb):
        links['imdb'] = f'https://www.imdb.com/title/{imdb}/'
    if type(tmdb) is int and 0 < tmdb < 100_000_000:
        links['tmdb'] = f'https://www.themoviedb.org/{"tv" if kind == "sonarr" else "movie"}/{tmdb}'
    elif kind == 'sonarr' and type(tvdb) is int and 0 < tvdb < 100_000_000:
        links['tvdb'] = f'https://thetvdb.com/dereferrer/series/{tvdb}'
    return links


def arr_items(kind, raw, now, episodes=None, problems=None, everything=False):
    """The titles worth showing: by default those coming up or with something missing; with `everything`, all of them."""
    last_day = calendar.monthrange(now.year + 1, now.month)[1]
    deadline = now.replace(year=now.year + 1, day=min(now.day, last_day))
    problems = problems or {}
    items = []
    for item in raw:
        monitored = bool(item.get('monitored'))
        if not monitored and not everything:
            continue
        airing = None
        status = item.get('status') or ''
        missing = (problems.get('missing') or {}).get(item.get('id')) if kind == 'sonarr' and monitored else None
        if kind == 'sonarr':
            airing = next_airing(item.get('nextAiring'))
            upcoming = monitored and status.casefold() in ('upcoming', 'continuing') and airing and now <= airing <= deadline
            if not upcoming:
                airing = None
            if not upcoming and not missing and not everything:
                continue
        elif item.get('hasFile') is not False and not everything:
            continue

        media_id = item.get('id')
        has_poster = any(image.get('coverType') == 'poster' for image in (item.get('images') or []))
        poster = (f'/api/posters/{kind}/{media_id}'
                  if type(media_id) is int and 0 < media_id <= 2147483647 and has_poster else None)
        entry = {
            'id': media_id if type(media_id) is int and 0 < media_id <= 2147483647 else None,
            'title': item.get('title') or 'Untitled',
            'year': item.get('year'),
            'status': status,
            'monitored': monitored,
            'nextAiring': airing.isoformat() if airing else None,
            'releaseDate': movie_release(item) if kind == 'radarr' else None,
            'poster': poster,
        }
        entry['links'] = title_links(kind, item)
        if everything:
            # What deleting it would free, and what it holds.
            statistics = item.get('statistics') if isinstance(item.get('statistics'), dict) else {}
            size = statistics.get('sizeOnDisk') if kind == 'sonarr' else item.get('sizeOnDisk')
            entry['size'] = size if type(size) is int and size > 0 else 0
            if kind == 'sonarr':
                files, total = statistics.get('episodeFileCount'), statistics.get('totalEpisodeCount')
                entry['episodes'] = {'files': files if type(files) is int else 0, 'total': total if type(total) is int else 0}
            else:
                entry['downloaded'] = bool(item.get('hasFile'))
        if kind == 'sonarr' and episodes and media_id in episodes:
            entry['nextEpisode'] = episodes[media_id]
        if missing:
            entry['missing'] = missing
        if kind == 'radarr':
            entry['nextRelease'] = next_release(item, now.date())
            availability = film_availability(item, now.date())
            entry['availability'] = availability
            entry['downloading'] = media_id in (problems.get('downloading') or ())
            # Past the day Radarr could get it, with nothing downloading: worth a look.
            if monitored and item.get('hasFile') is False and availability['available'] and not entry['downloading'] and (
                    availability['date'] is None or date.fromisoformat(availability['date']) <= now.date() - MISSING_GRACE):
                entry['overdue'] = True
        items.append(entry)
    if everything:
        # The whole library, as the apps list it, by title.
        return sorted(items, key=lambda item: (item['title'].casefold().removeprefix('the '), item['year'] or 0))
    if kind == 'radarr':
        # As Radarr's Missing list, with overdue films first, then those available now, then the rest by date.
        return sorted(items, key=lambda item: (not item.get('overdue'), not item['availability']['available'],
                                               item['availability']['date'] is None, item['availability']['date'] or '',
                                               item['title'].casefold()))
    # Series with missing episodes first, then by the next episode.
    return sorted(items, key=lambda item: (not item.get('missing'), item['nextAiring'] is None, item['nextAiring'] or '',
                                           item['title'].casefold()))


def arr_data(kind, everything=False):
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
        now = datetime.now(timezone.utc)
        episodes = None
        if kind == 'sonarr':
            # Episode numbers are a nicety: the series list still works if the calendar cannot be read.
            try:
                query = urllib.parse.urlencode({'start': now.isoformat(), 'end': (now + timedelta(days=366)).isoformat()})
                with arr_open(connection, '/api/v3/calendar?' + query) as response:
                    episodes = next_episodes(json.load(response), now)
            except (OSError, ValueError, TypeError):
                episodes = None
        # Warnings are a nicety too: without them the lists still show.
        try:
            problems = arr_problems(kind, connection, now)
        except (OSError, ValueError, TypeError, AttributeError):
            problems = None
        return {'configured': True, 'items': arr_items(kind, raw, now, episodes, problems, everything)}
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


# Plex, read directly with an X-Plex-Token: what is playing now and the recent watch history.
PLEX_CACHE = {}
PLEX_ART = re.compile(r'/library/metadata/[0-9]{1,10}/(?:thumb|art)/[0-9]{1,12}')


def plex_configured():
    return bool(os.environ.get('MARTINFLIX_PLEX_URL') and read_secret('MARTINFLIX_PLEX_TOKEN'))


def plex_open(path, params=None, accept='application/json', timeout=5):
    base = os.environ.get('MARTINFLIX_PLEX_URL', '').rstrip('/')
    query = f'?{urllib.parse.urlencode(params)}' if params else ''
    request = urllib.request.Request(base + path + query, headers={
        'Accept': accept, 'X-Plex-Token': read_secret('MARTINFLIX_PLEX_TOKEN'),
        'X-Plex-Client-Identifier': 'martinflix-dashboard', 'X-Plex-Product': 'MartinFlix'})
    return ARR_OPENER.open(request, timeout=timeout)


def plex_json(path, params=None, cache_seconds=0):
    key = (path, tuple(sorted((params or {}).items())))
    hit = PLEX_CACHE.get(key)
    if cache_seconds and hit and time.time() - hit[0] < cache_seconds:
        return hit[1]
    with plex_open(path, params) as response:
        data = json.load(response).get('MediaContainer') or {}
    if cache_seconds:
        PLEX_CACHE[key] = (time.time(), data)
    return data


def plex_art(path):
    """A poster through the dashboard, so the browser never sees the Plex token. Only library artwork paths pass."""
    if not plex_configured() or not PLEX_ART.fullmatch(path):
        return None
    params = {'width': 240, 'height': 360, 'minSize': 1, 'upscale': 1, 'url': path}
    try:
        with plex_open('/photo/:/transcode', params, accept='image/jpeg', timeout=8) as response:
            kind = response.headers.get_content_type()
            if kind not in ('image/jpeg', 'image/png', 'image/webp'):
                return None
            body = response.read(1024 * 1024 + 1)
    except (OSError, ValueError):
        return None
    return (body, kind) if len(body) <= 1024 * 1024 else None


def plex_title(item):
    """Series name and "S02E05 · Episode" for episodes; the title and year for films."""
    if item.get('type') == 'episode':
        season, number = item.get('parentIndex'), item.get('index')
        code = f'S{season:02d}E{number:02d}' if type(season) is int and type(number) is int else ''
        name = short_text(item.get('title'), 80)
        return short_text(item.get('grandparentTitle'), 120) or 'Untitled', ' · '.join(part for part in (code, name) if part)
    year = item.get('year')
    return short_text(item.get('title'), 120) or 'Untitled', str(year) if type(year) is int else ''


def plex_poster(item):
    art = item.get('grandparentThumb') if item.get('type') == 'episode' else item.get('thumb')
    art = art or item.get('thumb')
    return f'/api/plex-art?{urllib.parse.urlencode({"path": art})}' if isinstance(art, str) and PLEX_ART.fullmatch(art) else None


def plex_sessions():
    data = plex_json('/status/sessions')
    sessions = []
    for item in data.get('Metadata') or []:
        if not isinstance(item, dict):
            continue
        title, detail = plex_title(item)
        player = item.get('Player') or {}
        transcode = item.get('TranscodeSession') or {}
        decision = transcode.get('videoDecision') or transcode.get('audioDecision')
        offset, duration = item.get('viewOffset'), item.get('duration')
        sessions.append({
            'title': title, 'detail': detail, 'poster': plex_poster(item),
            'user': short_text((item.get('User') or {}).get('title'), 60) or 'Someone',
            'device': short_text(player.get('title') or player.get('product'), 60),
            'state': player.get('state') if player.get('state') in ('playing', 'paused', 'buffering') else 'playing',
            'percent': round(offset / duration * 100) if type(offset) is int and type(duration) is int and duration > 0 else None,
            'stream': {'transcode': 'Transcoding', 'copy': 'Direct stream'}.get(decision, 'Direct play'),
            'local': player.get('local') is True,
            # For the play log: who, and which title.
            'account': (item.get('User') or {}).get('id'), 'ratingKey': item.get('ratingKey'),
        })
    return sessions


def plex_accounts():
    accounts = plex_json('/accounts', cache_seconds=3600)
    return {item.get('id'): short_text(item.get('name'), 60) for item in accounts.get('Account') or []
            if isinstance(item, dict) and type(item.get('id')) is int}


def plex_history(limit=24):
    data = plex_json('/status/sessions/history/all', {'sort': 'viewedAt:desc', 'X-Plex-Container-Start': 0,
                                                      'X-Plex-Container-Size': limit}, cache_seconds=60)
    try:
        names = plex_accounts()
    except (OSError, ValueError):
        names = {}
    history = []
    for item in data.get('Metadata') or []:
        if not isinstance(item, dict):
            continue
        title, detail = plex_title(item)
        viewed = item.get('viewedAt')
        history.append({'title': title, 'detail': detail, 'poster': plex_poster(item),
                        'user': names.get(item.get('accountID')) or 'Someone',
                        'watchedAt': viewed if type(viewed) is int else None,
                        'account': item.get('accountID'), 'ratingKey': item.get('ratingKey')})
    return history


# The dashboard's own log of plays. Plex's history keeps only plays watched to the end, and misses some; every five
# minutes the dashboard notes what is playing, so partial plays are known too. Plex's finished views are merged in.
PLAY_LOG_EVERY = 300
# A play seen again within this long is the same play carrying on. So is one picked up again within a few hours from
# where it was or further on, after a pause. Anything else, such as the next day or starting over, is a new play.
PLAY_GAP = 15 * 60
PLAY_RESUME = 6 * 3600
PLAY_LOG_KEEP = 5000


def play_key(account, rating):
    """Who and which title, as both Plex's sessions and its history name them."""
    return (str(account) if account is not None else '', str(rating) if rating is not None else '')


class PlayLog:
    """The plays, in a file the dashboard and a one-off import (import-tautulli) can both add to: each change reads
    the file again first, under a lock, so neither overwrites the other."""

    def __init__(self, path):
        self.path = Path(path) if path else None
        self.lock = threading.Lock()
        self.stamp = None
        self.plays = []
        self.refresh()

    def refresh(self):
        """Read the file again if something else changed it."""
        if self.path is None:
            return
        try:
            stamp = self.path.stat().st_mtime_ns
        except OSError:
            return
        if stamp == self.stamp:
            return
        try:
            plays = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return
        self.stamp = stamp
        self.plays = [play for play in plays if isinstance(play, dict) and isinstance(play.get('seen'), int)] \
            if isinstance(plays, list) else []

    def save(self):
        if self.path is None:
            return
        self.plays.sort(key=lambda play: play['seen'])
        self.plays = self.plays[-PLAY_LOG_KEEP:]
        temporary = self.path.with_name(f'.plays.{secrets.token_hex(4)}')
        try:
            temporary.write_text(json.dumps(self.plays))
            temporary.replace(self.path)
            self.stamp = self.path.stat().st_mtime_ns
        except OSError:
            temporary.unlink(missing_ok=True)

    def changing(self):
        """Hold the file for a change, across processes."""
        import contextlib
        import fcntl

        @contextlib.contextmanager
        def held():
            with self.lock:
                if self.path is None:
                    yield
                    return
                with open(self.path.with_name('.plays.lock'), 'a') as handle:
                    fcntl.flock(handle, fcntl.LOCK_EX)
                    self.refresh()
                    yield
                    self.save()
        return held()

    def add(self, plays):
        """Add plays from elsewhere (an import), skipping any already here. Returns how many were new."""
        added = 0
        with self.changing():
            known = {(play.get('ratingKey'), play.get('started') // 60 if isinstance(play.get('started'), int) else None)
                     for play in self.plays}
            for play in plays:
                if (play['ratingKey'], play['started'] // 60) not in known:
                    self.plays.append(play)
                    known.add((play['ratingKey'], play['started'] // 60))
                    added += 1
        return added

    def record(self, sessions, now=None):
        """Note what is playing now: a play seen recently carries on, anything else starts a new one."""
        now = int(now if now is not None else time.time())
        with self.changing():
            for session in sessions:
                key = play_key(session.get('account'), session.get('ratingKey'))
                if not key[1]:
                    continue
                percent = session.get('percent') if type(session.get('percent')) is int else 0
                play = next((play for play in reversed(self.plays) if play_key(play.get('account'), play.get('ratingKey')) == key
                             and (now - play['seen'] <= PLAY_GAP or
                                  (now - play['seen'] <= PLAY_RESUME and percent >= play.get('progress', 0) - 2))), None)
                if play is None:
                    play = {'account': key[0], 'ratingKey': key[1], 'started': now, 'progress': 0}
                    self.plays.append(play)
                play.update(seen=now, progress=max(play.get('progress', 0), min(100, max(0, percent))),
                            title=session.get('title'), detail=session.get('detail'), poster=session.get('poster'),
                            user=session.get('user'))

    def merged(self, history, limit=30):
        """The log and Plex's finished views together: a play Plex says was finished shows as finished, and a
        finished view the log never saw still shows."""
        with self.lock:
            self.refresh()
            plays = [dict(play) for play in self.plays]
        rows = [{'title': play.get('title') or 'Untitled', 'detail': play.get('detail') or '', 'poster': play.get('poster'),
                 'user': play.get('user') or 'Someone', 'watchedAt': play['seen'], 'startedAt': play.get('started'),
                 'progress': 100 if play.get('finished') else play.get('progress', 0), 'finished': bool(play.get('finished')),
                 'imported': bool(play.get('imported')), 'key': play_key(play.get('account'), play.get('ratingKey'))}
                for play in plays]
        for view in history:
            viewed = view.get('watchedAt')
            key = play_key(view.get('account'), view.get('ratingKey'))
            match = None
            if type(viewed) is int:
                # Tautulli names the server's owner by their plex.tv account, Plex by 1: an imported play matches by
                # title and time alone.
                match = next((row for row in rows if (row['key'] == key or (row.get('imported') and row['key'][1] == key[1]))
                              and type(row.get('watchedAt')) is int
                              and (row.get('startedAt') or row['watchedAt']) - PLAY_GAP <= viewed <= row['watchedAt'] + PLAY_GAP), None)
            if match:
                if not match['finished']:
                    match.update(finished=True, progress=100, watchedAt=max(match['watchedAt'], viewed))
            else:
                rows.append({**{name: view.get(name) for name in ('title', 'detail', 'poster', 'user', 'watchedAt')},
                             'progress': 100, 'finished': True, 'imported': False, 'startedAt': None, 'key': key})
        rows.sort(key=lambda row: row.get('watchedAt') or 0, reverse=True)
        for row in rows:
            for name in ('key', 'startedAt', 'imported'):
                row.pop(name, None)
        return rows[:limit]


def tautulli_plays(base, key, page=1000, opener=None):
    """Every play Tautulli recorded, as play log entries."""
    opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}))
    start, plays = 0, []
    while True:
        query = urllib.parse.urlencode({'apikey': key, 'cmd': 'get_history', 'length': page, 'start': start,
                                        'order_column': 'date', 'order_dir': 'desc'})
        with opener.open(f'{base.rstrip("/")}/api/v2?{query}', timeout=60) as response:
            reply = json.load(response).get('response') or {}
        if reply.get('result') != 'success':
            raise ValueError(reply.get('message') or 'Tautulli refused the request.')
        rows = (reply.get('data') or {}).get('data') or []
        for row in rows:
            play = tautulli_play(row)
            if play:
                plays.append(play)
        start += len(rows)
        if not rows or start >= ((reply.get('data') or {}).get('recordsFiltered') or 0):
            return plays


def tautulli_play(row):
    """One row of Tautulli's history as a play log entry, or None for music and anything incomplete."""
    if not isinstance(row, dict) or row.get('media_type') not in ('movie', 'episode'):
        return None
    started, stopped = row.get('started'), row.get('stopped')
    rating = row.get('rating_key')
    if type(started) is not int or type(stopped) is not int or rating in (None, ''):
        return None
    if row['media_type'] == 'episode':
        def number(value):
            try:
                return int(value)
            except (TypeError, ValueError):
                return None
        season, episode = number(row.get('parent_media_index')), number(row.get('media_index'))
        code = f'S{season:02d}E{episode:02d}' if season is not None and episode is not None else ''
        title = short_text(row.get('grandparent_title'), 120) or 'Untitled'
        detail = ' · '.join(part for part in (code, short_text(row.get('title'), 80)) if part)
    else:
        title = short_text(row.get('title') or row.get('full_title'), 120) or 'Untitled'
        detail = str(row['year']) if str(row.get('year') or '').isdigit() else ''
    thumb = row.get('thumb')
    poster = f'/api/plex-art?{urllib.parse.urlencode({"path": thumb})}' if isinstance(thumb, str) and PLEX_ART.fullmatch(thumb) else None
    percent = row.get('percent_complete')
    finished = row.get('watched_status') == 1
    return {'account': str(row.get('user_id') or ''), 'ratingKey': str(rating), 'started': started, 'seen': stopped,
            'progress': 100 if finished else max(0, min(100, int(percent))) if isinstance(percent, (int, float)) else 0,
            'finished': finished, 'imported': True, 'title': title, 'detail': detail, 'poster': poster,
            'user': short_text(row.get('friendly_name') or row.get('user'), 60) or 'Someone'}


def import_tautulli(argv):
    """import-tautulli <address>: add Tautulli's history to the play log, once. Asks for Tautulli's API key."""
    import getpass
    if len(argv) != 1 or not re.fullmatch(r'https?://[A-Za-z0-9.\[\]:-]+(:[0-9]{1,5})?/?', argv[0]):
        raise SystemExit('Usage: import-tautulli http://<address>:8181')
    if PLAYS.path is None:
        raise SystemExit('Run this in the dashboard container: it has no play log here.')
    key = os.environ.get('TAUTULLI_KEY') or getpass.getpass('Tautulli API key (Settings → Web Interface → API key): ').strip()
    if not re.fullmatch(r'[A-Za-z0-9]{16,64}', key):
        raise SystemExit('That doesn’t look like a Tautulli API key.')
    try:
        plays = tautulli_plays(argv[0], key)
    except (OSError, ValueError) as exc:
        raise SystemExit(f'Tautulli could not be read: {exc}') from None
    added = PLAYS.add(plays)
    print(f'Tautulli had {len(plays)} plays of films and episodes; {added} were new to the play log.')


PLAYS = PlayLog(Path(os.environ['MARTINFLIX_DATA_DIR']) / 'plays.json' if os.environ.get('MARTINFLIX_DATA_DIR') else None)


def log_plays_forever(sleep=time.sleep):
    while True:
        try:
            if plex_configured():
                PLAYS.record(plex_sessions())
        except (OSError, ValueError, AttributeError, TypeError):
            pass
        sleep(PLAY_LOG_EVERY)


def live_playing():
    if not plex_configured():
        return {'configured': False}
    try:
        sessions = plex_sessions()
    except (OSError, ValueError, AttributeError):
        return {'configured': True, 'error': 'Plex is not answering right now.'}
    return {'configured': True, 'sessions': sessions}


def plex_activity():
    if plex_configured():
        playing = live_playing()
        if playing.get('error'):
            return {'configured': True, 'error': 'Plex activity is unavailable.'}
        return {'configured': True, 'streams': len(playing['sessions']),
                'sessions': [{'title': ' · '.join(filter(None, (item['title'], item['detail']))), 'state': item['state']}
                             for item in playing['sessions']]}
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
                result.append({'id': path.parent.name, 'created': data.get('created'), 'files': len(data.get('files', [])),
                               'missing': data.get('missing', []), 'errors': data.get('errors', []),
                               'apps': sorted((data.get('apps') or {}).keys()), 'kind': data.get('kind'),
                               'size': data.get('size') if type(data.get('size')) is int else None})
            except (OSError, ValueError):
                result.append({'id': path.parent.name, 'errors': ['Manifest unreadable']})
        schedule, problem = None, None
        try:
            control = controller_request('/status', timeout=10)
            if control is None:
                problem = 'Scheduled backups need the controller, which this installation doesn’t have.'
            else:
                schedule = control.get('backups') if isinstance(control, dict) else None
                if schedule is None:
                    problem = 'The controller is older than backups. Update MartinFlix to add them.'
        except ControllerError as exc:
            problem = f'The controller didn’t answer: {exc}'
        return {'directory': str(directory), 'snapshots': result, 'schedule': schedule, 'scheduleProblem': problem}
    except OSError:
        return {'directory': str(directory), 'snapshots': [], 'error': 'Cannot read backup directory.'}


PROFILE_NAME = 'MartinFlix HD (720p/1080p)'
POSTER_HOSTS = ('image.tmdb.org', 'artworks.thetvdb.com')
IMAGE_SOURCES = ' '.join('https://' + host for host in POSTER_HOSTS)


class AddError(ValueError):
    """A problem the admin can act on, shown on the Add page."""


def remote_poster(value):
    """Lookup results point at TMDB or TheTVDB artwork; anything else is dropped rather than loaded."""
    if not isinstance(value, str):
        return None
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme != 'https' or parsed.hostname not in POSTER_HOSTS or parsed.username or parsed.port:
        return None
    path = parsed.path.replace('/t/p/original/', '/t/p/w342/', 1)
    return urllib.parse.urlunsplit(('https', parsed.hostname, path, '', ''))


def short_text(value, limit=320):
    if not isinstance(value, str):
        return ''
    value = ' '.join(value.split())
    return value if len(value) <= limit else value[:limit].rsplit(' ', 1)[0] + '…'


def arr_json(connection, endpoint, payload=None, method=None, timeout=5):
    with arr_open(connection, endpoint, payload=payload, method=method, timeout=timeout) as response:
        body = response.read()
    return json.loads(body) if body else None


SERIES_MONITOR = ('all', 'future', 'latest', 'first', 'custom')
FILM_AVAILABILITY = ('announced', 'inCinemas', 'released')


def add_choices(kind):
    """The quality profiles and root folders a new title can use, with the MartinFlix defaults marked."""
    connection = arr_connection(kind)
    if connection is None:
        raise AddError(f'Connect {kind.capitalize()} to the dashboard first.')
    profiles = [item for item in arr_json(connection, '/api/v3/qualityprofile') or []
                if isinstance(item, dict) and type(item.get('id')) is int and isinstance(item.get('name'), str)]
    folders = [item for item in arr_json(connection, '/api/v3/rootfolder') or []
               if isinstance(item, dict) and isinstance(item.get('path'), str)]
    if not profiles:
        raise AddError(f'{kind.capitalize()} has no quality profiles.')
    if not folders:
        raise AddError(f'{kind.capitalize()} has no root folder yet. Rerun setup with --media-path, '
                       f'or add a root folder in {kind.capitalize()}.')
    profile = next((item for item in profiles if item['name'] == PROFILE_NAME), profiles[0])
    folder = next((item for item in folders if item.get('accessible', True)), folders[0])
    return connection, profiles, folders, profile, folder


def add_settings(kind):
    try:
        _, profiles, folders, profile, folder = add_choices(kind)
    except AddError as exc:
        return {'ready': False, 'problem': str(exc)}
    except (OSError, ValueError, TypeError, KeyError):
        return {'ready': False, 'problem': f'Could not reach {kind.capitalize()}.'}
    return {
        'ready': True,
        'profiles': [{'id': item['id'], 'name': item['name']} for item in profiles],
        'folders': [{'path': item['path'], 'freeSpace': item.get('freeSpace') if type(item.get('freeSpace')) is int else None,
                     'accessible': item.get('accessible', True) is not False} for item in folders],
        'profile': profile['id'], 'folder': folder['path'],
    }


def season_numbers(item):
    return sorted({season['seasonNumber'] for season in item.get('seasons') or []
                   if isinstance(season, dict) and type(season.get('seasonNumber')) is int and season['seasonNumber'] >= 0})


# Discover: trending and upcoming titles from TMDB, whether or not they are in the library.
TMDB_API = 'https://api.themoviedb.org/3'
TMDB_CACHE_SECONDS = 3600
TMDB_CACHE = {}
TMDB_LOCK = threading.Lock()
REGION_NAMES = {'GB': 'UK', 'US': 'US', 'IE': 'Irish', 'CA': 'Canadian', 'AU': 'Australian', 'NZ': 'New Zealand',
                'DE': 'German', 'FR': 'French', 'ES': 'Spanish', 'IT': 'Italian', 'NL': 'Dutch', 'PT': 'Portuguese',
                'BR': 'Brazilian', 'SE': 'Swedish', 'NO': 'Norwegian', 'DK': 'Danish'}


# MartinFlix's own TMDB API key, registered for this project, so Discover works without setup. Anyone can use
# their own key instead (setup's --set-tmdb-key), or turn Discover off (--discover off).
DEFAULT_TMDB_KEY = 'db55323b8d3e4154498498a75642b381'


def tmdb_key():
    if os.environ.get('MARTINFLIX_DISCOVER', 'on') == 'off':
        return ''
    return read_secret('MARTINFLIX_TMDB_KEY') or DEFAULT_TMDB_KEY


def discover_region():
    region = os.environ.get('MARTINFLIX_DISCOVER_REGION', 'GB').strip().upper()
    return region if re.fullmatch(r'[A-Z]{2}', region) else 'GB'


def tmdb_get(path, **params):
    """One TMDB API call. Accepts a v3 API key or a v4 read access token, whichever setup was given."""
    key = tmdb_key()
    if not key:
        raise AddError('Discover is turned off. Turn it on in Settings.')
    headers = {'Accept': 'application/json'}
    if key.startswith('eyJ'):
        headers['Authorization'] = f'Bearer {key}'
    else:
        params['api_key'] = key
    request = urllib.request.Request(f'{TMDB_API}{path}?{urllib.parse.urlencode(params)}', headers=headers)
    with ARR_OPENER.open(request, timeout=10) as response:
        return json.load(response)


def tmdb_cached(path, **params):
    cache_key = (path, tuple(sorted(params.items())))
    with TMDB_LOCK:
        hit = TMDB_CACHE.get(cache_key)
        if hit and time.time() - hit[0] < TMDB_CACHE_SECONDS:
            return hit[1]
    data = tmdb_get(path, **params)
    with TMDB_LOCK:
        TMDB_CACHE[cache_key] = (time.time(), data)
    return data


def tmdb_item(item, kind):
    title = item.get('title') if kind == 'radarr' else item.get('name')
    date = item.get('release_date') if kind == 'radarr' else item.get('first_air_date')
    identifier = item.get('id')
    if type(identifier) is not int or identifier < 1 or not isinstance(title, str) or not title.strip():
        return None
    date = date if isinstance(date, str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}', date) else None
    poster = item.get('poster_path')
    return {
        'id': identifier, 'kind': kind, 'title': title.strip(), 'date': date, 'year': int(date[:4]) if date else None,
        'poster': f'https://image.tmdb.org/t/p/w342{poster}' if isinstance(poster, str) and re.fullmatch(r'/[\w.-]+', poster) else None,
        'overview': short_text(item.get('overview'), 200),
    }


def library_tmdb_ids(kind):
    """TMDB IDs already in Radarr or Sonarr, so Discover can mark them. Empty when the app can't be reached."""
    connection = arr_connection(kind)
    if connection is None:
        return set()
    try:
        items = arr_json(connection, '/api/v3/movie' if kind == 'radarr' else '/api/v3/series')
    except (OSError, ValueError):
        return set()
    return {item.get('tmdbId') for item in items or [] if isinstance(item, dict) and type(item.get('tmdbId')) is int}


def discover_rows():
    region = discover_region()
    place = REGION_NAMES.get(region, region)
    today = time.strftime('%Y-%m-%d')
    ahead = time.strftime('%Y-%m-%d', time.gmtime(time.time() + 90 * 86400))
    recent = time.strftime('%Y-%m-%d', time.gmtime(time.time() - 90 * 86400))
    language = 'en-GB' if region == 'GB' else 'en-US'
    rows = [
        ('trending-films', 'Trending films', 'This week, worldwide', 'radarr', '/trending/movie/week', {'language': language}),
        ('trending-series', 'Trending series', 'This week, worldwide', 'sonarr', '/trending/tv/week', {'language': language}),
        ('cinemas', f'Coming to {place} cinemas', 'Next three months, most anticipated first', 'radarr', '/discover/movie',
         {'region': region, 'with_release_type': '2|3', 'release_date.gte': today, 'release_date.lte': ahead,
          'sort_by': 'popularity.desc', 'language': language}),
        ('streaming-films', f'New films on {place} streaming', 'Released in the last three months', 'radarr', '/discover/movie',
         {'watch_region': region, 'with_watch_monetization_types': 'flatrate', 'primary_release_date.gte': recent,
          'primary_release_date.lte': today, 'sort_by': 'popularity.desc', 'language': language}),
        ('streaming-series', f'New series on {place} streaming', 'First aired in the last three months', 'sonarr',
         '/discover/tv', {'watch_region': region, 'with_watch_monetization_types': 'flatrate',
                          'first_air_date.gte': recent, 'first_air_date.lte': today, 'sort_by': 'popularity.desc',
                          'language': language}),
    ]
    owned = {kind: library_tmdb_ids(kind) for kind in ('radarr', 'sonarr')}
    result = []
    for key, title, detail, kind, path, params in rows:
        try:
            data = tmdb_cached(path, **params)
        except (OSError, ValueError):
            result.append({'key': key, 'title': title, 'detail': detail, 'error': 'TMDB did not answer. Try again later.', 'items': []})
            continue
        items = [entry for entry in (tmdb_item(item, kind) for item in (data or {}).get('results', [])
                                     if isinstance(item, dict)) if entry]
        for entry in items:
            entry['inLibrary'] = entry['id'] in owned[kind]
        result.append({'key': key, 'title': title, 'detail': detail, 'items': items[:20]})
    return {'region': region, 'rows': result}


def tvdb_for_tmdb(tmdb_id):
    """Sonarr adds series by TheTVDB ID, which TMDB lists for each show it knows on TheTVDB."""
    try:
        ids = tmdb_cached(f'/tv/{tmdb_id}/external_ids')
    except (OSError, ValueError) as exc:
        raise AddError('TMDB did not answer. Try again later.') from exc
    tvdb = (ids or {}).get('tvdb_id')
    if type(tvdb) is not int or tvdb < 1:
        raise AddError('This series is not on TheTVDB yet, so Sonarr cannot add it.')
    return tvdb


def search_titles(kind, term):
    connection = arr_connection(kind)
    if connection is None:
        raise AddError(f'Connect {kind.capitalize()} to the dashboard first.')
    if kind == 'sonarr' and (match := re.fullmatch(r'tmdb:([1-9][0-9]{0,9})', term)):
        term = f'tvdb:{tvdb_for_tmdb(int(match.group(1)))}'
    route = '/api/v3/series/lookup?' if kind == 'sonarr' else '/api/v3/movie/lookup?'
    results = arr_json(connection, route + urllib.parse.urlencode({'term': term}))
    found = []
    for item in results if isinstance(results, list) else []:
        identifier = item.get('tvdbId' if kind == 'sonarr' else 'tmdbId') if isinstance(item, dict) else None
        if type(identifier) is not int or identifier < 1:
            continue
        entry = {
            'id': identifier,
            'title': item.get('title') or 'Untitled',
            'year': item.get('year') if type(item.get('year')) is int and item.get('year') > 0 else None,
            'overview': short_text(item.get('overview')),
            'poster': remote_poster(item.get('remotePoster')),
            'added': bool(item.get('id')) or bool(item.get('isExisting')),
            'status': item.get('status') if isinstance(item.get('status'), str) else '',
        }
        if kind == 'sonarr':
            entry['network'] = item.get('network') if isinstance(item.get('network'), str) else ''
            entry['seasonNumbers'] = season_numbers(item)
            entry['seasons'] = item.get('seasonCount') or len([number for number in entry['seasonNumbers'] if number])
        else:
            entry['studio'] = item.get('studio') if isinstance(item.get('studio'), str) else ''
            entry['runtime'] = item.get('runtime') if type(item.get('runtime')) is int else None
        found.append(entry)
        if len(found) == 24:
            break
    return found


def add_title(kind, request):
    """Add one title with the choices from the Add page. Every choice is checked against what the app offers."""
    identifier = request.get('id')
    if type(identifier) is not int or not 0 < identifier <= 2147483647:
        raise AddError('Choose a title from the search results.')
    connection, profiles, folders, default_profile, default_folder = add_choices(kind)
    profile_id = request.get('profile', default_profile['id'])
    if not any(item['id'] == profile_id for item in profiles):
        raise AddError('That quality profile no longer exists. Reload the page and choose again.')
    folder_path = request.get('folder', default_folder['path'])
    if not any(item['path'] == folder_path for item in folders):
        raise AddError('That folder is no longer a root folder. Reload the page and choose again.')
    search = request.get('search', True)
    if type(search) is not bool:
        raise AddError('Invalid search choice.')

    if kind == 'sonarr':
        matches = arr_json(connection, '/api/v3/series/lookup?' + urllib.parse.urlencode({'term': f'tvdb:{identifier}'}))
        item = next((match for match in matches or [] if match.get('tvdbId') == identifier), None)
    else:
        item = arr_json(connection, '/api/v3/movie/lookup/tmdb?' + urllib.parse.urlencode({'tmdbId': identifier}))
        if isinstance(item, list):
            item = item[0] if item else None
    if not isinstance(item, dict):
        raise AddError('That title could not be found any more. Search again.')
    if item.get('id') or item.get('isExisting'):
        raise AddError(f'{item.get("title", "This title")} is already in {kind.capitalize()}.')
    item.pop('id', None)
    item.update(qualityProfileId=profile_id, rootFolderPath=folder_path, monitored=True)

    if kind == 'sonarr':
        monitor = request.get('monitor', 'all')
        if monitor not in SERIES_MONITOR:
            raise AddError('Choose which seasons to monitor.')
        numbers = season_numbers(item)
        regular = [number for number in numbers if number > 0]
        if monitor == 'latest':
            chosen = set(regular[-1:])
        elif monitor == 'first':
            chosen = set(regular[:1])
        elif monitor == 'custom':
            chosen = request.get('seasons')
            if not isinstance(chosen, list) or not chosen or not all(type(number) is int for number in chosen) \
                    or not set(chosen) <= set(numbers):
                raise AddError('Choose at least one season.')
            chosen = set(chosen)
        options = {'searchForMissingEpisodes': search, 'searchForCutoffUnmetEpisodes': False}
        if monitor in ('all', 'future'):
            # Sonarr's own monitor modes; "future" follows only episodes that have not aired yet.
            options['monitor'] = monitor
        else:
            # Monitor exactly the chosen seasons. Specials stay unmonitored unless chosen.
            item['seasons'] = [dict(season, monitored=season.get('seasonNumber') in chosen)
                               for season in item.get('seasons') or [] if isinstance(season, dict)]
        item.update(seasonFolder=True, addOptions=options)
    else:
        availability = request.get('availability', 'released')
        if availability not in FILM_AVAILABILITY:
            raise AddError('Choose when the film counts as available.')
        item.update(minimumAvailability=availability, addOptions={'searchForMovie': search})
    collection, field = ('/api/v3/series', 'tvdbId') if kind == 'sonarr' else ('/api/v3/movie', 'tmdbId')
    try:
        # Adding fetches the title's details and artwork first, which can take Sonarr or Radarr a while.
        added = arr_json(connection, collection, payload=item, method='POST', timeout=60)
    except (TimeoutError, urllib.error.URLError) as exc:
        if isinstance(exc, urllib.error.HTTPError):
            raise
        # A slow answer can still mean it was added; the app's own list says for certain.
        found = arr_json(connection, f'{collection}?{urllib.parse.urlencode({field: identifier})}', timeout=15)
        if not any(isinstance(entry, dict) and entry.get(field) == identifier for entry in found or []):
            raise
        added = None
    return {'added': True, 'title': (added or item).get('title') or item.get('title'), 'searching': search}


def delete_title(kind, request):
    """Remove one series or film from Sonarr or Radarr, and with `files`, its folder and everything in it."""
    identifier, files = request.get('id'), request.get('files', False)
    if type(identifier) is not int or not 0 < identifier <= 2147483647:
        raise AddError('Choose a title to delete.')
    if type(files) is not bool:
        raise AddError('Invalid choice for the files.')
    connection = arr_connection(kind)
    if connection is None:
        raise AddError(f'Connect {kind.capitalize()} to the dashboard first.')
    collection = '/api/v3/series' if kind == 'sonarr' else '/api/v3/movie'
    try:
        item = arr_json(connection, f'{collection}/{identifier}', timeout=15)
    except urllib.error.HTTPError as exc:
        exc.close()
        if exc.code == 404:
            raise AddError(f'That title is no longer in {kind.capitalize()}.') from None
        raise
    if not isinstance(item, dict) or item.get('id') != identifier:
        raise AddError(f'That title is no longer in {kind.capitalize()}.')
    # Sonarr and Radarr name the exclusion option differently; neither adds one, so the title can be added again.
    exclusion = 'addImportListExclusion' if kind == 'sonarr' else 'addImportExclusion'
    query = urllib.parse.urlencode({'deleteFiles': str(files).lower(), exclusion: 'false'})
    # Deleting files can take a while on a network share.
    arr_json(connection, f'{collection}/{identifier}?{query}', method='DELETE', timeout=120)
    return {'deleted': True, 'title': item.get('title') or 'The title', 'files': files}


# Telegram: Sonarr and Radarr tell the dashboard what happened through a webhook named MartinFlix, and the dashboard
# posts one message in one format for both, with the poster.
TELEGRAM_TOKEN = re.compile(r'[0-9]{5,12}:[A-Za-z0-9_-]{30,64}')
TELEGRAM_CHAT = re.compile(r'-?[0-9]{1,20}|@[A-Za-z][A-Za-z0-9_]{4,31}')
HOOK_USER = 'martinflix'
# Episodes imported together (a season pack, or a few at once) arrive one event each; they become one message.
HOOK_GATHER_SECONDS = 45
INTERNET = urllib.request.build_opener(NoRedirect())


class TelegramError(Exception):
    pass


def telegram_settings():
    """(token, chat) when Telegram is set up, else None."""
    token, chat = read_secret('MARTINFLIX_TELEGRAM_TOKEN'), read_secret('MARTINFLIX_TELEGRAM_CHAT')
    if TELEGRAM_TOKEN.fullmatch(token) and TELEGRAM_CHAT.fullmatch(chat):
        return token, chat
    return None


def telegram_call(token, method, payload, timeout=20):
    request = urllib.request.Request(f'https://api.telegram.org/bot{token}/{method}', data=json.dumps(payload).encode(),
                                     headers={'Content-Type': 'application/json'}, method='POST')
    try:
        with INTERNET.open(request, timeout=timeout) as response:
            reply = json.load(response)
    except urllib.error.HTTPError as exc:
        try:
            reply = json.load(exc)
        except ValueError:
            reply = {}
        finally:
            exc.close()
        description = reply.get('description') if isinstance(reply, dict) else None
        raise TelegramError(telegram_problem(exc.code, description)) from None
    except (OSError, ValueError):
        raise TelegramError('Telegram could not be reached. Check this machine’s internet connection.') from None
    if not isinstance(reply, dict) or not reply.get('ok'):
        raise TelegramError(telegram_problem(None, reply.get('description') if isinstance(reply, dict) else None))
    return reply.get('result')


def telegram_problem(code, description):
    text = (description or '').lower()
    if code == 401 or 'unauthorized' in text:
        return 'Telegram doesn’t recognise that bot token. Copy it again from @BotFather.'
    if 'chat not found' in text:
        return 'Telegram can’t find that chat. Send your bot a message first, or check the chat ID.'
    if 'bot was blocked' in text or 'kicked' in text:
        return 'The bot can’t post in that chat: it was blocked or removed. Add it back, then try again.'
    if code == 409 or 'webhook' in text:
        return 'This bot is set up to send its updates elsewhere, so its chats can’t be listed. Enter the chat ID yourself.'
    return f'Telegram refused the message{": " + description if description else "."}'


def telegram_send(text, photo=None, settings=None):
    settings = settings or telegram_settings()
    if settings is None:
        return False
    token, chat = settings
    if photo:
        try:
            telegram_call(token, 'sendPhoto', {'chat_id': chat, 'photo': photo, 'caption': text[:1024], 'parse_mode': 'HTML'})
            return True
        except TelegramError:
            pass  # Telegram couldn't fetch the poster: the words alone still go.
    telegram_call(token, 'sendMessage', {'chat_id': chat, 'text': text[:4096], 'parse_mode': 'HTML',
                                         'link_preview_options': {'is_disabled': True}})
    return True


def telegram_chats(token):
    """Chats that have messaged the bot lately, so the chat ID needn't be looked up by hand."""
    updates = telegram_call(token, 'getUpdates', {'limit': 100, 'allowed_updates': ['message', 'channel_post', 'my_chat_member']})
    chats = {}
    for update in updates or []:
        for key in ('message', 'channel_post', 'my_chat_member'):
            chat = (update.get(key) or {}).get('chat') if isinstance(update, dict) else None
            if isinstance(chat, dict) and type(chat.get('id')) is int:
                name = chat.get('title') or ' '.join(filter(None, (chat.get('first_name'), chat.get('last_name')))) \
                    or chat.get('username') or str(chat['id'])
                chats[chat['id']] = {'id': str(chat['id']), 'name': short_text(name, 80), 'type': chat.get('type') or ''}
    return list(chats.values())


def hook_poster(item):
    for image in (item or {}).get('images') or []:
        url = image.get('remoteUrl') if isinstance(image, dict) else None
        if image.get('coverType') == 'poster' and isinstance(url, str) and url.startswith('https://') and len(url) < 500:
            return url
    return None


def hook_heading(item):
    title = short_text((item or {}).get('title'), 120) or 'Untitled'
    year = (item or {}).get('year')
    return f'<b>{html.escape(title)}</b>' + (f' ({year})' if type(year) is int and year > 0 else '')


def episode_codes(episodes):
    """"S04E06", "S04E01–E06" or "S03E10, S04E01": each season's episodes, runs joined."""
    seasons = {}
    for season, number in sorted(episodes):
        seasons.setdefault(season, []).append(number)
    parts = []
    for season, numbers in seasons.items():
        start = previous = numbers[0]
        for number in numbers[1:] + [None]:
            if number is not None and number == previous + 1:
                previous = number
                continue
            parts.append(f'S{season:02d}E{start:02d}' + (f'–E{previous:02d}' if previous != start else ''))
            if number is not None:
                start = previous = number
    return ', '.join(parts)


class HookMessages:
    """Turns Sonarr's and Radarr's webhook events into Telegram messages. Episodes imported within a short while of
    each other, for the same series, are gathered into one message."""

    def __init__(self, send=None, gather=HOOK_GATHER_SECONDS, timer=threading.Timer):
        self.send = send or (lambda text, photo: telegram_send(text, photo))
        self.gather, self.timer = gather, timer
        self.pending = {}
        self.lock = threading.Lock()

    def handle(self, app, event):
        kind = event.get('eventType') if isinstance(event, dict) else None
        # Sonarr and Radarr send a Test event when the webhook is added or saved: not worth a message each time.
        # The dashboard's own Send a test message covers checking the chat.
        if kind == 'SeriesAdd' and app == 'sonarr':
            series = event.get('series') or {}
            self.deliver(f'{hook_heading(series)}\nNow monitored: episodes will download as they become available', hook_poster(series))
        elif kind == 'MovieAdded' and app == 'radarr':
            movie = event.get('movie') or {}
            self.deliver(f'{hook_heading(movie)}\nNow monitored: it will download once it’s available', hook_poster(movie))
        elif kind == 'Download' and app == 'radarr':
            movie = event.get('movie') or {}
            state = 'Upgraded' if event.get('isUpgrade') else 'Ready to watch'
            self.deliver(f'{hook_heading(movie)}\n{state}', hook_poster(movie))
        elif kind == 'Download' and app == 'sonarr':
            self.gather_episodes(event)

    def gather_episodes(self, event):
        series = event.get('series') or {}
        key = series.get('id') or series.get('tvdbId') or series.get('title')
        episodes = {(item['seasonNumber'], item['episodeNumber']): item.get('title') for item in event.get('episodes') or []
                    if isinstance(item, dict) and type(item.get('seasonNumber')) is int and type(item.get('episodeNumber')) is int}
        with self.lock:
            batch = self.pending.get(key)
            if batch is None:
                batch = self.pending[key] = {'series': series, 'episodes': {}, 'upgrade': True, 'timer': None}
            batch['episodes'].update(episodes)
            batch['upgrade'] = batch['upgrade'] and bool(event.get('isUpgrade'))
            if batch['timer'] is not None:
                batch['timer'].cancel()
            batch['timer'] = self.timer(self.gather, self.flush, args=(key,))
            batch['timer'].daemon = True
            batch['timer'].start()

    def flush(self, key):
        with self.lock:
            batch = self.pending.pop(key, None)
        if batch is None:
            return
        episodes = batch['episodes']
        if len(episodes) == 1:
            (code, name), = episodes.items()
            line = episode_codes([code]) + (f' · {html.escape(short_text(name, 100))}' if name else '')
        else:
            line = f'{episode_codes(episodes)} · {len(episodes)} episodes' if episodes else ''
        state = 'Upgraded' if batch['upgrade'] else 'Ready to watch'
        self.deliver('\n'.join(filter(None, (hook_heading(batch['series']), line, state))), hook_poster(batch['series']))

    def deliver(self, text, photo):
        try:
            self.send(text, photo)
        except TelegramError as exc:
            print(f'Telegram: {exc}', flush=True)


HOOKS = HookMessages()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        path = url.path
        form_login = self.server.admin_state_file is not None
        if form_login and path in ('/style.css', '/auth.js'):
            self.send_web_file(path[1:], 'text/css' if path == '/style.css' else 'text/javascript')
            return
        if re.fullmatch(r'/fonts/[a-z0-9-]+\.woff2', path):
            self.send_asset(path[1:], 'font/woff2', cache='public, max-age=604800, immutable')
            return
        if path in ('/assets/martinflix-mark.webp', '/assets/martinflix-masthead.webp'):
            self.send_asset(path[1:], 'image/webp')
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
        if path == '/change-pin' and form_login and controller_configured():
            self.send_pin_page()
            return
        if path == '/api/status':
            catalog = {key: CATALOG.get(key, {}) for key in ('apps', 'branches', 'uses', 'groupRest')}
            data = {'docker': service_status(), 'backups': backup_status(), 'time': int(time.time()),
                    'canChangePassword': form_login, 'canChangePin': form_login and controller_configured(),
                    'discover': bool(tmdb_key()), 'release': release_brief(),
                    'catalog': catalog}
            self.send_json(data)
        elif path in ('/api/system', '/api/settings'):
            try:
                result = controller_request('/release' if path == '/api/system' else '/settings', timeout=15)
            except ControllerError as exc:
                self.send_json({'error': str(exc)}, 502)
                return
            if result is None:
                self.send_json({'configured': False})
                return
            if path == '/api/system' and result.get('available'):
                result['changes'] = release_changes(result['current'].get('revision'), result['available'].get('revision'))
            self.send_json({'configured': True, **result})
        elif path == '/api/discover':
            if not tmdb_key():
                self.send_json({'error': 'Discover is turned off. Turn it on in Settings.'}, 404)
            else:
                self.send_json(discover_rows())
        elif path == '/api/live':
            if urllib.parse.parse_qs(url.query).get('summary') == ['1']:
                transfer = live_transfer()
                transfer.pop('downloads', None)
                playing = live_playing()
                self.send_json({'transfer': transfer, 'playing': {
                    'configured': playing['configured'], 'count': len(playing.get('sessions') or []),
                    'error': bool(playing.get('error'))}})
            else:
                self.send_json({'transfer': live_transfer(), 'playing': live_playing()})
        elif path == '/api/watch-history':
            if not plex_configured():
                self.send_json({'configured': False})
            else:
                try:
                    history = plex_history(limit=50)
                except (OSError, ValueError, AttributeError):
                    history = None
                try:
                    # The play log still shows if Plex doesn't answer.
                    rows = PLAYS.merged(history or [])
                except (KeyError, TypeError, ValueError) as exc:
                    print(f'Play log: {exc!r}', flush=True)
                    self.send_json({'configured': True, 'error': 'The watch history could not be put together.'})
                else:
                    self.send_json({'configured': True, 'history': rows,
                                    **({'notice': 'Plex isn’t answering, so finished plays may show as partial.'} if history is None else {})})
        elif path == '/api/plex-art':
            art = plex_art((urllib.parse.parse_qs(url.query).get('path') or [''])[0])
            if art is None:
                self.send_error(404)
            else:
                body, kind = art
                self.send_response(200)
                self.send_header('Content-Type', kind)
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Cache-Control', 'private, max-age=86400')
                self.send_header('X-Content-Type-Options', 'nosniff')
                self.end_headers()
                self.wfile.write(body)
        elif path == '/api/activity':
            self.send_json(dashboard_activity())
        elif path in ('/api/sonarr', '/api/radarr'):
            everything = urllib.parse.parse_qs(url.query).get('show') == ['all']
            self.send_json(arr_data(path.rsplit('/', 1)[1], everything))
        elif search := re.fullmatch(r'/api/search/(sonarr|radarr)', path):
            term = ' '.join(urllib.parse.parse_qs(url.query).get('term', [''])[0].split())
            if not 1 <= len(term) <= 100:
                self.send_json({'error': 'Enter a title to search for.'}, 400)
                return
            try:
                self.send_json({'results': search_titles(search.group(1), term)})
            except AddError as exc:
                self.send_json({'error': str(exc)}, 409)
            except (OSError, ValueError, TypeError, AttributeError):
                self.send_json({'error': f'{search.group(1).capitalize()} could not search right now. Try again.'}, 502)
        elif settings := re.fullmatch(r'/api/add-settings/(sonarr|radarr)', path):
            self.send_json(add_settings(settings.group(1)))
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
        elif path == '/' or path.strip('/') in PAGES:
            page = path.strip('/') or 'overview'
            body = (WEB / 'index.html').read_bytes().replace(b'data-page="overview"', f'data-page="{page}"'.encode(), 1)
            # Menu items that depend on settings are shown from the start, so they don't blink in once the page loads.
            for item, shown in ((b'nav-discover', bool(tmdb_key()) or page == 'discover'), (b'nav-password', form_login),
                                (b'nav-pin', form_login and controller_configured())):
                if shown:
                    body = body.replace(b'id="' + item + b'" hidden', b'id="' + item + b'"', 1)
            self.send_web_file('index.html', 'text/html', body=body)
        elif path in ('/app.js', '/style.css'):
            self.send_web_file(path[1:], 'text/javascript' if path == '/app.js' else 'text/css')
        elif re.fullmatch(r'/assets/[a-z0-9-]+\.webp', path):
            self.send_asset(path[1:], 'image/webp')
        else:
            self.send_error(404)

    def same_origin(self):
        try:
            origin = urllib.parse.urlsplit(self.headers.get('Origin', ''))
        except ValueError:
            return False
        return origin.scheme in ('http', 'https') and origin.netloc == self.headers.get('Host')

    def do_POST(self):
        path = urllib.parse.urlsplit(self.path).path
        if hook := re.fullmatch(r'/api/hook/(sonarr|radarr)', path):
            self.hook_request(hook.group(1))
            return
        if path in ('/api/telegram/test', '/api/telegram/chats'):
            self.telegram_request(path)
            return
        if add := re.fullmatch(r'/api/(add|delete)/(sonarr|radarr)', path):
            self.add_request(add.group(2), add.group(1))
            return
        if control := re.fullmatch(r'/api/control/(restart|update)', path):
            self.control_request(control.group(1))
            return
        if path == '/api/backup':
            self.backup_request()
            return
        if path == '/api/vpn-test':
            if self.json_body(64) is None:
                return
            try:
                check = controller_request('/vpn-test', {}, timeout=90)
            except ControllerError as exc:
                self.send_json({'error': str(exc)}, 502)
                return
            if check is None:
                self.send_json({'error': 'The VPN check needs the controller.'}, 503)
                return
            home = home_address() if check.get('installed') else None
            self.send_json({**check, 'home': home, 'verdict': vpn_verdict(check, home) if check.get('installed') else None,
                            'checkedAt': datetime.now(timezone.utc).isoformat(timespec='seconds')})
            return
        if path in ('/api/system/check', '/api/system/update', '/api/system/finish', '/api/settings', '/api/restore'):
            self.system_request(path)
            return
        if self.server.admin_state_file is None or path not in ('/login', '/setup-password', '/change-password', '/change-pin'):
            self.send_error(404)
            return
        if not self.same_origin():
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
        if path == '/change-pin':
            if self.server.password_setup_required:
                self.redirect('/setup-password')
            else:
                self.change_pin(fields)
            return
        changing = path == '/change-password'
        if changing == self.server.password_setup_required:
            self.redirect('/setup-password' if self.server.password_setup_required else '/')
            return
        self.change_password(fields, changing)

    def backup_request(self):
        if not self.same_origin():
            self.send_json({'error': 'Open the dashboard and try again.'}, 403)
            return
        if not self.authorized():
            self.require_login()
            return
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 256 or self.headers.get_content_type() != 'application/json':
                raise ValueError
            pin = json.loads(self.rfile.read(length))['pin']
        except (OSError, ValueError, TypeError, KeyError):
            self.send_json({'error': 'Invalid request.'}, 400)
            return
        if not isinstance(pin, str) or not re.fullmatch(r'[0-9]{4,8}', pin):
            self.send_json({'error': 'Enter your 4 to 8 digit PIN.'}, 400)
            return
        try:
            result = controller_request('/backup', {'pin': pin}, timeout=30)
        except ControllerError as exc:
            self.send_json({'error': str(exc)}, exc.status if exc.status in (400, 403, 409, 423, 503) else 502)
            return
        if result is None:
            self.send_json({'error': 'Backups need the controller. Rerun setup to add it.'}, 503)
            return
        self.send_json({'ok': True, 'message': 'Backup started. It takes a few minutes and appears here when done.'})

    def json_body(self, limit):
        """The JSON object of a dashboard action, or None once an error has been sent."""
        if not self.same_origin():
            self.send_json({'error': 'Open the dashboard and try again.'}, 403)
            return None
        if not self.authorized():
            self.require_login()
            return None
        if self.server.password_setup_required:
            self.send_json({'error': 'Set your dashboard password first.'}, 403)
            return None
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= limit or self.headers.get_content_type() != 'application/json':
                raise ValueError
            request = json.loads(self.rfile.read(length))
            if not isinstance(request, dict):
                raise ValueError
            return request
        except (OSError, ValueError):
            self.send_json({'error': 'Invalid request.'}, 400)
            return None

    def hook_request(self, app):
        """A webhook from Sonarr or Radarr. It signs in with the hook password setup gave both, not the dashboard's."""
        expected = read_secret('MARTINFLIX_HOOK_PASSWORD')
        try:
            scheme, encoded = self.headers.get('Authorization', '').split(' ', 1)
            user, password = base64.b64decode(encoded, validate=True).decode('utf-8').split(':', 1)
            valid = scheme.lower() == 'basic' and len(expected) >= 24 and hmac.compare_digest(user, HOOK_USER) \
                and hmac.compare_digest(password.encode(), expected.encode())
        except (ValueError, UnicodeDecodeError, binascii.Error):
            valid = False
        if not valid:
            self.send_json({'error': 'Not allowed.'}, 401)
            return
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 1024 * 1024:
                raise ValueError
            event = json.loads(self.rfile.read(length))
        except (OSError, ValueError):
            self.send_json({'error': 'Invalid request.'}, 400)
            return
        self.send_response(204)
        self.end_headers()
        if isinstance(event, dict) and telegram_settings() is not None:
            # Telegram can be slow; the app that sent this needn't wait for it.
            threading.Thread(target=HOOKS.handle, args=(app, event), daemon=True).start()

    def telegram_request(self, path):
        request = self.json_body(1024)
        if request is None:
            return
        try:
            if path == '/api/telegram/test':
                settings = telegram_settings()
                if settings is None:
                    self.send_json({'error': 'Save a bot token and chat ID first.'}, 409)
                    return
                telegram_send('<b>MartinFlix</b> is connected. Downloads and new series and films will be posted here.',
                              settings=settings)
                self.send_json({'ok': True})
                return
            token = request.get('token') or (telegram_settings() or ('', ''))[0] or read_secret('MARTINFLIX_TELEGRAM_TOKEN')
            if not isinstance(token, str) or not TELEGRAM_TOKEN.fullmatch(token):
                self.send_json({'error': 'Enter the bot token from @BotFather first.'}, 400)
                return
            self.send_json({'chats': telegram_chats(token)})
        except TelegramError as exc:
            self.send_json({'error': str(exc)}, 502)

    def system_request(self, path):
        """Check for, or install, a MartinFlix update, or apply changed settings, through the controller."""
        request = self.json_body(8192)
        if request is None:
            return
        payload = {}
        if path != '/api/system/check':
            pin = request.get('pin')
            if not isinstance(pin, str) or not re.fullmatch(r'[0-9]{4,8}', pin):
                self.send_json({'error': 'Enter your 4 to 8 digit PIN.'}, 400)
                return
            payload['pin'] = pin
        if path == '/api/settings':
            payload.update(settings=request.get('settings') or {}, secrets=request.get('secrets') or {})
        if path == '/api/restore':
            backup = request.get('backup')
            if not isinstance(backup, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}_\d{6}', backup):
                self.send_json({'error': 'Choose a backup to restore.'}, 400)
                return
            payload['backup'] = backup
        if path == '/api/system/finish':
            kind = request.get('kind')
            payload['kind'] = kind if kind in ('update', 'settings', 'restore', 'finish') else ''
        target = {'/api/system/check': '/release/check', '/api/system/update': '/release/update',
                  '/api/system/finish': '/release/finish', '/api/settings': '/settings', '/api/restore': '/restore'}[path]
        try:
            result = controller_request(target, payload, timeout=60)
        except ControllerError as exc:
            self.send_json({'error': str(exc)}, exc.status if exc.status in (400, 403, 404, 409, 423, 503) else 502)
            return
        if result is None:
            self.send_json({'error': 'Updates and settings need the controller. Run the update from a terminal.'}, 503)
            return
        self.send_json({'ok': True, **result})

    def control_request(self, action):
        if not self.same_origin():
            self.send_json({'error': 'Open the dashboard and try again.'}, 403)
            return
        if not self.authorized():
            self.require_login()
            return
        if self.server.password_setup_required:
            self.send_json({'error': 'Set your dashboard password first.'}, 403)
            return
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 1024 or self.headers.get_content_type() != 'application/json':
                raise ValueError
            request = json.loads(self.rfile.read(length))
            name, pin = request['service'], request['pin']
        except (OSError, ValueError, TypeError, KeyError):
            self.send_json({'error': 'Invalid request.'}, 400)
            return
        if not isinstance(name, str) or name not in CATALOG['apps']:
            self.send_json({'error': 'Unknown app.'}, 400)
            return
        if not isinstance(pin, str) or not re.fullmatch(r'[0-9]{4,8}', pin):
            self.send_json({'error': 'Enter your 4 to 8 digit PIN.'}, 400)
            return
        try:
            # An update downloads an image and recreates containers, which can take several minutes.
            result = controller_request(f'/{action}', {'service': CONTROL_NAMES.get(name, name), 'pin': pin}, timeout=900)
        except ControllerError as exc:
            self.send_json({'error': str(exc)}, exc.status if exc.status in (400, 403, 404, 409, 423, 503) else 502)
            return
        if result is None:
            self.send_json({'error': 'Restarts and updates need the controller. Rerun setup to add it.'}, 503)
            return
        self.send_json({'ok': True, 'message': control_message(result)})

    def add_request(self, kind, action='add'):
        if not self.same_origin():
            self.send_json({'error': 'Open the dashboard and try again.'}, 403)
            return
        if not self.authorized():
            self.require_login()
            return
        if self.server.password_setup_required:
            self.send_json({'error': 'Set your dashboard password first.'}, 403)
            return
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 4096 or self.headers.get_content_type() != 'application/json':
                raise ValueError
            request = json.loads(self.rfile.read(length))
            if not isinstance(request, dict):
                raise ValueError
        except (OSError, ValueError):
            self.send_json({'error': 'Invalid request.'}, 400)
            return
        if action == 'delete':
            try:
                self.send_json(delete_title(kind, request))
            except AddError as exc:
                self.send_json({'error': str(exc)}, 409)
            except urllib.error.HTTPError as exc:
                exc.close()
                self.send_json({'error': f'{kind.capitalize()} refused to delete this title. Check its logs for the reason.'}, 502)
            except (OSError, ValueError, TypeError, KeyError, AttributeError):
                self.send_json({'error': f'Could not reach {kind.capitalize()}. Check whether the title is still there, then try again.'}, 502)
            return
        try:
            if 'id' not in request and type(request.get('tmdb')) is int and 0 < request['tmdb'] <= 2147483647:
                # From Discover: TMDB's ID, and the usual defaults. Sonarr adds series by TheTVDB's ID.
                request = {'id': tvdb_for_tmdb(request['tmdb']) if kind == 'sonarr' else request['tmdb']}
            self.send_json(add_title(kind, request))
        except AddError as exc:
            self.send_json({'error': str(exc)}, 409)
        except urllib.error.HTTPError as exc:
            exc.close()
            self.send_json({'error': f'{kind.capitalize()} refused to add this title. Check its logs for the reason.'}, 502)
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            self.send_json({'error': f'Could not reach {kind.capitalize()}. Try again.'}, 502)

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
                if synced_apps():
                    try:
                        sync_app_passwords(password)
                    except (OSError, ValueError) as exc:
                        if isinstance(exc, urllib.error.HTTPError):
                            exc.close()
                        self.send_auth_page('setup-password.html',
                                            f'Could not finish updating {app_list()}. Check they are running and submit '
                                            'the same new password again. The dashboard still uses its current password.',
                                            502, changing=changing)
                        return
                save_admin_password(self.server.admin_state_file, verifier.encoded)
            except OSError:
                self.send_auth_page('setup-password.html', 'Could not save the dashboard password. Check its storage '
                                    'and submit the same new password again.', 500, changing=changing)
                return
            self.server.password_verifier = verifier
            self.server.admin_password = ''
            self.server.password_setup_required = False
            self.server.session_key = new_session_key(self.server.admin_state_file)
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

    def change_pin(self, fields):
        token = fields.get('token', [''])[0]
        if not hmac.compare_digest(token.encode(), self.server.password_setup_token.encode()):
            self.send_error(403, 'Reopen the Change PIN page and try again.')
            return
        current, new, confirmation = (fields.get(name, [''])[0] for name in ('current', 'pin', 'confirm'))
        if not re.fullmatch(r'[0-9]{4,8}', current):
            self.send_pin_page('Enter your current PIN: 4 to 8 digits.', 400)
            return
        if not re.fullmatch(r'[0-9]{4,8}', new):
            self.send_pin_page('Use 4 to 8 digits for the new PIN.', 400)
            return
        if new != confirmation:
            self.send_pin_page('The new PINs don’t match. Enter the same PIN twice.', 400)
            return
        try:
            # The controller checks the current PIN itself, and wrong guesses count towards its lockout.
            if controller_request('/pin', {'pin': current, 'new': new}, timeout=30) is None:
                self.send_error(404)
                return
        except ControllerError as exc:
            self.send_pin_page(str(exc), exc.status if exc.status in (400, 403, 423) else 502)
            return
        self.send_pin_page(saved=True)

    def send_pin_page(self, message='', status=200, saved=False):
        error = f'<p class="setup-error" role="alert">{html.escape(message)}</p>' if message else ''
        notice = ('<p class="setup-success" role="status">PIN changed. Use the new PIN for restarts and updates.</p>'
                  if saved else '')
        body = ((WEB / 'change-pin.html').read_text()
                .replace('{{TOKEN}}', html.escape(self.server.password_setup_token))
                .replace('{{NOTICE}}', notice).replace('{{ERROR}}', error).encode())
        self.send_web_file('change-pin.html', 'text/html', status, body)

    def send_auth_page(self, name, message='', status=200, password_set=False, changing=False):
        template = (WEB / name).read_text()
        error = f'<p class="setup-error" role="alert">{html.escape(message)}</p>' if message else ''
        notice = '<p class="setup-success" role="status">Password saved. Sign in with your new password.</p>' if password_set else ''
        current = ('<label for="current">Current password</label><div class="password-field">'
                   '<input type="password" id="current" name="current" autocomplete="current-password" required>'
                   '<button type="button" class="password-toggle" aria-controls="current" aria-label="Show current password" '
                   'aria-pressed="false" hidden><span class="password-eye" aria-hidden="true"></span></button></div>')
        apps = app_list() if synced_apps() else ''
        scope = f'<p class="setup-note">This password also signs you in to {html.escape(apps)}.</p>' if apps else ''
        # Saving waits for every app to take the new password, so say what is happening while it runs.
        progress = 'Updating every app with your new password. This can take up to a minute.' if apps else 'Saving your password…'
        if changing:
            scope += '<a href="/">Back to dashboard</a>'
        body = (template.replace('{{ACTION}}', '/change-password' if changing else '/setup-password')
                .replace('{{HEADING}}', 'Change your dashboard password' if changing else 'Set your dashboard password')
                .replace('{{CURRENT}}', current if changing else '')
                .replace('{{PASSWORD_SCOPE}}', scope).replace('{{PROGRESS}}', progress)
                .replace('{{TOKEN}}', html.escape(self.server.password_setup_token))
                .replace('{{MIN_PASSWORD}}', str(MIN_PERSONAL_PASSWORD))
                .replace('{{NOTICE}}', notice).replace('{{ERROR}}', error).encode())
        self.send_web_file(name, 'text/html', status, body)

    def send_asset(self, name, content_type, cache='private, max-age=3600'):
        try:
            body = (WEB / name).read_bytes()
        except FileNotFoundError:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', cache)
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        self.wfile.write(body)

    def send_web_file(self, name, content_type, status=200, body=None):
        if body is None:
            body = (WEB / name).read_bytes()
        self.send_response(status)
        self.send_header('Content-Type', content_type + '; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', f"default-src 'self'; img-src 'self' {IMAGE_SOURCES}; "
                         "base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
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


    def send_json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
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


def new_session_key(state_file):
    """A new key for signing sign-in cookies, which signs everyone out. Kept beside the password hash, so sessions
    survive the dashboard restarting, as it does for an update."""
    key = secrets.token_bytes(32)
    if state_file is not None:
        path = state_file.with_name('session-key')
        temporary = path.with_name(f'.session-key.{secrets.token_hex(4)}')
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, 'w') as output:
                output.write(key.hex())
            os.replace(temporary, path)
        except OSError:
            temporary.unlink(missing_ok=True)
    return key


def load_session_key(state_file):
    if state_file is not None:
        try:
            key = bytes.fromhex(state_file.with_name('session-key').read_text().strip())
            if len(key) == 32:
                return key
        except (OSError, ValueError):
            pass
    return new_session_key(state_file)


def save_admin_password(path, encoded):
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, delete=False) as output:
        temporary = Path(output.name)
        output.write(encoded + '\n')
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


if __name__ == '__main__' and sys.argv[1:2] == ['import-tautulli']:
    import_tautulli(sys.argv[2:])
    sys.exit(0)


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
    server.session_key = load_session_key(state_file)
    threading.Thread(target=sample_speeds_forever, daemon=True, name='speed-samples').start()
    if PLAYS.path is not None:
        threading.Thread(target=log_plays_forever, daemon=True, name='play-log').start()
    print('MartinFlix listening on %s:%d' % (host, port), flush=True)
    server.serve_forever()
