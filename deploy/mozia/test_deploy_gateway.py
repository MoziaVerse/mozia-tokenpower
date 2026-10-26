import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location('deploy_gateway', Path(__file__).with_name('deploy-gateway.py'))
deploy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy)
SHA = 'a' * 40
DIGEST = 'sha256:' + 'b' * 64


class DeploymentSafetyTests(unittest.TestCase):
    def test_ssh_command_allowlist(self):
        self.assertEqual(deploy.parse_command('deploy ' + SHA), ('deploy', SHA))
        self.assertEqual(deploy.parse_command('status'), ('status', None))
        for command in ('bash', 'deploy main', 'deploy ' + SHA + '; id',
                        'deploy ' + SHA + ' sub2api', 'status extra'):
            with self.subTest(command=command), self.assertRaises(ValueError):
                deploy.parse_command(command)

    def test_registry_payload_cannot_select_an_arbitrary_image(self):
        with self.assertRaises(ValueError):
            deploy.validate_payload({'digest': 'attacker/image:latest', 'username': 'user', 'token': 'x'})
        with self.assertRaises(ValueError):
            deploy.validate_payload({'digest': DIGEST, 'username': '-u other', 'token': 'x'})

    def exercise_deployment(self, *, revision_label=SHA, backup_status=0, health_failure=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'backups').mkdir()
            (root / 'secrets').mkdir()
            old = {name: {'Image': 'sha256:old-' + name, 'Config': {'Image': 'old/' + name},
                          'State': {'Health': {'Status': 'healthy'}}}
                   for name in ('newapi', 'sub2api')}

            def inspect(name, kind='container'):
                if kind == 'image':
                    return {'Id': 'sha256:new', 'Architecture': 'arm64', 'Os': 'linux',
                            'Config': {'Labels': {'org.opencontainers.image.revision': revision_label}}}
                return old['newapi' if name == 'mozia-newapi-1' else 'sub2api']

            def run(args, **kwargs):
                if args[0] == 'git':
                    if 'branch' in args:
                        return b'mozia'
                    if 'rev-parse' in args:
                        return SHA.encode()
                if '--format' in args:
                    return json.dumps({'services': {name: {'image': 'old/' + name, 'build': {}}
                                                     for name in old}}).encode()
                return b''

            def dump(args, **kwargs):
                self.assertIn('pg_dump', args)
                kwargs['stdout'].write(b'PGDMP-test')
                return SimpleNamespace(returncode=backup_status)

            starts = []

            def start(config, service):
                starts.append((json.loads(Path(config).read_text())['services'][service]['image'], service))

            with patch.multiple(deploy, ROOT=root, PINS=root / 'secrets/images.override.json'), \
                    patch.object(deploy, 'run', side_effect=run), \
                    patch.object(deploy, 'inspect', side_effect=inspect), \
                    patch.object(deploy.subprocess, 'run', side_effect=dump), \
                    patch.object(deploy.shutil, 'disk_usage', return_value=SimpleNamespace(free=10 * 1024**3)), \
                    patch.object(deploy, 'emit'), \
                    patch.object(deploy, 'start_service', side_effect=start), \
                    patch.object(deploy, 'check_health', side_effect=[RuntimeError('unhealthy'), None]
                                 if health_failure else None):
                error = None
                try:
                    deploy.deploy('tokenpower', SHA, {'digest': DIGEST, 'username': 'user', 'token': 'ephemeral'})
                except RuntimeError as failure:
                    error = failure
                pins = json.loads(deploy.PINS.read_text()) if deploy.PINS.exists() else None
                return starts, error, pins

    def test_wrong_revision_never_restarts_an_application(self):
        starts, error, pins = self.exercise_deployment(revision_label='c' * 40)
        self.assertIsNotNone(error)
        self.assertEqual(starts, [])
        self.assertIsNone(pins)

    def test_failed_database_backup_never_restarts_an_application(self):
        starts, error, pins = self.exercise_deployment(backup_status=1)
        self.assertIsNotNone(error)
        self.assertEqual(starts, [])
        self.assertIsNone(pins)

    def test_unhealthy_release_restores_previous_exact_image_without_changing_pins(self):
        starts, error, pins = self.exercise_deployment(health_failure=True)
        self.assertIsNotNone(error)
        self.assertEqual(starts[-1], ('sha256:old-newapi', 'newapi'))
        self.assertEqual(len(starts), 2)
        self.assertIsNone(pins)

    def test_success_keeps_the_other_service_pinned(self):
        starts, error, pins = self.exercise_deployment()
        self.assertIsNone(error)
        self.assertEqual(len(starts), 1)
        self.assertEqual(pins['services']['sub2api']['image'], 'old/sub2api')
        self.assertEqual(pins['services']['newapi']['image'],
                         'ghcr.io/moziaverse/mozia-tokenpower@' + DIGEST)


if __name__ == '__main__':
    unittest.main()
