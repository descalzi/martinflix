#!/usr/bin/env python3
"""Local MartinFlix dashboard and installer-managed password setup."""
import json
import base64
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
PAGES = ('overview', 'library', 'add', 'services', 'backups')


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


def arr_open(connection, endpoint, *, payload=None, method=None):
    base, key = connection
    headers = {'X-Api-Key': key}
    body = None
    if payload is not None:
        headers['Content-Type'] = 'application/json'
        body = json.dumps(payload).encode()
    request = urllib.request.Request(base + endpoint, data=body, headers=headers, method=method)
    return ARR_OPENER.open(request, timeout=5)


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
    for kind in (app for app in apps if app != 'transmission'):
        connection = arr_connection(kind, prowlarr_via_vpn=behind_vpn)
        if connection is None:
            raise ValueError('Missing application API key')
        version = 'v1' if kind == 'prowlarr' else 'v3'
        with arr_open(connection, f'/api/{version}/config/host') as response:
            config = json.load(response)
        if not isinstance(config, dict) or type(config.get('id')) is not int or config['id'] < 1:
            raise ValueError('Invalid application host settings')
        if not isinstance(config.get('username'), str) or not config['username'].strip():
            config.update(username=ADMIN_APP_USER, authenticationMethod='forms', authenticationRequired='enabled')
        config.update(password=password, passwordConfirmation=password)
        hosts.append((connection, f'/api/{version}/config/host/{config["id"]}', config))
    clients = []
    if 'transmission' in apps:
        for kind in ARR_COMPOSE_ADDRESSES:
            connection = arr_connection(kind)
            with arr_open(connection, '/api/v3/downloadclient') as response:
                entries = json.load(response)
            for entry in entries if isinstance(entries, list) else []:
                if entry.get('implementation') == 'Transmission' and entry.get('name') == SHARED_TRANSMISSION_CLIENT:
                    for field in entry.get('fields', []):
                        if field.get('name') == 'username':
                            field['value'] = ADMIN_APP_USER
                        elif field.get('name') == 'password':
                            field['value'] = password
                    clients.append((connection, f'/api/v3/downloadclient/{entry["id"]}?forceSave=true', entry))
    for connection, endpoint, payload in hosts:
        with arr_open(connection, endpoint, payload=payload, method='PUT'):
            pass
    if 'transmission' in apps:
        # Transmission's sidecar watches this bind-mounted file, so it must be rewritten in place.
        with open(os.environ['MARTINFLIX_TRANSMISSION_PASSWORD_FILE'], 'r+', encoding='utf-8') as secret:
            secret.write(password)
            secret.truncate()
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
            local_only = installer.get('localOnly', False)
        service['web'] = {'port': port, 'scheme': web.get('scheme', 'http'),
                          'path': web.get('path', '/'), 'localOnly': local_only}
    services = [service for service in all_services
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


def arr_items(kind, raw, now, episodes=None):
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
        entry = {
            'title': item.get('title') or 'Untitled',
            'year': item.get('year'),
            'status': status,
            'nextAiring': airing.isoformat() if airing else None,
            'releaseDate': movie_release(item) if kind == 'radarr' else None,
            'poster': poster,
        }
        if kind == 'sonarr' and episodes and media_id in episodes:
            entry['nextEpisode'] = episodes[media_id]
        if kind == 'radarr':
            entry['nextRelease'] = next_release(item, now.date())
        items.append(entry)
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
        return {'configured': True, 'items': arr_items(kind, raw, now, episodes)}
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


def arr_json(connection, endpoint, payload=None, method=None):
    with arr_open(connection, endpoint, payload=payload, method=method) as response:
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


def search_titles(kind, term):
    connection = arr_connection(kind)
    if connection is None:
        raise AddError(f'Connect {kind.capitalize()} to the dashboard first.')
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
        added = arr_json(connection, '/api/v3/series', payload=item, method='POST')
    else:
        availability = request.get('availability', 'released')
        if availability not in FILM_AVAILABILITY:
            raise AddError('Choose when the film counts as available.')
        item.update(minimumAvailability=availability, addOptions={'searchForMovie': search})
        added = arr_json(connection, '/api/v3/movie', payload=item, method='POST')
    return {'added': True, 'title': (added or item).get('title') or item.get('title'), 'searching': search}


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
        if path == '/api/status':
            catalog = {key: CATALOG[key] for key in ('apps', 'branches', 'startAfter')}
            data = {'docker': service_status(), 'backups': backup_status(), 'time': int(time.time()),
                    'canChangePassword': form_login, 'catalog': catalog}
            self.send_json(data)
        elif path == '/api/activity':
            self.send_json(dashboard_activity())
        elif path in ('/api/sonarr', '/api/radarr'):
            self.send_json(arr_data(path.rsplit('/', 1)[1]))
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
        if add := re.fullmatch(r'/api/add/(sonarr|radarr)', path):
            self.add_request(add.group(1))
            return
        if self.server.admin_state_file is None or path not in ('/login', '/setup-password', '/change-password'):
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
        changing = path == '/change-password'
        if changing == self.server.password_setup_required:
            self.redirect('/setup-password' if self.server.password_setup_required else '/')
            return
        self.change_password(fields, changing)

    def add_request(self, kind):
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
        try:
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
        scope = f'<p class="setup-note">This password also signs you in to {html.escape(app_list())}.</p>' if synced_apps() else ''
        if changing:
            scope += '<a href="/">Back to dashboard</a>'
        body = (template.replace('{{ACTION}}', '/change-password' if changing else '/setup-password')
                .replace('{{HEADING}}', 'Change your dashboard password' if changing else 'Set your dashboard password')
                .replace('{{CURRENT}}', current if changing else '')
                .replace('{{PASSWORD_SCOPE}}', scope)
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
