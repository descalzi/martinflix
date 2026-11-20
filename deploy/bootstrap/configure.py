"""Apply the initial library and download settings through the apps' local APIs."""
import json
import urllib.error
import urllib.request

PROFILE_NAME = 'MartinFlix HD (720p/1080p)'
ARR_NAMES = {'sonarr': 'Sonarr', 'radarr': 'Radarr'}
ARR_PORTS = {'sonarr': 8989, 'radarr': 7878}
ROOT_FOLDERS = {'sonarr': '/data/TV Shows', 'radarr': '/data/Movies'}
TRANSMISSION_USER = 'martinflix'
PROWLARR_INDEXERS = {
    '1337x': ('1337x', 25),
    'eztv': ('EZTV', 25),
    'kickasstorrents-ws': ('kickasstorrents.ws', 25),
    'limetorrents': ('LimeTorrents', 25),
    'thepiratebay': ('The Pirate Bay', 25),
    'torrentdownloads': ('Torrent Downloads', 25),
    'world-torrent': ('World-torrent', 26),
    'yts': ('YTS', 25),
}
RELEASE_PROFILES = {
    'sonarr': [('Bad Stuff', True, ['MeGusta', 'scr', 'exe', 'ETHEL'])],
    'radarr': [('No OPUS', False, ['Opus']), ('No HEVC', False, ['HEVC'])],
}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())


class AppApiHttpError(ValueError):
    def __init__(self, name, route, status):
        super().__init__(f'{name}: {route} returned HTTP {status}. Check the app’s settings or logs and rerun.')
        self.status = status


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
            exc.close()
            raise AppApiHttpError(self.name, route, exc.code) from None
        except (OSError, ValueError):
            raise ValueError(f'{self.name}: could not read {route}. Check the app is running and rerun.') from None


def ensure_arr_login(api, initial_password):
    config = api.request('config/host')
    if config.get('authenticationMethod') not in ('none', None):
        return
    password = initial_password()
    config.update(authenticationMethod='forms', authenticationRequired='enabled',
                  username='admin', password=password, passwordConfirmation=password)
    api.request(f'config/host/{config["id"]}', config, method='PUT')


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


def ensure_release_profiles(api, app):
    existing_names = {profile['name'].casefold() for profile in api.request('releaseprofile')}
    for name, enabled, ignored in RELEASE_PROFILES[app]:
        if name.casefold() in existing_names:
            continue
        api.request('releaseprofile', {
            'name': name,
            'enabled': enabled,
            'required': [],
            'ignored': ignored,
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


def ensure_download_path(api, host):
    mappings = api.request('remotepathmapping')
    if not any(item['host'] == host and item['remotePath'].rstrip('/') == '/downloads'
               for item in mappings):
        api.request('remotepathmapping', {
            'host': host, 'remotePath': '/downloads/', 'localPath': '/data/downloads/',
        })


def seed_radarr_indexer_defaults(api):
    config = api.request('config/indexer')
    defaults = {'maximumSize': 4500, 'availabilityDelay': 1, 'rssSyncInterval': 120}
    if any(config.get(name) != value for name, value in defaults.items()):
        api.request(f'config/indexer/{config["id"]}', {**config, **defaults}, method='PUT')


def seed_prowlarr_indexers(api):
    existing = api.request('indexer')
    definitions = {item.get('definitionName') for item in existing}
    names = {item['name'].casefold() for item in existing}
    missing = {definition: (name, priority) for definition, (name, priority) in PROWLARR_INDEXERS.items()
               if definition not in definitions and name.casefold() not in names}
    if not missing:
        return
    templates = {item.get('definitionName'): item for item in api.request('indexer/schema')
                 if item.get('privacy') == 'public' and item.get('implementation') == 'Cardigann'}
    profile = next((item for item in api.request('appprofile') if item['name'] == 'Standard'), None)
    for definition, (name, priority) in missing.items():
        if definition not in templates or profile is None:
            print(f'   Prowlarr: {name} skipped. Its built-in definition or the Standard sync profile is unavailable.', flush=True)
            continue
        entry = templates[definition].copy()
        entry.pop('id', None)
        entry.update(name=name, priority=priority, appProfileId=profile['id'], enable=True, tags=[])
        try:
            api.request('indexer', entry)
        except AppApiHttpError as exc:
            if exc.status not in (400, 422):
                raise
            api.request('indexer', {**entry, 'enable': False})
            print(f'   Prowlarr: {name} was added disabled. Test it in Prowlarr before enabling it.', flush=True)
        else:
            print(f'   Prowlarr: {name} added and enabled.', flush=True)


def configure_apps(arr_keys, ports, media_path, prowlarr_key=None, transmission_password=None, *,
                   seed_indexers=False, seed_radarr_indexers=False, seed_prowlarr=False, arr_address='127.0.0.1',
                   downloads_behind_vpn=True):
    transmission_host = 'gluetun' if downloads_behind_vpn else 'transmission'
    prowlarr_host = 'gluetun' if downloads_behind_vpn else 'prowlarr'
    prowlarr = AppApi('Prowlarr', ports['prowlarr'], prowlarr_key, version=1) if prowlarr_key else None
    for app, name in ARR_NAMES.items():
        api = AppApi(name, ports[app], arr_keys[app], address=arr_address)
        ensure_quality_profile(api, app)
        ensure_release_profiles(api, app)
        if app == 'sonarr' and seed_indexers:
            seed_sonarr_indexer_defaults(api)
        if app == 'radarr' and seed_radarr_indexers:
            seed_radarr_indexer_defaults(api)
        if media_path:
            ensure_root_folder(api, ROOT_FOLDERS[app])
        if transmission_password:
            category = 'tvCategory' if app == 'sonarr' else 'movieCategory'
            ensure_connection(api, 'downloadclient', 'MartinFlix Transmission', 'Transmission', 'host', {
                'host': transmission_host, 'port': 9091, 'username': TRANSMISSION_USER,
                'password': transmission_password, category: app,
            })
            ensure_download_path(api, transmission_host)
        if prowlarr:
            ensure_connection(prowlarr, 'applications', name, name, 'baseUrl', {
                'baseUrl': f'http://{app}:{ARR_PORTS[app]}', 'prowlarrUrl': f'http://{prowlarr_host}:9696',
                'apiKey': arr_keys[app],
            }, syncLevel='fullSync')
    if prowlarr and seed_prowlarr:
        seed_prowlarr_indexers(prowlarr)
