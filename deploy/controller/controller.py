#!/usr/bin/env python3
"""MartinFlix controller: the one container with Docker access.

It sits on a private network that only the dashboard joins, and it does only what is listed here: report container
status, restart an app, update an app to its newest image, take backups, and update MartinFlix itself or apply its
settings by running setup again (see releases.py). Every action needs the PIN chosen during setup, which the
controller checks itself, so even a compromised dashboard cannot act without it. Apps that share another app's
network or process namespace, such as the download apps behind Gluetun, are restarted or recreated after it.
"""
import hashlib
import hmac
import json
import os
import secrets
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from backups import NAME, BackupError, Backups
from releases import Releases, UpdateError, secret_values, setup_arguments
import vpncheck

# Restarting these ends the request that asked for it, so the controller answers first and restarts them after.
SELF = frozenset({'dashboard', 'controller'})
RESTART_DELAY_SECONDS = 1.0
MAX_FAILURES = 5
LOCKOUT_SECONDS = 300
PIN_LENGTHS = range(4, 9)
SCRYPT = {'n': 2 ** 14, 'r': 8, 'p': 1}


def hash_pin(pin, salt=None):
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(pin.encode(), salt=salt, dklen=32, **SCRYPT)
    return f'scrypt${salt.hex()}${digest.hex()}'


def pin_matches(pin, encoded):
    try:
        scheme, salt, digest = encoded.strip().split('$')
        if scheme != 'scrypt':
            return False
        candidate = hashlib.scrypt(pin.encode(), salt=bytes.fromhex(salt), dklen=32, **SCRYPT)
        return hmac.compare_digest(candidate.hex(), digest)
    except ValueError:
        return False


def valid_pin(pin):
    return isinstance(pin, str) and pin.isascii() and pin.isdigit() and len(pin) in PIN_LENGTHS


class PinGuard:
    """Checks PINs and locks actions for a while after repeated wrong guesses."""

    def __init__(self, pin_file, clock=time.monotonic):
        self.pin_file = Path(pin_file)
        self.clock = clock
        self.failures = 0
        self.locked_until = 0.0
        self.lock = threading.Lock()

    def check(self, pin):
        """Returns None when the PIN is right, otherwise (HTTP status, message)."""
        with self.lock:
            remaining = self.locked_until - self.clock()
            if remaining > 0:
                minutes = max(1, round(remaining / 60))
                return 423, f'Too many wrong PINs. Try again in {minutes} minute{"s" if minutes != 1 else ""}.'
            try:
                encoded = self.pin_file.read_text()
            except OSError:
                return 503, 'No PIN is set. Rerun setup to choose one.'
            if valid_pin(pin) and pin_matches(pin, encoded):
                self.failures = 0
                return None
            self.failures += 1
            if self.failures >= MAX_FAILURES:
                self.failures = 0
                self.locked_until = self.clock() + LOCKOUT_SECONDS
                return 423, f'Too many wrong PINs. Actions are locked for {LOCKOUT_SECONDS // 60} minutes.'
            left = MAX_FAILURES - self.failures
            return 403, f'Wrong PIN. {left} tr{"ies" if left != 1 else "y"} left.'

    def replace(self, current, new):
        """Store a new PIN once the current one checks out. Returns None, or (HTTP status, message)."""
        if problem := self.check(current):
            return problem
        if not valid_pin(new):
            return 400, 'Use 4 to 8 digits for the new PIN.'
        if new == current:
            return 400, 'Choose a PIN different from the current one.'
        with self.lock:
            temporary = self.pin_file.with_name(f'.{self.pin_file.name}.{secrets.token_hex(4)}')
            try:
                descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, 'w') as output:
                    output.write(hash_pin(new) + '\n')
                os.replace(temporary, self.pin_file)
            except OSError:
                temporary.unlink(missing_ok=True)
                return 500, 'The controller could not save the new PIN. Your current PIN still works.'
        return None


def dependents(services, name):
    """Services that must follow `name`, in order: those using its network or process namespace, transitively."""
    order, queue = [], [name]
    while queue:
        current = queue.pop(0)
        for other, spec in services.items():
            if other in order or other == name:
                continue
            if f'service:{current}' in (spec.get('network_mode'), spec.get('pid')):
                order.append(other)
                queue.append(other)
    return order


class Stack:
    """Runs Docker and Compose for the project setup saved in the controller config."""

    def __init__(self, config_file, runner=subprocess.run, later=None):
        self.config_file = Path(config_file)
        self.runner = runner
        self.cached = (None, None)
        self.later = later or (lambda action: threading.Timer(RESTART_DELAY_SECONDS, action).start())

    def config(self):
        return json.loads(self.config_file.read_text())

    def run(self, args, timeout=900):
        config = self.config()
        environment = {'PATH': os.environ.get('PATH', '/usr/local/bin:/usr/bin:/bin'), 'HOME': os.environ.get('HOME', '/tmp'),
                       **config['env']}
        return self.runner(['docker', *args], env=environment, capture_output=True, text=True, timeout=timeout)

    def compose(self, *args, timeout=900):
        return self.run([*self.config()['compose'], *args], timeout=timeout)

    def services(self):
        """The project's services from `docker compose config`, cached until setup rewrites the config."""
        stamp = self.config_file.stat().st_mtime_ns
        if self.cached[0] != stamp:
            result = self.compose('config', '--format', 'json', timeout=60)
            if result.returncode:
                raise RuntimeError('Compose could not read the stack: ' + tail(result))
            self.cached = (stamp, json.loads(result.stdout)['services'])
        return self.cached[1]

    def controllable(self):
        return self.services()

    @staticmethod
    def updatable(spec):
        # MartinFlix's own containers change version together, through Settings, not one by one.
        image = spec.get('image') or ''
        return 'build' not in spec and not image.startswith(os.environ.get('MARTINFLIX_IMAGE_REPO', 'ghcr.io/descalzi/martinflix') + ':')

    def containers(self):
        project = self.config()['project']
        listed = self.run(['ps', '-aq', '--filter', f'label=com.docker.compose.project={project}'], timeout=30)
        ids = listed.stdout.split()
        if listed.returncode or not ids:
            return {}
        inspected = self.run(['inspect', *ids], timeout=30)
        if inspected.returncode:
            return {}
        result = {}
        for item in json.loads(inspected.stdout):
            service = item['Config']['Labels'].get('com.docker.compose.service')
            if not service:
                continue
            state = item['State']
            result[service] = {
                'id': item['Id'], 'state': state.get('Status', 'unknown'), 'startedAt': state.get('StartedAt'),
                'health': (state.get('Health') or {}).get('Status'), 'exitCode': state.get('ExitCode'),
                'image': item['Config'].get('Image'),
            }
        return result

    def status(self):
        services = self.controllable()
        containers = self.containers()
        return {'services': {name: {**containers.get(name, {'state': 'missing'}), 'dependents': dependents(services, name),
                                    'canUpdate': self.updatable(spec)}
                             for name, spec in services.items()}}

    def restart(self, name):
        services = self.controllable()
        if name in SELF:
            self.later(lambda: self.compose('restart', name))
            return {'followed': [], 'deferred': True}
        follow = dependents(services, name)
        for group in ([name], follow):
            if group:
                result = self.compose('restart', *group)
                if result.returncode:
                    raise RuntimeError(f'Restarting {", ".join(group)} failed: {tail(result)}')
        return {'followed': follow}

    def update(self, name):
        services = self.controllable()
        if not self.updatable(services[name]):
            raise ValueError(f'{name} is part of MartinFlix. Update MartinFlix from Settings instead.')
        follow = dependents(services, name)
        before = self.containers().get(name, {}).get('id')
        pulled = self.compose('pull', '--quiet', name)
        if pulled.returncode:
            raise RuntimeError(f'Downloading the {name} image failed: {tail(pulled)}')
        started = self.compose('up', '-d', '--no-deps', name)
        if started.returncode:
            raise RuntimeError(f'Starting {name} failed: {tail(started)}')
        if self.containers().get(name, {}).get('id') == before:
            return {'updated': False, 'followed': [], 'image': services[name].get('image')}
        if follow:
            # A recreated container has a new network namespace, so the apps sharing it must be recreated too.
            recreated = self.compose('up', '-d', '--no-deps', '--force-recreate', *follow)
            if recreated.returncode:
                raise RuntimeError(f'Recreating {", ".join(follow)} failed: {tail(recreated)}')
        return {'updated': True, 'followed': follow, 'image': services[name].get('image')}


class TransmissionPassword:
    """Restart Transmission so it picks up a new shared password, when the dashboard asks after changing it.

    The LinuxServer image sets Transmission's login from USER and PASS each time the container starts, and PASS is
    read from the password file the dashboard has just rewritten. The controller only ever restarts Transmission,
    and only when that file differs from the version it last applied, so this request needs no PIN.
    """

    def __init__(self, stack, busy, marker):
        self.stack = stack
        self.busy = busy
        self.marker = Path(marker)

    def apply(self):
        source = self.stack.config().get('transmissionPasswordFile')
        if not source or 'transmission' not in self.stack.controllable():
            raise KeyError('Transmission is not part of this stack.')
        digest = hashlib.sha256(Path(source).read_bytes()).hexdigest()
        try:
            if self.marker.read_text().strip() == digest:
                return {'restarted': False}
        except FileNotFoundError:
            pass
        if not self.busy.acquire(timeout=300):
            raise RuntimeError('Another restart or update is still running. Submit the same password again shortly.')
        try:
            self.stack.restart('transmission')
        finally:
            self.busy.release()
        self.marker.write_text(digest)
        return {'restarted': True}


def tail(result, limit=600):
    text = ((result.stderr or '') + (result.stdout or '')).strip()
    return text[-limit:] or f'exit code {result.returncode}'


class Handler(BaseHTTPRequestHandler):
    server_version = 'MartinFlixController'

    def log_message(self, fmt, *args):
        print('%s - %s' % (self.address_string(), fmt % args), flush=True)

    def send_json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def authorized(self):
        expected = f'Bearer {self.server.token}'
        return hmac.compare_digest(self.headers.get('Authorization', '').encode(), expected.encode())

    def do_GET(self):
        if self.path == '/ping':
            self.send_json({'ok': True})
            return
        if not self.authorized():
            self.send_json({'error': 'Not authorized.'}, 401)
            return
        routes = {
            '/status': lambda: {**self.server.stack.status(), 'backups': self.server.backups.status(),
                                'release': self.server.releases.brief()},
            '/release': self.server.releases.status,
            '/release/brief': self.server.releases.brief,
            '/settings': self.server.releases.settings,
        }
        if self.path not in routes:
            self.send_json({'error': 'Not found.'}, 404)
            return
        try:
            self.send_json(routes[self.path]())
        except (OSError, RuntimeError, ValueError, KeyError, subprocess.SubprocessError) as exc:
            self.send_json({'error': str(exc) or 'Docker status is unavailable.'}, 502)

    def do_POST(self):
        if not self.authorized():
            self.send_json({'error': 'Not authorized.'}, 401)
            return
        if self.path == '/transmission-password':
            try:
                self.send_json({'ok': True, **self.server.transmission.apply()})
            except KeyError as exc:
                self.send_json({'error': exc.args[0]}, 404)
            except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
                self.send_json({'error': str(exc) or 'Transmission could not be restarted.'}, 502)
            return
        if self.path == '/vpn-test':
            # Only looks, so it needs no PIN.
            try:
                self.send_json(vpncheck.check(self.server.stack))
            except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
                self.send_json({'error': str(exc) or 'The VPN check could not run.'}, 502)
            return
        if self.path == '/release/check':
            # Only downloads an image, so it needs no PIN.
            self.server.releases.check_soon()
            self.send_json({'ok': True, 'checking': True}, 202)
            return
        action = {'/restart': 'restart', '/update': 'update', '/pin': 'pin', '/backup': 'backup',
                  '/release/update': 'release', '/release/finish': 'finish', '/settings': 'settings',
                  '/restore': 'restore'}.get(self.path)
        if action is None:
            self.send_json({'error': 'Not found.'}, 404)
            return
        try:
            length = int(self.headers.get('Content-Length', '0'))
            request = json.loads(self.rfile.read(min(length, 16384)) or b'{}')
            name, pin = request.get('service'), request.get('pin')
        except (ValueError, AttributeError):
            self.send_json({'error': 'Invalid request.'}, 400)
            return
        if action == 'pin':
            if problem := self.server.pins.replace(pin, request.get('new')):
                self.send_json({'error': problem[1]}, problem[0])
            else:
                self.send_json({'ok': True})
            return
        if problem := self.server.pins.check(pin):
            self.send_json({'error': problem[1]}, problem[0])
            return
        if action in ('release', 'settings', 'restore', 'finish'):
            self.start_setup(action, request)
            return
        if updating(self.server):
            self.send_json({'error': 'MartinFlix is updating. Try again when it has finished.'}, 409)
            return
        if action == 'backup':
            # A backup takes minutes, so it runs in the background; the Backups page follows its progress.
            if self.server.backups.running:
                self.send_json({'error': 'A backup is already running.'}, 409)
                return
            threading.Thread(target=take_backup, args=(self.server, 'manual'), daemon=True).start()
            self.send_json({'ok': True, 'started': True}, 202)
            return
        try:
            if name not in self.server.stack.controllable():
                self.send_json({'error': 'That app cannot be controlled from here.'}, 404)
                return
            if not self.server.busy.acquire(blocking=False):
                self.send_json({'error': 'Another restart or update is still running. Try again shortly.'}, 409)
                return
            try:
                result = getattr(self.server.stack, action)(name)
            finally:
                self.server.busy.release()
            self.send_json({'ok': True, 'action': action, 'service': name, **result})
        except (OSError, RuntimeError, ValueError, KeyError, subprocess.SubprocessError) as exc:
            self.send_json({'error': str(exc) or f'The {action} failed.'}, 502)


    def start_setup(self, action, request):
        """Update MartinFlix, or apply settings, by running setup in the updater container."""
        try:
            if action == 'restore':
                name = request.get('backup')
                if not isinstance(name, str) or not NAME.fullmatch(name):
                    self.send_json({'error': 'Choose a backup to restore.'}, 400)
                    return
                folder = self.server.backups.root() / name
                if not (folder / 'manifest.json').is_file():
                    self.send_json({'error': 'That backup is no longer in the backup folder.'}, 404)
                    return
                if self.server.backups.running:
                    self.send_json({'error': 'A backup is running. Restore once it has finished.'}, 409)
                    return
                result = self.server.releases.start('restore', ['--restore', str(folder)], detail=name)
            elif action == 'settings':
                arguments = setup_arguments(request.get('settings') or {})
                given = secret_values(request.get('secrets'))
                if not arguments and not given:
                    self.send_json({'error': 'Nothing changed.'}, 400)
                    return
                result = self.server.releases.start('settings', arguments, given)
            elif action == 'finish':
                result = self.server.releases.start('finish', detail=str(request.get('kind') or '')[:20])
            else:
                result = self.server.releases.start('update')
            self.send_json({'ok': True, **result}, 202)
        except UpdateError as exc:
            self.send_json({'error': str(exc)}, exc.status)
        except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
            self.send_json({'error': str(exc) or 'The update could not start.'}, 502)


def updating(server):
    try:
        return server.releases.running()
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        return False


def take_backup(server, kind):
    """One backup, holding the same lock as restarts and updates so nothing restarts an app mid-backup."""
    with server.busy:
        if kind == 'scheduled' and updating(server):
            print('Backup skipped: MartinFlix is updating. The next one runs tomorrow.', flush=True)
            return
        try:
            manifest = server.backups.run(kind)
            print(f'Backup {manifest["created"]} done: {len(manifest["apps"])} apps'
                  + (f', problems: {"; ".join(manifest["errors"])}' if manifest['errors'] else '.'), flush=True)
        except BackupError as exc:
            print(f'Backup failed: {exc}', flush=True)


def schedule_backups(server):
    while True:
        when = server.backups.first_run()
        server.backups.sleep(max(1.0, (when - server.backups.now()).total_seconds()))
        take_backup(server, 'scheduled')


def serve():
    token = Path(os.environ['MARTINFLIX_CONTROL_TOKEN_FILE']).read_text().strip()
    if len(token) < 32:
        raise SystemExit('The controller token is missing or too short. Rerun setup.')
    server = ThreadingHTTPServer(('0.0.0.0', int(os.environ.get('MARTINFLIX_CONTROL_PORT', '9800'))), Handler)
    server.token = token
    server.stack = Stack(os.environ['MARTINFLIX_CONTROL_CONFIG'])
    server.pins = PinGuard(os.environ['MARTINFLIX_CONTROL_PIN_FILE'])
    server.busy = threading.Lock()
    marker = Path(os.environ['MARTINFLIX_CONTROL_PIN_FILE']).with_name('transmission-password-applied')
    server.transmission = TransmissionPassword(server.stack, server.busy, marker)
    server.backups = Backups(server.stack)
    server.releases = Releases(server.stack)
    threading.Thread(target=server.releases.check_forever, daemon=True).start()
    threading.Thread(target=server.releases.tidy_forever, daemon=True).start()
    if server.stack.config().get('backupDir'):
        threading.Thread(target=schedule_backups, args=(server,), daemon=True).start()
    print('MartinFlix controller listening on port %d' % server.server_address[1], flush=True)
    server.serve_forever()


if __name__ == '__main__':
    serve()
