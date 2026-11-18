#!/usr/bin/env python3
"""Capture existing application-generated backups, without touching live databases."""
import argparse
import datetime
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

SOURCES = {'sonarr': '/var/lib/sonarr/Backups/scheduled', 'radarr': '/var/lib/radarr/Backups/scheduled', 'prowlarr': '/var/lib/prowlarr/Backups/scheduled', 'bazarr': '/var/lib/bazarr/backup', 'tautulli': '/var/lib/tautulli/backups'}

def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()

def create(destination):
    now = datetime.datetime.now(datetime.timezone.utc)
    target = destination / now.strftime('%Y%m%dT%H%M%S.%fZ')
    target.mkdir(mode=0o700, parents=True, exist_ok=False)
    manifest = {'created': now.isoformat(), 'files': [], 'missing': [], 'errors': [], 'scope': 'latest existing application-generated backup per configured service; not a complete host backup'}
    for name, source in SOURCES.items():
        try:
            candidates = [p for p in Path(source).iterdir() if p.is_file() and not p.is_symlink() and p.suffix.lower() in ('.zip', '.gz', '.backup')]
            if not candidates:
                manifest['missing'].append(name)
                continue
            latest = max(candidates, key=lambda p: p.stat().st_mtime)
            out = target / name / latest.name
            out.parent.mkdir(mode=0o700)
            shutil.copyfile(latest, out)
            os.chmod(out, 0o600)
            manifest['files'].append({'service': name, 'path': str(out.relative_to(target)), 'sha256': digest(out), 'bytes': out.stat().st_size, 'source_modified': datetime.datetime.fromtimestamp(latest.stat().st_mtime, datetime.timezone.utc).isoformat()})
        except OSError as exc:
            manifest['errors'].append(name + ': ' + str(exc))
    (target / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    os.chmod(target / 'manifest.json', 0o600)
    print(str(target))
    if manifest['missing'] or manifest['errors']:
        print('Incomplete: ' + ', '.join(manifest['missing'] + manifest['errors']), file=sys.stderr)
        return 1
    return 0

def verify(target):
    try:
        manifest = json.loads((target / 'manifest.json').read_text())
        if not manifest['files'] or manifest['missing'] or manifest['errors']:
            raise ValueError('snapshot is incomplete')
        for entry in manifest['files']:
            path = (target / entry['path']).resolve()
            if target.resolve() not in path.parents or digest(path) != entry['sha256']:
                raise ValueError('checksum mismatch: ' + entry['path'])
        print('Checksums verified for %d files. This does not verify application restore.' % len(manifest['files']))
        return 0
    except (OSError, ValueError, KeyError) as exc:
        print('Verification failed: ' + str(exc), file=sys.stderr)
        return 1

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    c = sub.add_parser('create')
    c.add_argument('--destination', type=Path, default=Path(os.environ.get('MARTINFLIX_BACKUP_DIR', str(Path.home() / '.local/share/martinflix/backups'))).expanduser())
    v = sub.add_parser('verify')
    v.add_argument('snapshot', type=Path)
    args = parser.parse_args()
    sys.exit(create(args.destination) if args.command == 'create' else verify(args.snapshot))
