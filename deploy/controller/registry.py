"""Ask image registries for the digest each tag points at now, without downloading anything.

The controller has no internet access, so it runs this in a short-lived container of the MartinFlix image on
Docker's ordinary network. A HEAD request for a manifest returns its digest in a header; registries don't count it as
a download. Usage: registry.py IMAGE... prints {"IMAGE": "sha256:..." or null, ...} as JSON.
"""
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

# A multi-platform index first, then a single manifest, in Docker's and OCI's formats.
ACCEPT = ', '.join((
    'application/vnd.oci.image.index.v1+json', 'application/vnd.docker.distribution.manifest.list.v2+json',
    'application/vnd.docker.distribution.manifest.v2+json', 'application/vnd.oci.image.manifest.v1+json'))
DOCKER_HUB = 'registry-1.docker.io'
DIGEST = re.compile(r'sha256:[0-9a-f]{64}')
TIMEOUT = 20


def parse(reference):
    """registry host, repository and tag from an image reference such as lscr.io/linuxserver/sonarr:latest."""
    name, _, digest = reference.partition('@')
    if digest:
        return None
    first, _, rest = name.partition('/')
    if rest and ('.' in first or ':' in first or first == 'localhost'):
        host, path = first, rest
    else:
        host, path = DOCKER_HUB, name
    path, _, tag = path.rpartition(':') if ':' in path.rsplit('/', 1)[-1] else (path, '', 'latest')
    if host in ('docker.io', 'index.docker.io'):
        host = DOCKER_HUB
    if host == DOCKER_HUB and '/' not in path:
        path = f'library/{path}'
    return host, path, tag or 'latest'


def bearer_token(challenge, repository, opener):
    """An anonymous pull token, from the service named in the registry's WWW-Authenticate challenge."""
    fields = dict(re.findall(r'(\w+)="([^"]*)"', challenge or ''))
    realm = fields.get('realm', '')
    if not realm.startswith('https://'):
        return None
    query = {'scope': fields.get('scope') or f'repository:{repository}:pull'}
    if fields.get('service'):
        query['service'] = fields['service']
    with opener.open(realm + '?' + urllib.parse.urlencode(query), timeout=TIMEOUT) as response:
        body = json.loads(response.read(65536))
    return body.get('token') or body.get('access_token')


def remote_digest(reference, opener=None):
    """The digest the tag points at in its registry now, or None if it can't be read."""
    opener = opener or urllib.request.build_opener()
    parsed = parse(reference)
    if not parsed:
        return None
    host, repository, tag = parsed
    url = f'https://{host}/v2/{repository}/manifests/{urllib.parse.quote(tag)}'
    headers = {'Accept': ACCEPT}
    for attempt in range(2):
        try:
            with opener.open(urllib.request.Request(url, headers=headers, method='HEAD'), timeout=TIMEOUT) as response:
                digest = response.headers.get('Docker-Content-Digest', '')
            return digest if DIGEST.fullmatch(digest) else None
        except urllib.error.HTTPError as exc:
            challenge = exc.headers.get('WWW-Authenticate', '')
            exc.close()
            if exc.code != 401 or attempt or not challenge.lower().startswith('bearer'):
                return None
            token = bearer_token(challenge, repository, opener)
            if not token:
                return None
            headers['Authorization'] = f'Bearer {token}'
    return None


def main(references):
    found = {}
    for reference in references:
        try:
            found[reference] = remote_digest(reference)
        except (OSError, ValueError):
            found[reference] = None
    print(json.dumps(found))


if __name__ == '__main__':
    main(sys.argv[1:])
