#!/usr/bin/env python3
"""Export a Plex server's settings and library definitions, without anything that identifies the server or account.

Run it on the host where Plex runs. It reads Preferences.xml from the Plex container, asks that Plex for its libraries
and their advanced settings, and prints JSON that MartinFlix can turn into defaults for a new Plex server:

    python3 deploy/tools/plex-export-settings.py > plex-settings.json

Options:
    --container NAME     Plex's container (default: plex). Use --preferences instead for Plex outside Docker.
    --preferences PATH   Read Preferences.xml from this path instead of the container.
    --url URL            Plex's address (default: http://127.0.0.1:32400).
    --media-root PATH    Folder Plex sees your media under; library folders are shown relative to it.

Left out on purpose: tokens, machine and certificate identifiers, account name and email, and state Plex records for
itself (timestamps, versions, last-used ports). Watch history and watched status are not settings and are not read.
"""
import argparse
import json
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from xml.etree import ElementTree

CONTAINER_PREFERENCES = '/config/Library/Application Support/Plex Media Server/Preferences.xml'
# Preferences that identify this server or account, or that Plex keeps as its own state.
# PublishServerOnPlexOnlineKey is kept: it is the Enable Remote Access switch, not an account detail.
PRIVATE = re.compile(r'Token|Identifier|UUID|Mail|Username|PlexOnline(?!Key)|PubSub|Certificate|Epoch|Version|^Last|'
                     r'^FirstRun|^OldestPreviousVersion|^AcceptedEULA|^LanguageInCloud|^MetricsEpoch|^Pms|Secret|Password',
                     re.IGNORECASE)


def read_preferences(container, path):
    if path:
        return Path(path).read_text(encoding='utf-8')
    result = subprocess.run(['docker', 'exec', container, 'cat', CONTAINER_PREFERENCES],
                            capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise SystemExit(f'Could not read Preferences.xml from the {container} container: {result.stderr.strip()}\n'
                         'Pass --container with Plex’s container name, or --preferences with the file’s path.')
    return result.stdout


def plex_get(url, token, path):
    request = urllib.request.Request(url.rstrip('/') + path, headers={'X-Plex-Token': token, 'Accept': 'application/json'})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=20) as response:
        return json.load(response).get('MediaContainer') or {}


def relative(location, media_root):
    if media_root and (location == media_root or location.startswith(media_root.rstrip('/') + '/')):
        return {'relative': location[len(media_root.rstrip('/')):].lstrip('/'), 'original': location}
    return {'original': location}


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--container', default='plex')
    parser.add_argument('--preferences')
    parser.add_argument('--url', default='http://127.0.0.1:32400')
    parser.add_argument('--media-root', default='')
    args = parser.parse_args()

    root = ElementTree.fromstring(read_preferences(args.container, args.preferences))
    token = root.get('PlexOnlineToken', '')
    settings = {key: value for key, value in sorted(root.attrib.items()) if not PRIVATE.search(key)}
    left_out = sorted(key for key in root.attrib if PRIVATE.search(key))

    libraries = []
    try:
        sections = plex_get(args.url, token, '/library/sections').get('Directory') or [] if token else []
    except OSError as exc:
        sections = []
        print(f'Could not reach Plex at {args.url} ({exc}), so libraries were not read. Use --url if it listens elsewhere.',
              file=sys.stderr)
    if token:
        for section in sections:
            changed = {}
            for setting in plex_get(args.url, token, f'/library/sections/{section["key"]}/prefs').get('Setting') or []:
                if str(setting.get('value')) != str(setting.get('default')):
                    changed[setting['id']] = setting.get('value')
            libraries.append({
                'title': section.get('title'), 'type': section.get('type'), 'agent': section.get('agent'),
                'scanner': section.get('scanner'), 'language': section.get('language'),
                'folders': [relative(item.get('path', ''), args.media_root) for item in section.get('Location') or []],
                'settings': changed,
            })
    else:
        print('This Plex has no account token, so libraries were not read.', file=sys.stderr)

    json.dump({'server': settings, 'libraries': libraries, 'leftOut': left_out}, sys.stdout, indent=2, sort_keys=False)
    print()
    print(f'Exported {len(settings)} server settings and {len(libraries)} libraries. '
          f'Left out {len(left_out)} private or internal values.', file=sys.stderr)


if __name__ == '__main__':
    main()
