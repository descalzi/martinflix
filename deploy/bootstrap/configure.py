"""Apply the initial library and download settings through the apps' local APIs."""
import json
import urllib.error
import urllib.request

PROFILE_NAME = 'MartinFlix HD (720p/1080p)'
ARR_NAMES = {'sonarr': 'Sonarr', 'radarr': 'Radarr'}
ARR_PORTS = {'sonarr': 8989, 'radarr': 7878}
ROOT_FOLDERS = {'sonarr': '/data/TV Shows', 'radarr': '/data/Movies'}
TRANSMISSION_USER = 'martinflix'


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())


class AppApi:
    def __init__(self, name, port, key, version=3, address='127.0.0.1'):
        self.name = name
        self.base = f'http://{address}:{port}/api/v{version}/'
        self.key = key

    def request(self, route, payload=None, method=None):
        headers = {'X-Api-Key': self.key, 'Accept': 'application/json'}
        body = None
        if payload is not None:
            body = json.dumps(payload).encode()
            headers['Content-Type'] = 'application/json'
        try:
            request = urllib.request.Request(self.base + route, data=body, headers=headers, method=method)
            with OPENER.open(request, timeout=30) as response:
                data = response.read()
            return json.loads(data) if data else None
        except urllib.error.HTTPError as exc:
            raise ValueError(f'{self.name}: {route} returned HTTP {exc.code}. Check the app’s settings or logs and rerun.') from None
        except (OSError, ValueError):
            raise ValueError(f'{self.name}: could not read {route}. Check the app is running and rerun.') from None


def item_name(item):
    return item.get('name') or (item.get('quality') or {}).get('name', '')


def select_quality(item, allowed):
    children = item.get('items') or []
    selected = [select_quality(child, allowed) for child in children]
    item['allowed'] = any(selected) if children else item_name(item) in allowed
    return item['allowed']


def ensure_quality_profile(api, app):
    if any(profile['name'] == PROFILE_NAME for profile in api.request('qualityprofile')):
        return
    profile = api.request('qualityprofile/schema')
    allowed = {f'{source}-{resolution}p' for source in ('HDTV', 'WEBDL', 'WEBRip', 'Bluray')
               for resolution in (720, 1080)}
    if app == 'radarr':
        allowed.add('Remux-1080p')
    cutoff_name = 'HDTV-720p' if app == 'sonarr' else 'Bluray-720p'
    for item in profile['items']:
        select_quality(item, allowed)
    cutoff = next((item for item in profile['items'] if item_name(item) == cutoff_name and item['allowed']), None)
    if cutoff is None:
        raise ValueError(f'{api.name}: no compatible 720p quality was available for the MartinFlix profile.')
    profile.update(name=PROFILE_NAME, upgradeAllowed=False, cutoff=cutoff.get('id') or cutoff['quality']['id'])
    profile.pop('id', None)
    api.request('qualityprofile', profile)


def ensure_sonarr_exclusions(api):
    name = 'Bad Stuff'
    if any(profile['name'] == name for profile in api.request('releaseprofile')):
        return
    api.request('releaseprofile', {
        'name': name,
        'enabled': True,
        'required': [],
        'ignored': ['MeGusta', 'scr', 'exe', 'ETHEL'],
        'indexerId': 0,
        'tags': [],
    })


def seed_sonarr_indexer_defaults(api):
    config = api.request('config/indexer')
    updates = {'minimumAge': 120, 'maximumSize': 3000}
    updates = {name: value for name, value in updates.items() if config.get(name) == 0}
    if updates:
        api.request(f'config/indexer/{config["id"]}', {**config, **updates}, method='PUT')


def ensure_root_folder(api, path):
    if not any(folder['path'].rstrip('/') == path for folder in api.request('rootfolder')):
        api.request('rootfolder', {'path': path})


def field_value(entry, name):
    return next((field.get('value') for field in entry['fields'] if field['name'] == name), None)


def ensure_connection(api, route, name, implementation, match_field, values, **settings):
    for item in api.request(route):
        if item['name'] == name:
            return
        if item['implementation'] == implementation and field_value(item, match_field) == values[match_field]:
            return
    entry = next((item for item in api.request(route + '/schema') if item['implementation'] == implementation), None)
    if entry is None:
        raise ValueError(f'{api.name}: {implementation} is not supported by this app version.')
    available = {field['name'] for field in entry['fields']}
    if missing := values.keys() - available:
        raise ValueError(f'{api.name}: missing {implementation} settings: {", ".join(sorted(missing))}.')
    entry.pop('id', None)
    entry.update(name=name, enable=True, **settings)
    for field in entry['fields']:
        if field['name'] in values:
            field['value'] = values[field['name']]
    api.request(route + '/test', entry)
    api.request(route, entry)


def ensure_download_path(api):
    mappings = api.request('remotepathmapping')
    if not any(item['host'] == 'gluetun' and item['remotePath'].rstrip('/') == '/downloads'
               for item in mappings):
        api.request('remotepathmapping', {
            'host': 'gluetun', 'remotePath': '/downloads/', 'localPath': '/data/downloads/',
        })


def configure_apps(arr_keys, ports, media_path, prowlarr_key=None, transmission_password=None, *, seed_indexers=False, arr_address='127.0.0.1'):
    prowlarr = AppApi('Prowlarr', ports['prowlarr'], prowlarr_key, version=1) if prowlarr_key else None
    for app, name in ARR_NAMES.items():
        api = AppApi(name, ports[app], arr_keys[app], address=arr_address)
        ensure_quality_profile(api, app)
        if app == 'sonarr':
            ensure_sonarr_exclusions(api)
            if seed_indexers:
                seed_sonarr_indexer_defaults(api)
        if media_path:
            ensure_root_folder(api, ROOT_FOLDERS[app])
        if prowlarr:
            category = 'tvCategory' if app == 'sonarr' else 'movieCategory'
            ensure_connection(api, 'downloadclient', 'MartinFlix Transmission', 'Transmission', 'host', {
                'host': 'gluetun', 'port': 9091, 'username': TRANSMISSION_USER,
                'password': transmission_password, category: app,
            })
            ensure_download_path(api)
            ensure_connection(prowlarr, 'applications', name, name, 'baseUrl', {
                'baseUrl': f'http://{app}:{ARR_PORTS[app]}', 'prowlarrUrl': 'http://gluetun:9696',
                'apiKey': arr_keys[app],
            }, syncLevel='fullSync')
