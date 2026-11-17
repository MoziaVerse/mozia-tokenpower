import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('deployment', Path(__file__).with_name('test.py'))
deployment = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deployment)
SHA = 'a' * 40


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        self.previous = {'commit': 'b' * 40, 'branch': 'main', 'repository': 'fixture'}
        (self.root / 'deployment.json').write_text(json.dumps(self.previous))
        self.old = self.root / 'releases/old'
        self.old.mkdir(parents=True)
        (self.root / 'current').symlink_to(self.old)
        (self.root / 'compose.yml').write_text('existing config retained')
        self.calls = []
        self.running_image = 'old-image'
        self.fail_backup = False
        self.fail_start = False
        self.health = 'healthy'
        self.labels = {'com.docker.compose.project': deployment.PROJECT,
                       'com.docker.compose.service': 'newapi',
                       'com.docker.compose.project.config_files': str(self.root / 'compose.yml')}
        for name, value in [('ROOT', self.root), ('inspect', self.inspect)]:
            replacement = patch.object(deployment, name, value)
            replacement.start()
            self.addCleanup(replacement.stop)
        replacement = patch.object(deployment.subprocess, 'run', self.run_command)
        replacement.start()
        self.addCleanup(replacement.stop)

    def inspect(self, *args):
        if args[0] == 'image':
            return {'Id': 'candidate-image', 'Config': {'Labels': {'org.opencontainers.image.revision': SHA}}}
        return {'Image': self.running_image, 'Config': {'Image': self.running_image, 'Labels': self.labels},
                'State': {'Health': {'Status': self.health}}}

    def run_command(self, args, **kwargs):
        self.calls.append(args)
        if 'pg_dump' in args:
            self.assertFalse((self.root / 'deployment-in-progress.json').exists())
            if not self.fail_backup:
                kwargs['stdout'].write(b'isolated database backup')
            return subprocess.CompletedProcess(args, int(self.fail_backup))
        if 'up' in args:
            self.assertEqual(args[-1], 'newapi')
            self.assertIn('--no-deps', args)
            self.assertTrue((self.root / 'deployment-in-progress.json').is_file())
            self.assertTrue(any((self.root / 'backups').glob('*.dump')))
            self.running_image = 'candidate-image'
            if self.fail_start:
                raise subprocess.CalledProcessError(1, args)
        return subprocess.CompletedProcess(args, 0)

    def test_success_preserves_configuration_and_only_updates_gateway(self):
        deployment.deploy(SHA, '10')
        self.assertEqual((self.root / 'compose.yml').read_text(), 'existing config retained')
        self.assertEqual((self.root / 'current').resolve(), self.root / 'releases' / SHA)
        self.assertEqual(json.loads((self.root / 'deployment.json').read_text())['commit'], SHA)
        self.assertFalse((self.root / 'deployment-in-progress.json').exists())
        count = len(self.calls)
        deployment.deploy('c' * 40, '9')
        self.assertEqual(len(self.calls), count)

    def test_backup_failure_does_not_restart_and_can_be_retried(self):
        self.fail_backup = True
        with self.assertRaisesRegex(RuntimeError, 'backup failed'):
            deployment.deploy(SHA, '10')
        self.assertFalse(any('up' in args for args in self.calls))
        self.assertEqual((self.root / 'current').resolve(), self.old)
        self.fail_backup = False
        deployment.deploy(SHA, '10')

    def test_failed_start_preserves_previous_record_and_blocks_following_deployments(self):
        self.fail_start = True
        with self.assertRaises(subprocess.CalledProcessError):
            deployment.deploy(SHA, '10')
        self.assertEqual(json.loads((self.root / 'deployment.json').read_text()), self.previous)
        self.assertEqual((self.root / 'current').resolve(), self.old)
        count = len(self.calls)
        with self.assertRaisesRegex(RuntimeError, 'incomplete'):
            deployment.deploy('c' * 40, '11')
        self.assertEqual(len(self.calls), count)

    def test_unhealthy_image_does_not_publish_success(self):
        self.health = 'unhealthy'
        with self.assertRaisesRegex(RuntimeError, 'health'):
            deployment.deploy(SHA, '10')
        self.assertEqual(json.loads((self.root / 'deployment.json').read_text()), self.previous)
        self.assertTrue((self.root / 'deployment-in-progress.json').is_file())

    def test_rejects_unverified_image_or_wrong_service_before_backup(self):
        with self.assertRaisesRegex(RuntimeError, 'verified commit'):
            deployment.deploy('c' * 40, '10')
        self.labels['com.docker.compose.project'] = 'another-service'
        with self.assertRaisesRegex(RuntimeError, 'configured test'):
            deployment.deploy(SHA, '10')
        self.assertEqual(self.calls, [])

    def test_rejects_unsafe_identifiers(self):
        for sha, run_id in [('main', '10'), (SHA, '../state'), (SHA, '0')]:
            with self.subTest(sha=sha, run_id=run_id):
                with self.assertRaises(ValueError):
                    deployment.deploy(sha, run_id)
        self.assertEqual(self.calls, [])


if __name__ == '__main__':
    unittest.main()
