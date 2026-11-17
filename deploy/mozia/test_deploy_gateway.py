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

    def exercise_deployment(self, *, key='tokenpower', revision_label=SHA, backup_status=0, health_failure=False, record_failure=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'backups').mkdir()
            (root / 'secrets').mkdir()
            old = {name: {'Image': 'sha256:old-' + name, 'Config': {'Image': 'old/' + name},
                          'State': {'Health': {'Status': 'healthy'}}}
                   for name in ('newapi', 'sub2api')}

            def inspect(name, kind='container'):
                if kind == 'image':
                    return {'Id': 'sha256:new', 'Architecture': 'amd64', 'Os': 'linux',
                            'Config': {'Labels': {'org.opencontainers.image.revision': revision_label}}}
                return {'mozia-tokenpower-1': old['newapi'], 'mozia-sub2api-1': old['sub2api']}[name]

            def run(args, **kwargs):
                if args[0] == 'git':
                    self.assertEqual(args[2], str(root / ('mozia-tokenpower' if key == 'tokenpower' else 'mozia-sub2api')))
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
            write_json = deploy.atomic_json

            def record(path, value):
                if record_failure and path.name == 'result.json':
                    raise OSError('simulated full filesystem')
                write_json(path, value)

            def start(config, service):
                starts.append((json.loads(Path(config).read_text())['services'][service]['image'], service))

            with patch.multiple(deploy, ROOT=root, PINS=root / 'secrets/images.override.json'), \
                    patch.object(deploy, 'run', side_effect=run), \
                    patch.object(deploy, 'inspect', side_effect=inspect), \
                    patch.object(deploy.subprocess, 'run', side_effect=dump), \
                    patch.object(deploy.shutil, 'disk_usage', return_value=SimpleNamespace(free=10 * 1024**3)), \
                    patch.object(deploy, 'emit'), \
                    patch.object(deploy, 'atomic_json', side_effect=record), \
                    patch.object(deploy, 'start_service', side_effect=start), \
                    patch.object(deploy, 'check_health', side_effect=[RuntimeError('unhealthy'), None]
                                 if health_failure else None):
                error = None
                try:
                    deploy.deploy(key, SHA, {'digest': DIGEST, 'username': 'user', 'token': 'ephemeral'})
                except Exception as failure:
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

    def test_sub2api_deploy_preserves_tokenpower_pin(self):
        starts, error, pins = self.exercise_deployment(key='sub2api')
        self.assertIsNone(error)
        self.assertEqual(starts, [('ghcr.io/moziaverse/mozia-sub2api@' + DIGEST, 'sub2api')])
        self.assertEqual(pins['services']['newapi']['image'], 'old/newapi')

    def test_both_health_checks_use_the_renamed_gateway(self):
        for service, container in [('newapi', 'mozia-tokenpower-1'), ('sub2api', 'mozia-sub2api-1')]:
            with self.subTest(service=service), \
                    patch.object(deploy, 'inspect', return_value={
                        'Image': 'expected', 'State': {'Health': {'Status': 'healthy'}}
                    }) as inspect, patch.object(deploy, 'run') as run:
                deploy.check_health(service, 'expected')
                inspect.assert_called_once_with(container)
                run.assert_called_once_with([
                    'docker', 'exec', 'mozia-tokenpower-1', 'wget', '-q', '-T', '10',
                    '-O', '/dev/null', 'http://sub2api:8080/health'
                ], timeout=20)

    def test_result_write_failure_restores_image_without_committing_new_pins(self):
        starts, error, pins = self.exercise_deployment(record_failure=True)
        self.assertIsInstance(error, OSError)
        self.assertEqual(starts[-1], ('sha256:old-newapi', 'newapi'))
        self.assertIsNone(pins)


if __name__ == '__main__':
    unittest.main()
