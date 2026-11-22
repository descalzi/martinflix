import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'deploy/bootstrap'))
import launch  # noqa: E402


def done(stdout='', returncode=0):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr='')


class LaunchTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.home = Path(folder.name) / 'home'
        self.home.mkdir()
        self.socket = Path(folder.name) / 'docker.sock'
        self.socket.touch()
        self.calls = []
        patcher = patch.object(launch, 'SOCKET', self.socket)
        patcher.start()
        self.addCleanup(patcher.stop)
        environment = patch.dict(os.environ, {'HOME': str(self.home), 'HOSTNAME': 'f00dcafe', 'MARTINFLIX_STAGE': '',
                                              'MARTINFLIX_VERSION': '2026.11.22.7', 'MARTINFLIX_CHANNEL': 'edge',
                                              'MARTINFLIX_RUN_AS': '', 'MARTINFLIX_DOCKER_SOCKET': '', 'TERM': 'xterm'})
        environment.start()
        self.addCleanup(environment.stop)

    def run_docker(self, command, **kwargs):
        self.calls.append(command)
        if command[1] == 'inspect':
            return done('sha256:' + 'e' * 64 + '\n')
        return done()

    def second(self, args, interactive=True):
        with patch.object(launch.Path, 'is_socket', lambda path: path == self.socket), \
                patch.object(launch, 'account', return_value=(1000, 1000)):
            return launch.second_step(args, interactive, self.run_docker)

    def test_the_installer_sees_this_machines_folders_network_and_account(self):
        command = self.second(['--media-path', '/mnt/martinflix', '--backup-dir', '/mnt/martinflix/Backups'])
        self.assertEqual(command[:4], ['docker', 'run', '--rm', '-it'])
        self.assertIn('--network', command)
        self.assertEqual(command[command.index('--network') + 1], 'host')
        self.assertEqual(command[command.index('--user') + 1], '1000:1000')
        mounts = [command[index + 1] for index, item in enumerate(command) if item == '--mount']
        self.assertIn(f'type=bind,source={self.home},target={self.home}', mounts)
        self.assertIn('type=bind,source=/mnt/martinflix,target=/mnt/martinflix', mounts)
        self.assertIn('type=bind,source=/mnt/martinflix/Backups,target=/mnt/martinflix/Backups', mounts)
        self.assertIn('type=bind,source=/dev,target=/host/dev,readonly', mounts)
        self.assertIn('MARTINFLIX_STAGE=run', command)
        self.assertEqual(command[-4:], ['install', '--media-path', '/mnt/martinflix', '--backup-dir', '/mnt/martinflix/Backups'][-4:])
        image = command[command.index('install') - 1]
        self.assertEqual(image, 'ghcr.io/descalzi/martinflix:2026.11.22.7')
        self.assertIn(['docker', 'image', 'tag', 'sha256:' + 'e' * 64, image], self.calls, 'named by its version, no download')

    def test_an_update_finds_its_folders_in_the_saved_settings(self):
        state = self.home / '.local/state/martinflix'
        state.mkdir(parents=True)
        (state / 'install-options.json').write_text(json.dumps({'media_path': '/srv/media', 'backup_dir': str(self.home / 'b')}))
        command = self.second(['--update'], interactive=False)
        mounts = [command[index + 1] for index, item in enumerate(command) if item == '--mount']
        self.assertIn('type=bind,source=/srv/media,target=/srv/media', mounts)
        self.assertFalse(any('/b,' in mount for mount in mounts), 'folders inside home come with it')
        self.assertNotIn('-it', command)

    def test_relative_paths_and_root_are_refused_with_the_command_to_use(self):
        with self.assertRaisesRegex(launch.LaunchError, 'full path'):
            self.second(['--media-path', 'media'])
        with patch.object(launch.Path, 'is_socket', lambda path: True), patch.object(launch, 'account', return_value=(0, 0)), \
                self.assertRaisesRegex(launch.LaunchError, 'not as root'):
            launch.second_step([], True, self.run_docker)
        with patch.object(launch.Path, 'is_socket', lambda path: False), self.assertRaisesRegex(launch.LaunchError, 'docker.sock'):
            launch.second_step([], True, self.run_docker)

    def test_the_controller_can_name_the_account_setup_runs_as(self):
        with patch.dict(os.environ, {'MARTINFLIX_RUN_AS': '1001:1002'}):
            self.assertEqual(launch.account(self.home), (1001, 1002))
        self.assertEqual(launch.account(self.home), (self.home.stat().st_uid, self.home.stat().st_gid))

    def test_commands(self):
        with patch.object(launch, 'launch', side_effect=lambda args: args) as started:
            self.assertEqual(launch.main(['update', '--discover', 'off']), ['--update', '--discover', 'off'])
            self.assertEqual(launch.main(['install', '--media-path', '/m']), ['--media-path', '/m'])
            self.assertEqual(launch.main(['reset']), ['--reset'])
        self.assertEqual(started.call_count, 3)
        with redirect_stdout(io.StringIO()) as shown:
            self.assertEqual(launch.main([]), 0)
        self.assertIn('ghcr.io/descalzi/martinflix:edge install', shown.getvalue())
        with redirect_stdout(io.StringIO()) as shown:
            launch.main(['version'])
        self.assertIn('2026.11.22.7 (edge channel', shown.getvalue())


if __name__ == '__main__':
    unittest.main()
