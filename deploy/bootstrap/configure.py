"""Apply the initial library and download settings through the apps' local APIs."""
import base64
import json
from pathlib import PurePosixPath
import urllib.error
import urllib.parse
import urllib.request

PROFILE_NAME = 'MartinFlix HD (720p/1080p)'
ARR_NAMES = {'sonarr': 'Sonarr', 'radarr': 'Radarr'}
ARR_PORTS = {'sonarr': 8989, 'radarr': 7878}
ROOT_FOLDERS = {'sonarr': '/data/TV Shows', 'radarr': '/data/Movies'}
TRANSMISSION_USER = 'admin'
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
# Prowlarr's tags say which indexers each app gets, as on the reference host: Prowlarr gives an app only the indexers
# that share one of its tags. FlareSolverr, which gets indexers past Cloudflare's browser check, carries the same tags,
# so it serves every tagged indexer. (The reference host also has "books", for a books app this stack doesn't have;
# TorrentDownloads carries only that tag there, so here it has none and stays for searching in Prowlarr itself.)
PROWLARR_INDEXER_TAGS = {
    '1337x': ('movies',), 'eztv': ('movies', 'series'), 'kickasstorrents-ws': ('movies', 'series'),
    'limetorrents': ('movies', 'series'), 'thepiratebay': ('movies', 'series'), 'torrentdownloads': (),
    'world-torrent': ('movies', 'series'), 'yts': ('movies',),
}
PROWLARR_APP_TAGS = {'Sonarr': ('movies', 'series'), 'Radarr': ('movies',)}
FLARESOLVERR_TAGS = ('movies', 'series')
# The tag earlier setups used for FlareSolverr, replaced by the ones above.
OLD_FLARESOLVERR_TAG = 'flaresolverr'


def ensure_tags(api, labels):
    """Prowlarr's IDs for these tag labels, adding the ones it doesn't have."""
    existing = {item['label'].casefold(): item['id'] for item in api.request('tag')}
    for label in labels:
        if label not in existing:
            existing[label] = api.request('tag', {'label': label})['id']
    return {label: existing[label] for label in labels}


def tag_ids(labels, ids):
    return [ids[label] for label in labels]
# The site address each indexer uses on the reference host, when Prowlarr still offers it. Others (World-torrent has
# none there) get the first address Prowlarr lists, as its own Add dialog would, which Prowlarr uses anyway.
PROWLARR_BASE_URLS = {
    '1337x': 'https://1337x.to/',
    'eztv': 'https://eztvx.to/',
    'kickasstorrents-ws': 'https://kickass.ws/',
    'limetorrents': 'https://www.limetorrents.lol/',
    'thepiratebay': 'https://thepiratebay.host/',
    'torrentdownloads': 'https://www.torrentdownloads.pro/',
    'yts': 'https://yts.mx/',
}


def chosen_base_url(item):
    """The address an indexer should use, or None when Prowlarr lists none for it."""
    offered = [url for url in item.get('indexerUrls') or [] if isinstance(url, str)]
    preferred = PROWLARR_BASE_URLS.get(item.get('definitionName'))
    return preferred if preferred in offered else (offered[0] if offered else None)


def with_base_url(item):
    """The indexer with its base URL filled in when it has none. Returns None when nothing changes."""
    url = chosen_base_url(item)
    fields = item.get('fields') or []
    field = next((field for field in fields if field.get('name') == 'baseUrl'), None)
    if url is None or field is None or field.get('value'):
        return None
    return {**item, 'fields': [{**entry, 'value': url} if entry is field else entry for entry in fields]}
# The reference host's Transmission choices, applied once to installs that started before setup shipped them.
# New installs get the same values from transmission-settings.json before Transmission first starts.
TRANSMISSION_SESSION = {
    'seedRatioLimit': 0,
    'seedRatioLimited': True,
    'blocklist-enabled': True,
    'blocklist-url': 'https://github.com/Naunter/BT_BlockLists/raw/master/bt_blocklists.gz',
    'encryption': 'required',
    'utp-enabled': True,
    'lpd-enabled': True,
    'peer-limit-per-torrent': 75,
}
# The reference host's Bazarr choices. Settings use Bazarr's own form names; subzero-* switch subtitle clean-up mods.
BAZARR_PROVIDERS = ['supersubtitles', 'yifysubtitles', 'tvsubtitles', 'subtitulamostv', 'opensubtitlescom',
                    'embeddedsubtitles']
BAZARR_SETTINGS = {
    'settings-analytics-enabled': 'false',
    'settings-general-adaptive_searching': 'false',
    'settings-general-days_to_upgrade_subs': '20',
    'settings-general-dont_notify_manual_actions': 'true',
    'settings-general-embedded_subs_show_desired': 'false',
    'settings-general-language_equals': ['spa:spl', 'spl:spa'],
    'settings-general-serie_default_enabled': 'true',
    'settings-general-serie_default_profile': '1',
    'settings-general-movie_default_enabled': 'true',
    'settings-general-movie_default_profile': '1',
    'settings-general-upgrade_frequency': '24',
    'settings-general-upgrade_manual': 'false',
    'settings-opensubtitlescom-include_ai_translated': 'true',
    'settings-sonarr-series_sync': '180',
    'settings-sonarr-full_update_hour': '2',
    'settings-radarr-movies_sync': '180',
    'settings-radarr-full_update_hour': '3',
    'settings-subsync-use_subsync': 'true',
    'settings-subsync-no_fix_framerate': 'false',
    'subzero-remove_HI': 'true',
    'subzero-common': 'true',
    'subzero-remove_tags': 'true',
}
BAZARR_LANGUAGES = ['en', 'fr', 'pb', 'es', 'ea']


def bazarr_profile_item(item_id, language):
    return {'id': item_id, 'language': language, 'audio_exclude': 'False', 'hi': 'False', 'forced': 'False',
            'audio_only_include': 'False'}


BAZARR_PROFILES = [
    {'profileId': 1, 'name': 'All Languages', 'cutoff': None, 'mustContain': [], 'mustNotContain': [],
     'originalFormat': 0, 'tag': None,
     'items': [bazarr_profile_item(number, code) for number, code in ((2, 'ea'), (3, 'pb'), (4, 'en'), (5, 'fr'))]},
    {'profileId': 2, 'name': 'Not-English', 'cutoff': None, 'mustContain': [], 'mustNotContain': [],
     'originalFormat': 0, 'tag': None,
     'items': [bazarr_profile_item(number, code) for number, code in ((1, 'pb'), (2, 'ea'))]},
]
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


class TransmissionRpc:
    """Minimal Transmission RPC client: basic auth plus the session ID handshake."""

    def __init__(self, address, port, username, password):
        self.url = f'http://{address}:{port}/transmission/rpc'
        token = base64.b64encode(f'{username}:{password}'.encode()).decode()
        self.headers = {'Authorization': f'Basic {token}', 'Content-Type': 'application/json'}

    def call(self, method, arguments=None, timeout=30):
        body = json.dumps({'method': method, 'arguments': arguments or {}}).encode()
        for _ in range(2):
            request = urllib.request.Request(self.url, data=body, headers=self.headers, method='POST')
            try:
                with OPENER.open(request, timeout=timeout) as response:
                    result = json.loads(response.read())
            except urllib.error.HTTPError as exc:
                session = exc.headers.get('X-Transmission-Session-Id')
                exc.close()
                if exc.code == 409 and session:
                    self.headers['X-Transmission-Session-Id'] = session
                    continue
                raise ValueError(f'Transmission: {method} returned HTTP {exc.code}. Check its login and rerun.') from None
            except (OSError, ValueError):
                raise ValueError(f'Transmission: could not reach {method}. Check the app is running and rerun.') from None
            if result.get('result') != 'success':
                raise ValueError(f'Transmission: {method} failed: {result.get("result")}')
            return result.get('arguments') or {}
        raise ValueError('Transmission: the RPC session could not be established.')


def seed_transmission_defaults(rpc):
    rpc.call('session-set', TRANSMISSION_SESSION)
    try:
        size = rpc.call('blocklist-update', timeout=90).get('blocklist-size')
        print(f'   Transmission: blocklist loaded{f" with {size:,} rules" if isinstance(size, int) else ""}.', flush=True)
    except ValueError:
        print('   Transmission: the blocklist will download at its nightly update.', flush=True)


def refresh_health(api):
    """Ask the app to re-run its health checks now. A failure only delays the refresh, so it is not fatal."""
    try:
        api.request('command', {'name': 'CheckHealth'})
    except ValueError:
        print(f'   {api.name}: its health checks will refresh on their own schedule.', flush=True)


class BazarrApi:
    """Bazarr's settings API takes form fields rather than JSON, and an X-API-KEY header."""

    def __init__(self, address, port, key):
        self.name = 'Bazarr'
        self.base = f'http://{address}:{port}/api/'
        self.key = key

    def get_settings(self):
        request = urllib.request.Request(self.base + 'system/settings', headers={'X-API-KEY': self.key, 'Accept': 'application/json'})
        try:
            with OPENER.open(request, timeout=30) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as exc:
            exc.close()
            raise AppApiHttpError(self.name, 'system/settings', exc.code) from None
        except (OSError, ValueError):
            raise ValueError('Bazarr: could not read its settings. Check the app is running and rerun.') from None

    def save(self, fields):
        """Fields are (name, value) pairs; repeated names carry Bazarr's list settings."""
        body = urllib.parse.urlencode(fields).encode()
        request = urllib.request.Request(self.base + 'system/settings', data=body, method='POST', headers={
            'X-API-KEY': self.key, 'Content-Type': 'application/x-www-form-urlencoded'})
        try:
            with OPENER.open(request, timeout=60) as response:
                response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors='replace').strip()[:200]
            exc.close()
            raise ValueError(f'Bazarr rejected its settings (HTTP {exc.code}){": " + detail if detail else ""}.') from None
        except OSError:
            raise ValueError('Bazarr: could not save its settings. Check the app is running and rerun.') from None

    def run_task(self, task):
        """Start one of Bazarr's scheduled tasks now, such as syncing its series from Sonarr."""
        body = urllib.parse.urlencode({'taskid': task}).encode()
        request = urllib.request.Request(self.base + 'system/tasks', data=body, method='POST', headers={
            'X-API-KEY': self.key, 'Content-Type': 'application/x-www-form-urlencoded'})
        with OPENER.open(request, timeout=30) as response:
            response.read()


def form_fields(settings):
    fields = []
    for name, value in settings.items():
        fields.extend((name, item) for item in value) if isinstance(value, list) else fields.append((name, value))
    return fields


def ensure_bazarr_login(api, initial_password):
    if (api.get_settings().get('auth') or {}).get('type') in ('form', 'basic'):
        return
    api.save([('settings-auth-type', 'form'), ('settings-auth-username', 'admin'),
              ('settings-auth-password', initial_password())])


def configure_bazarr(api, arr_keys, *, seed=False, opensubtitles=None):
    """Connect Bazarr to Sonarr and Radarr on every run; apply the reference choices once."""
    fields = []
    for app in ('sonarr', 'radarr'):
        # Bazarr reaches both apps by their Compose service names on the stack's network.
        fields += [(f'settings-general-use_{app}', 'true'), (f'settings-{app}-ip', app),
                   (f'settings-{app}-port', str(ARR_PORTS[app])), (f'settings-{app}-base_url', ''),
                   (f'settings-{app}-ssl', 'false'), (f'settings-{app}-apikey', arr_keys[app])]
    if seed:
        providers = [name for name in BAZARR_PROVIDERS if name != 'opensubtitlescom' or opensubtitles]
        fields += form_fields({**BAZARR_SETTINGS, 'settings-general-enabled_providers': providers})
        if opensubtitles:
            fields += [('settings-opensubtitlescom-username', opensubtitles[0]),
                       ('settings-opensubtitlescom-password', opensubtitles[1])]
        fields += [('languages-enabled', code) for code in BAZARR_LANGUAGES]
        fields.append(('languages-profiles', json.dumps(BAZARR_PROFILES)))
    api.save(fields)
    if seed:
        print('   Bazarr: subtitle providers, languages and the "All Languages" and "Not-English" profiles are set.', flush=True)
        if not opensubtitles:
            print('   Bazarr: OpenSubtitles.com is off. Add your login under Settings → Providers to turn it on.', flush=True)


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


def ensure_connection(api, route, name, implementation, match_field, values, optional=None, **settings):
    """Add a connection unless one with this name, or to the same place, exists. `optional` values are set when the
    app's version has those settings."""
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
    values = {**values, **{key: value for key, value in (optional or {}).items() if key in available}}
    entry.pop('id', None)
    entry.update(name=name, enable=True, **settings)
    for field in entry['fields']:
        if field['name'] in values:
            field['value'] = values[field['name']]
    api.request(route + '/test', entry)
    api.request(route, entry)


# Download apps are reached as "gluetun" when they share the VPN's network, otherwise by their own service names.
# Every run points existing connections at the current route, so the route can change in either direction.
ROUTE_HOSTS = {'transmission': ('transmission', 'gluetun'), 'prowlarr': ('prowlarr', 'gluetun')}
FLARESOLVERR_URLS = {True: 'http://127.0.0.1:8191/', False: 'http://flaresolverr:8191/'}


def put_if_changed(api, route, item, changes):
    """Apply field changes to one saved record, writing only when something differs."""
    changed = False
    for field in item.get('fields') or []:
        if field.get('name') in changes and field.get('value') != changes[field['name']]:
            field['value'] = changes[field['name']]
            changed = True
    if changed:
        api.request(f'{route}/{item["id"]}?forceSave=true', item, method='PUT')
    return changed


def sync_transmission_client(api, host, password):
    """Point MartinFlix's Transmission connection at the current route and the shared admin login."""
    for item in api.request('downloadclient'):
        if item['implementation'] != 'Transmission':
            continue
        if item['name'] != 'MartinFlix Transmission' and field_value(item, 'host') not in (*ROUTE_HOSTS['transmission'], '127.0.0.1'):
            continue
        # Stored passwords come back masked, so set the password on every run.
        for field in item['fields']:
            if field['name'] == 'password':
                field['value'] = None
        put_if_changed(api, 'downloadclient', item, {'host': host, 'username': TRANSMISSION_USER, 'password': password})


def ensure_download_path(api, host):
    mappings = api.request('remotepathmapping')
    for item in mappings:
        # A mapping for the other route would point imports at a host that no longer serves Transmission.
        if item['host'] != host and item['host'] in ROUTE_HOSTS['transmission'] and item['remotePath'].rstrip('/') == '/downloads':
            api.request(f'remotepathmapping/{item["id"]}', method='DELETE')
    if not any(item['host'] == host and item['remotePath'].rstrip('/') == '/downloads'
               for item in mappings):
        api.request('remotepathmapping', {
            'host': host, 'remotePath': '/downloads/', 'localPath': '/data/downloads/',
        })


def sync_prowlarr_routes(prowlarr, prowlarr_host, behind_vpn):
    """Keep Prowlarr's own address in Sonarr and Radarr, and any FlareSolverr proxy, on the current route."""
    for item in prowlarr.request('applications'):
        if item.get('implementation') in ARR_NAMES.values() and \
                field_value(item, 'prowlarrUrl') in tuple(f'http://{host}:9696' for host in ROUTE_HOSTS['prowlarr']):
            put_if_changed(prowlarr, 'applications', item, {'prowlarrUrl': f'http://{prowlarr_host}:9696'})
    for item in prowlarr.request('indexerproxy'):
        if item.get('implementation') == 'FlareSolverr' and field_value(item, 'host') in FLARESOLVERR_URLS.values():
            put_if_changed(prowlarr, 'indexerproxy', item, {'host': FLARESOLVERR_URLS[behind_vpn]})


def seed_radarr_indexer_defaults(api):
    config = api.request('config/indexer')
    defaults = {'maximumSize': 4500, 'availabilityDelay': 1, 'rssSyncInterval': 120}
    if any(config.get(name) != value for name, value in defaults.items()):
        api.request(f'config/indexer/{config["id"]}', {**config, **defaults}, method='PUT')


def ensure_flaresolverr_proxy(api, behind_vpn, ids, retag=False):
    """Make FlareSolverr Prowlarr's proxy for the tagged indexers, keeping one already set up. `retag` gives an
    existing one the tags too, once, replacing the "flaresolverr" tag earlier setups used."""
    proxy = next((item for item in api.request('indexerproxy') if item.get('implementation') == 'FlareSolverr'), None)
    wanted = tag_ids(FLARESOLVERR_TAGS, ids)
    if proxy is not None:
        if retag and sorted(proxy.get('tags') or []) != sorted(wanted):
            api.request(f'indexerproxy/{proxy["id"]}?forceSave=true', {**proxy, 'tags': wanted}, method='PUT')
            print('   Prowlarr: FlareSolverr now serves the indexers tagged movies or series.', flush=True)
        return
    entry = next((item for item in api.request('indexerproxy/schema') if item.get('implementation') == 'FlareSolverr'), None)
    if entry is None:
        print('   Prowlarr: FlareSolverr proxy skipped. This Prowlarr version does not offer it.', flush=True)
        return
    entry.pop('id', None)
    entry.update(name='FlareSolverr', tags=wanted)
    for field in entry['fields']:
        if field['name'] == 'host':
            field['value'] = FLARESOLVERR_URLS[behind_vpn]
    api.request('indexerproxy?forceSave=true', entry)
    print('   Prowlarr: FlareSolverr added as the proxy for the indexers tagged movies or series.', flush=True)


def tag_prowlarr_apps(api, ids):
    """Give the Sonarr and Radarr applications their tags, so each gets only the indexers meant for it."""
    for item in api.request('applications'):
        labels = PROWLARR_APP_TAGS.get(item.get('implementation'))
        if labels is None or item.get('name') not in PROWLARR_APP_TAGS:
            continue
        wanted = sorted(set(item.get('tags') or []) | set(tag_ids(labels, ids)))
        if wanted != sorted(item.get('tags') or []):
            api.request(f'applications/{item["id"]}?forceSave=true', {**item, 'tags': wanted}, method='PUT')
            print(f'   Prowlarr: {item["name"]} gets the indexers tagged {" or ".join(labels)}.', flush=True)


def update_seeded_prowlarr_indexers(api, *, enable, ids=None, drop=None):
    """Put right the indexers earlier setups added: turn them on (`enable`), give them a site address if they have
    none, and give them their tags (`ids`), without the old "flaresolverr" tag (`drop`)."""
    for item in api.request('indexer'):
        definition = item.get('definitionName')
        if definition not in PROWLARR_INDEXERS:
            continue
        current = item.get('tags') or []
        tags = current
        if ids is not None:
            tags = sorted({tag for tag in current if tag != drop} | set(tag_ids(PROWLARR_INDEXER_TAGS[definition], ids)))
        addressed = with_base_url(item)
        if (enable and not item.get('enable')) or sorted(tags) != sorted(current) or addressed:
            api.request(f'indexer/{item["id"]}?forceSave=true',
                        {**(addressed or item), 'enable': item.get('enable') or enable, 'tags': tags}, method='PUT')
            print(f'   Prowlarr: {item["name"]} updated'
                  f'{f", using {chosen_base_url(item)}" if addressed else ""}.', flush=True)


def drop_old_flaresolverr_tag(api, drop):
    """Remove the "flaresolverr" tag earlier setups made, once nothing uses it."""
    if drop is None:
        return
    for route in ('indexer', 'indexerproxy', 'applications'):
        if any(drop in (item.get('tags') or []) for item in api.request(route)):
            return
    api.request(f'tag/{drop}', method='DELETE')


def seed_prowlarr_indexers(api, ids):
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
        entry = with_base_url(entry) or entry
        entry.update(name=name, priority=priority, appProfileId=profile['id'], enable=True,
                     tags=tag_ids(PROWLARR_INDEXER_TAGS[definition], ids))
        # Save without Prowlarr's connection test: an indexer that is down today still belongs in the list, and
        # Prowlarr's health checks report the ones that keep failing.
        try:
            api.request('indexer?forceSave=true', entry)
        except AppApiHttpError as exc:
            if exc.status not in (400, 422):
                raise
            print(f'   Prowlarr: {name} skipped. Prowlarr rejected its settings (HTTP {exc.status}).', flush=True)
        else:
            print(f'   Prowlarr: {name} added and enabled.', flush=True)


MEDIA_ROOT = '/data'
SHARED_TRANSMISSION = 'MartinFlix Transmission'
ARR_COLLECTIONS = {'sonarr': ('series', 'seriesIds', 'series'), 'radarr': ('movie', 'movieIds', 'films')}


def moved_path(path, old):
    """A path in the old stack's media folder, as this installation sees it; None for any other path."""
    path, old = (path or '').rstrip('/'), old.rstrip('/')
    if path == old:
        return MEDIA_ROOT
    if path.startswith(old + '/'):
        return MEDIA_ROOT + path[len(old):]
    return None


def adopt_legacy(arr_keys, ports, legacy_media, media, *, arr_address='127.0.0.1', prowlarr_key=None,
                 prowlarr_address='127.0.0.1', downloads_behind_vpn=True, transmission=False, plex_alias=None,
                 bazarr=None, log=print):
    """Once, after restoring an export of a stack setup didn't install: point its data at this installation. Series
    and films move to this installation's media folder without their files moving (they are the same files), and
    the old stack's download clients and links between apps give way to this installation's own."""
    for app, name in ARR_NAMES.items():
        api = AppApi(name, ports[app], arr_keys[app], address=arr_address)
        collection, ids_field, noun = ARR_COLLECTIONS[app]
        groups, elsewhere = {}, 0
        for item in api.request(collection) or []:
            path = item.get('path') or ''
            root = item.get('rootFolderPath') or str(PurePosixPath(path).parent)
            new_root = moved_path(root, legacy_media)
            if new_root is None or moved_path(path, legacy_media) is None:
                elsewhere += not path.startswith(MEDIA_ROOT + '/')
                continue
            groups.setdefault(new_root, []).append(item['id'])
        old_roots = [folder for folder in api.request('rootfolder') if moved_path(folder['path'], legacy_media)]
        for new_root, ids in sorted(groups.items()):
            # The folder is the old one, on the same share; on another machine it may not exist yet.
            (media / new_root.removeprefix(MEDIA_ROOT + '/')).mkdir(parents=True, exist_ok=True)
            ensure_root_folder(api, new_root)
            # Moving to another root folder without moving files keeps each title's own folder name.
            api.request(f'{collection}/editor', {ids_field: ids, 'rootFolderPath': new_root, 'moveFiles': False},
                        method='PUT')
        for folder in old_roots:
            api.request(f'rootfolder/{folder["id"]}', method='DELETE')
        moved = sum(len(ids) for ids in groups.values())
        counted = noun if moved != 1 or noun == 'series' else noun.removesuffix('s')
        log(f'   {name}: ' + (f'{moved} {counted} now in {", ".join(sorted(groups))}' if moved else f'no {noun} to move')
            + (f'; {elsewhere} elsewhere left as they were' if elsewhere else '') + '.', flush=True)
        if transmission:
            for client in api.request('downloadclient'):
                if client.get('implementation') == 'Transmission' and client.get('name') != SHARED_TRANSMISSION:
                    api.request(f'downloadclient/{client["id"]}', method='DELETE')
        for mapping in api.request('remotepathmapping'):
            if not (mapping.get('localPath') or '').startswith(MEDIA_ROOT + '/'):
                api.request(f'remotepathmapping/{mapping["id"]}', method='DELETE')
        if plex_alias:
            # Plex still knows the media by the old stack's folders, so library updates name them that way.
            for item in api.request('notification'):
                if item.get('implementation') == 'PlexServer':
                    put_if_changed(api, 'notification', item, {'mapFrom': MEDIA_ROOT + '/',
                                                               'mapTo': plex_alias.rstrip('/') + '/'})
    if prowlarr_key:
        prowlarr = AppApi('Prowlarr', ports['prowlarr'], prowlarr_key, version=1, address=prowlarr_address)
        prowlarr_host = 'gluetun' if downloads_behind_vpn else 'prowlarr'
        for item in prowlarr.request('applications'):
            app = {name: app for app, name in ARR_NAMES.items()}.get(item.get('implementation'))
            if app:
                put_if_changed(prowlarr, 'applications', item, {
                    'baseUrl': f'http://{app}:{ARR_PORTS[app]}', 'prowlarrUrl': f'http://{prowlarr_host}:9696',
                    'apiKey': arr_keys[app]})
        if transmission:
            for client in prowlarr.request('downloadclient'):
                if client.get('implementation') == 'Transmission' and client.get('name') != SHARED_TRANSMISSION:
                    prowlarr.request(f'downloadclient/{client["id"]}', method='DELETE')
        log('   Prowlarr: Sonarr and Radarr are reached at this installation’s addresses.', flush=True)
    if bazarr:
        # Bazarr learns the new folders from Sonarr and Radarr at its next sync; start that now.
        for task in ('update_series', 'update_movies'):
            try:
                bazarr.run_task(task)
            except (OSError, ValueError):
                log('   Bazarr: its sync with Sonarr and Radarr will pick up the new folders within the hour.', flush=True)
                break


def configure_apps(arr_keys, ports, media_path, prowlarr_key=None, transmission_password=None, *,
                   seed_indexers=False, seed_radarr_indexers=False, seed_prowlarr=False, arr_address='127.0.0.1',
                   downloads_behind_vpn=True, prowlarr_address='127.0.0.1', flaresolverr=False,
                   enable_prowlarr_indexers=False, address_prowlarr_indexers=False, tag_prowlarr=False):
    transmission_host = 'gluetun' if downloads_behind_vpn else 'transmission'
    prowlarr_host = 'gluetun' if downloads_behind_vpn else 'prowlarr'
    prowlarr = AppApi('Prowlarr', ports['prowlarr'], prowlarr_key, version=1, address=prowlarr_address) if prowlarr_key else None
    ids = ensure_tags(prowlarr, ('movies', 'series')) if prowlarr else None
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
            sync_transmission_client(api, transmission_host, transmission_password)
            ensure_download_path(api, transmission_host)
        if prowlarr:
            ensure_connection(prowlarr, 'applications', name, name, 'baseUrl', {
                'baseUrl': f'http://{app}:{ARR_PORTS[app]}', 'prowlarrUrl': f'http://{prowlarr_host}:9696',
                'apiKey': arr_keys[app],
            }, syncLevel='fullSync', tags=tag_ids(PROWLARR_APP_TAGS[name], ids))
    if prowlarr:
        sync_prowlarr_routes(prowlarr, prowlarr_host, downloads_behind_vpn)
    if prowlarr and transmission_password:
        # So a release can also be sent to Transmission straight from Prowlarr's search. Behind the VPN, Prowlarr and
        # Transmission share Gluetun's network, so Transmission is on Prowlarr's own localhost.
        prowlarr_transmission = '127.0.0.1' if downloads_behind_vpn else 'transmission'
        ensure_connection(prowlarr, 'downloadclient', 'MartinFlix Transmission', 'Transmission', 'host', {
            'host': prowlarr_transmission, 'port': 9091, 'username': TRANSMISSION_USER, 'password': transmission_password,
        }, optional={'category': 'prowlarr'})
        sync_transmission_client(prowlarr, prowlarr_transmission, transmission_password)
    if prowlarr:
        old = next((item['id'] for item in prowlarr.request('tag') if item['label'].casefold() == OLD_FLARESOLVERR_TAG), None)
        if flaresolverr:
            ensure_flaresolverr_proxy(prowlarr, downloads_behind_vpn, ids, retag=tag_prowlarr)
        if tag_prowlarr:
            tag_prowlarr_apps(prowlarr, ids)
        if seed_prowlarr:
            seed_prowlarr_indexers(prowlarr, ids)
        elif enable_prowlarr_indexers or address_prowlarr_indexers or tag_prowlarr:
            update_seeded_prowlarr_indexers(prowlarr, enable=enable_prowlarr_indexers, ids=ids if tag_prowlarr else None,
                                            drop=old)
        if tag_prowlarr:
            drop_old_flaresolverr_tag(prowlarr, old)
    # Health warnings, such as a missing download folder, otherwise linger until the apps' next scheduled check.
    for api in (*(AppApi(name, ports[app], arr_keys[app], address=arr_address) for app, name in ARR_NAMES.items()),
                *([prowlarr] if prowlarr else [])):
        refresh_health(api)
