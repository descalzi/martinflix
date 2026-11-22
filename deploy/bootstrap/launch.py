#!/usr/bin/env python3
"""The MartinFlix image's entry point.

Setup runs in two steps. You start the first one with only the Docker socket and your home folder. It works out
which other folders setup needs, such as the media and backup folders, then starts the installer itself in a second
container. That container sees exactly those folders, at the same paths as on this machine, on this machine's
network, as your account. Docker resolves every path in the Compose files on this machine, so the paths must match.
"""
import os
import subprocess
import sys
from pathlib import Path

APP = Path(__file__).resolve().parents[2]
BOOTSTRAP = APP / 'deploy' / 'bootstrap'
SOCKET = Path('/var/run/docker.sock')
# This machine's /dev, read-only, so setup can check for the VPN's tunnel device and a GPU for Plex.
HOST_DEV = '/host/dev'
START = ('docker run --rm -it -v /var/run/docker.sock:/var/run/docker.sock -v "$HOME:$HOME" -e HOME \\\n'
         '  {image} {command}')

USAGE = """MartinFlix {version}

Commands
  install [options]   Set up MartinFlix on this machine.
  update [options]    Update MartinFlix, keeping its settings. Options change them.
  reset [options]     Archive this installation's app data and start fresh.
  version             Show this image's version.

Start setup with:
  {start}

Add --help after install for every option."""


def image_repo():
    return os.environ.get('MARTINFLIX_IMAGE_REPO', 'ghcr.io/descalzi/martinflix')


def own_image():
    return f'{image_repo()}:{os.environ.get("MARTINFLIX_VERSION", "dev")}'


def version_text():
    revision = os.environ.get('MARTINFLIX_REVISION', '')[:7]
    channel = os.environ.get('MARTINFLIX_CHANNEL', 'dev')
    return f'MartinFlix {os.environ.get("MARTINFLIX_VERSION", "dev")} ({channel} channel' + (
        f', revision {revision})' if revision else ')')


def start_command(command='install'):
    channel = os.environ.get('MARTINFLIX_CHANNEL')
    return START.format(image=f'{image_repo()}:{channel if channel in ("edge", "stable") else "edge"}', command=command)


class LaunchError(Exception):
    pass


def account(home):
    """The account setup runs as: the owner of the home folder, unless the controller names the stack's account."""
    if run_as := os.environ.get('MARTINFLIX_RUN_AS'):
        uid, _, gid = run_as.partition(':')
        return int(uid), int(gid)
    info = home.stat()
    return info.st_uid, info.st_gid


def tag_own_image(run=subprocess.run):
    """Name this image by its version, so the second step and Compose find it here without downloading it again."""
    image = own_image()
    found = run(['docker', 'inspect', '--format', '{{.Image}}', os.environ.get('HOSTNAME', '')],
                capture_output=True, text=True)
    if found.returncode == 0 and found.stdout.strip().startswith('sha256:'):
        run(['docker', 'image', 'tag', found.stdout.strip(), image], capture_output=True)
    elif run(['docker', 'image', 'inspect', image], capture_output=True).returncode:
        run(['docker', 'pull', '--quiet', image], capture_output=True)
    return image


def folders(args, home):
    """The folders setup reads and writes, besides the home folder: state, media and backups."""
    sys.path.insert(0, str(BOOTSTRAP))
    import install
    parsed = install.build_parser().parse_args(args)
    state = install.resolve_state_directory(parsed.state_dir)
    try:
        saved = install.saved_options(state)
    except (OSError, ValueError):
        saved = {}
    result = [home]
    for flag, value in (('--state-dir', state), ('--media-path', parsed.media_path or saved.get('media_path')),
                        ('--backup-dir', parsed.backup_dir or saved.get('backup_dir')),
                        ('--restore', parsed.restore)):
        if not value:
            continue
        path = Path(value)
        if not path.is_absolute():
            raise LaunchError(f'Use a full path for {flag}, starting with /, such as /mnt/media.')
        if ',' in str(path):
            raise LaunchError(f'Choose a folder without a comma in its path for {flag}.')
        if path != home and not path.is_relative_to(home) and path not in result:
            result.append(path)
    return result


def second_step(args, interactive, run=subprocess.run):
    """The docker run command for the installer, with this machine's folders, network and account."""
    home = Path(os.environ.get('HOME') or '/')
    if not SOCKET.is_socket():
        raise LaunchError('Setup needs Docker. Start it with:\n  ' + start_command())
    if str(home) == '/' or not home.is_dir() or account(home)[0] == 0:
        raise LaunchError('Start setup as your own account with your home folder, not as root:\n  ' + start_command())
    if ',' in str(home):
        raise LaunchError('Setup cannot use a home folder with a comma in its path. Add --state-dir and --backup-dir.')
    uid, gid = account(home)
    host_socket = os.environ.get('MARTINFLIX_DOCKER_SOCKET') or str(SOCKET)
    mounts = folders(args, home)
    image = tag_own_image(run)
    command = ['docker', 'run', '--rm', *(['-it'] if interactive else []), '--network', 'host',
               '--user', f'{uid}:{gid}', '--group-add', str(SOCKET.stat().st_gid), '--workdir', str(home),
               '--label', 'io.martinflix.setup=1',
               '-e', f'HOME={home}', '-e', 'MARTINFLIX_STAGE=run', '-e', f'MARTINFLIX_DOCKER_SOCKET={host_socket}',
               '-e', f'MARTINFLIX_HOST_DEV={HOST_DEV}', '-e', f'TERM={os.environ.get("TERM") or "dumb"}',
               '-e', 'PYTHONUNBUFFERED=1',
               '-v', f'{host_socket}:/var/run/docker.sock',
               '--mount', f'type=bind,source=/dev,target={HOST_DEV},readonly']
    for folder in mounts:
        # --mount, unlike -v, refuses a folder that does not exist rather than creating it as root.
        command += ['--mount', f'type=bind,source={folder},target={folder}']
    return [*command, image, 'install', *args]


def launch(args, run=subprocess.run):
    if os.environ.get('MARTINFLIX_STAGE') == 'run':
        sys.path.insert(0, str(BOOTSTRAP))
        import install
        return install.main(args)
    interactive = sys.stdin.isatty()
    try:
        command = second_step(args, interactive, run)
    except (LaunchError, ValueError) as exc:
        print(exc, file=sys.stderr)
        return 1
    try:
        # Without a terminal (the dashboard's updates), one stream keeps setup's messages and errors in order.
        code = run(command, **({} if interactive else {'stderr': subprocess.STDOUT})).returncode
    except KeyboardInterrupt:
        return 130
    if code == 125:
        print('\nDocker could not start setup. If it says a folder does not exist, check the path, and that the NAS '
              'share is mounted on this machine.', file=sys.stderr)
    return code


def main(argv):
    command, args = (argv[0], argv[1:]) if argv else ('help', [])
    if command == 'dashboard':
        os.chdir(APP)
        os.execv(sys.executable, [sys.executable, '-B', 'server.py'])
    if command == 'controller':
        os.execv(sys.executable, [sys.executable, '-B', str(APP / 'deploy/controller/controller.py')])
    if command == 'version':
        print(version_text())
        return 0
    if command in ('install', 'update', 'reset'):
        extra = {'install': [], 'update': ['--update'], 'reset': ['--reset']}[command]
        if extra and extra[0] in args:
            extra = []
        return launch([*extra, *args])
    print(USAGE.format(version=os.environ.get('MARTINFLIX_VERSION', 'dev'), start=start_command()))
    return 0 if command in ('help', '--help', '-h') else 2


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
