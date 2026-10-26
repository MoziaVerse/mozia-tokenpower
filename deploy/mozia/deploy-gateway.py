#!/usr/bin/env python3
"""Restricted SSH entry point for the two Mozia application deployments.

Install this reviewed file root-owned at /opt/mozia/tools/deploy-gateway.py.
Each authorized_keys entry fixes the service argument and disables SSH forwarding.
No shell, arbitrary image name, arbitrary revision, or database restore is exposed.
"""

import datetime
import fcntl
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time

ROOT = Path('/opt/mozia')
STACK = ROOT / 'mozia-newapi/deploy/mozia/compose.yaml'
PINS = ROOT / 'secrets/images.override.json'
SERVICES = {
    'tokenpower': ('newapi', 'mozia-newapi', 'mozia-tokenpower'),
    'sub2api': ('sub2api', 'mozia-sub2api', 'mozia-sub2api'),
}


def run(args, *, timeout=180, **kwargs):
    # Never forward command stderr: Compose and registry errors may contain secrets.
    result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            timeout=timeout, check=False, **kwargs)
    if result.returncode:
        raise RuntimeError(f'{args[0]} operation failed (exit {result.returncode})')
    return result.stdout


def inspect(name, kind='container'):
    return json.loads(run(['docker', kind, 'inspect', name]))[0]


def atomic_json(path, value):
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as out:
        temp = Path(out.name)
        try:
            os.chmod(temp, 0o600)
            json.dump(value, out)
            out.flush()
            os.fsync(out.fileno())
        except BaseException:
            temp.unlink(missing_ok=True)
            raise
    os.replace(temp, path)


def parse_command(command):
    parts = shlex.split(command)
    if parts == ['status']:
        return 'status', None
    if len(parts) == 2 and parts[0] == 'deploy' and re.fullmatch(r'[0-9a-f]{40}', parts[1]):
        return 'deploy', parts[1]
    raise ValueError('Only status or deploy <40-character commit SHA> is allowed')


def validate_payload(payload):
    if not re.fullmatch(r'sha256:[0-9a-f]{64}', payload.get('digest', '')):
        raise ValueError('Invalid image digest')
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}', payload.get('username', '')):
        raise ValueError('Invalid registry username')
    if not 1 <= len(payload.get('token', '')) <= 10000:
        raise ValueError('Invalid registry credential')
    return payload


def compose(config):
    return ['docker', 'compose', '--project-name', 'mozia', '-f', str(config)]


def start_service(config, service):
    run(compose(config) + ['up', '-d', '--no-deps', '--no-build', '--pull', 'never',
                          '--wait', '--wait-timeout', '240', service], timeout=300)


def check_health(service, expected_image):
    current = inspect(f'mozia-{service}-1')
    if (current['Image'] != expected_image or
            current['State'].get('Health', {}).get('Status') != 'healthy'):
        raise RuntimeError('Running image or container health did not match')
    # Verify the gateway can still reach its upstream on the internal network.
    run(['docker', 'exec', 'mozia-newapi-1', 'wget', '-q', '-T', '10', '-O',
         '/dev/null', 'http://sub2api:8080/health'], timeout=20)


def emit(message):
    try:
        print(message, flush=True)
    except BrokenPipeError:
        pass


def deploy(key, revision, payload):
    service, checkout, repository = SERVICES[key]
    repo = ROOT / checkout
    git = ['git', '-C', str(repo)]
    if run(git + ['status', '--porcelain']).strip():
        raise RuntimeError('Server checkout has local changes; deployment stopped')
    if run(git + ['branch', '--show-current']).strip() != b'mozia':
        raise RuntimeError('Server checkout must be on mozia')
    run(git + ['fetch', '--no-tags', 'origin', 'refs/heads/mozia:refs/remotes/origin/mozia'])
    tip = run(git + ['rev-parse', 'origin/mozia']).decode().strip()
    if tip != revision:
        emit('Skipped: a newer mozia commit is already available; this run is stale.')
        return
    run(git + ['merge-base', '--is-ancestor', 'HEAD', revision])

    if shutil.disk_usage(ROOT).free < 2 * 1024**3:
        raise RuntimeError('Less than 2 GiB free disk space; deployment stopped')
    image = f'ghcr.io/moziaverse/{repository}@{payload["digest"]}'
    with tempfile.TemporaryDirectory(prefix='mozia-registry-') as directory:
        docker = ['docker', '--config', directory]
        run(docker + ['login', 'ghcr.io', '-u', payload['username'], '--password-stdin'],
            input=payload['token'].encode(), timeout=60)
        run(docker + ['pull', image], timeout=900)
    payload.clear()
    built = inspect(image, 'image')
    if (built['Architecture'] != 'arm64' or built['Os'] != 'linux' or
            built['Config'].get('Labels', {}).get('org.opencontainers.image.revision') != revision):
        raise RuntimeError('Image architecture or revision label did not match')

    current = {name: inspect(f'mozia-{name}-1') for name in ('newapi', 'sub2api')}
    previous = current[service]
    # Use Docker image IDs for rollback; a mutable tag can never select a newer image.
    source = ['docker', 'compose', '--env-file', str(ROOT / 'secrets/stack.env'), '-f', str(STACK)]
    if PINS.exists():
        source += ['-f', str(PINS)]
    config = json.loads(run(source + ['config', '--format', 'json']))
    for name, container in current.items():
        config['services'][name]['image'] = container['Image']
        config['services'][name].pop('build', None)

    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    backup = ROOT / 'backups' / f'{stamp}-{key}-{revision[:12]}'
    backup.mkdir(mode=0o700)
    old_config = backup / 'previous-compose.json'
    atomic_json(old_config, config)
    database = 'newapi' if service == 'newapi' else 'sub2api'
    dump = backup / f'{database}.dump'
    with dump.open('wb') as out:
        result = subprocess.run(['docker', 'exec', f'mozia-{service}-postgres-1',
                                 'pg_dump', '-U', database, '-d', database, '-Fc'],
                                stdout=out, stderr=subprocess.PIPE, timeout=300, check=False)
    if result.returncode or not dump.stat().st_size:
        raise RuntimeError('Database backup failed; application was not restarted')
    with dump.open('rb') as source_dump:
        run(['docker', 'exec', '-i', f'mozia-{service}-postgres-1', 'pg_restore', '--list'],
            stdin=source_dump)
    emit(f'Backup verified: {backup}')

    config['services'][service]['image'] = image
    candidate = backup / 'candidate-compose.json'
    atomic_json(candidate, config)
    run(compose(candidate) + ['config', '--quiet'])
    # Recheck after the pull/backup, before interrupting the running service.
    run(git + ['fetch', '--no-tags', 'origin', 'refs/heads/mozia:refs/remotes/origin/mozia'])
    if run(git + ['rev-parse', 'origin/mozia']).decode().strip() != revision:
        emit('Skipped before restart: a newer mozia commit is available.')
        return
    if run(git + ['status', '--porcelain']).strip():
        raise RuntimeError('Server checkout changed during preparation')
    switched = False
    try:
        switched = True
        start_service(candidate, service)
        check_health(service, built['Id'])
        run(git + ['merge', '--ff-only', '--no-stat', revision])
        pins = {'services': {name: {'image': data['Config']['Image']}
                             for name, data in current.items()}}
        pins['services'][service]['image'] = image
        atomic_json(backup / 'result.json', {
            'status': 'verified', 'service': key, 'revision': revision, 'image': image,
            'previous_image': previous['Image'], 'backup': str(backup),
        })
        # Commit the image pins last: a failed result write must leave old pins intact.
        atomic_json(PINS, pins)
        emit(f'Deployed {key} {revision}; container healthy and internal upstream reachable.')
    except BaseException:
        if switched:
            emit('Deployment failed; attempting to restore the previous application image.')
            try:
                start_service(old_config, service)
                check_health(service, previous['Image'])
                emit('Previous application image restored and healthy. Database was not restored.')
            except BaseException:
                emit('Automatic image recovery failed; operator attention is required. Backup retained.')
        raise


def main():
    os.umask(0o077)
    if len(sys.argv) != 2 or sys.argv[1] not in SERVICES:
        raise ValueError('Service must be fixed by the SSH authorized_keys entry')
    key = sys.argv[1]
    action, revision = parse_command(os.environ.get('SSH_ORIGINAL_COMMAND', ''))
    if action == 'status':
        service = SERVICES[key][0]
        container = inspect(f'mozia-{service}-1')
        emit(json.dumps({'service': key, 'image': container['Config']['Image'],
                         'health': container['State'].get('Health', {}).get('Status')}))
        return
    raw = sys.stdin.buffer.read(16385)
    if len(raw) > 16384:
        raise ValueError('Deployment request is too large')
    payload = validate_payload(json.loads(raw))
    with (ROOT / 'tools/deploy-gateway.lock').open('a') as lock:
        deadline = time.monotonic() + 1800
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RuntimeError('Timed out waiting for the shared deployment lock')
                time.sleep(2)
        deploy(key, revision, payload)


if __name__ == '__main__':
    def interrupted(signum, frame):
        raise RuntimeError('Deployment connection interrupted')
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, interrupted)
    try:
        main()
    except BaseException as error:
        # Do not echo arbitrary payloads, registry errors, or Compose secrets.
        detail = str(error) if isinstance(error, RuntimeError) else type(error).__name__
        emit(f'Deployment failed: {detail}; inspect server state before retrying.')
        sys.exit(1)
