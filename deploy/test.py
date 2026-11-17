#!/usr/bin/env python3
"""Update only the existing Mozia test gateway; retain its Compose configuration and storage."""
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

ROOT = Path('/srv/mozia-newapi-test')
CONTAINER = 'mozia-newapi-test-newapi-1'
PROJECT = 'mozia-newapi-test'


def inspect(*args):
    return json.loads(subprocess.check_output(['docker', *args]))[0]


def write_record(path, value):
    pending = path.with_suffix(path.suffix + '.pending')
    with pending.open('w') as stream:
        json.dump(value, stream, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    pending.replace(path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def deploy(sha, run_id):
    if not re.fullmatch(r'[0-9a-f]{40}', sha) or not re.fullmatch(r'[1-9][0-9]*', run_id):
        raise ValueError('Expected full commit SHA and positive workflow run ID')
    os.umask(0o077)
    with (ROOT / '.deployment.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        marker = ROOT / 'deployment-in-progress.json'
        if marker.exists():
            raise RuntimeError('Previous deployment is incomplete; inspect the application and database before retrying')
        state_file = ROOT / 'deployment.json'
        previous = json.loads(state_file.read_text())
        if int(previous.get('runId', 0)) >= int(run_id):
            print('An equal or newer deployment has completed; skipped')
            return
        existing = inspect('inspect', CONTAINER)
        labels = existing['Config'].get('Labels') or {}
        if (labels.get('com.docker.compose.project') != PROJECT
                or labels.get('com.docker.compose.service') != 'newapi'
                or labels.get('com.docker.compose.project.config_files', '').split(',')[0] != str(ROOT / 'compose.yml')):
            raise RuntimeError('Existing gateway does not match the configured test deployment')
        image = 'mozia-newapi:' + sha
        candidate = inspect('image', 'inspect', image)
        if candidate['Config'].get('Labels', {}).get('org.opencontainers.image.revision') != sha:
            raise RuntimeError('Candidate image does not match the verified commit')
        release = ROOT / 'releases' / sha
        release.mkdir(parents=True, exist_ok=True)
        override = release / 'compose.override.json'
        write_record(override, {'services': {'newapi': {'image': image, 'stop_grace_period': '60s'}}})
        compose = ['docker', 'compose', '--project-name', PROJECT, '-f', str(ROOT / 'compose.yml'), '-f', str(override)]
        subprocess.run(compose + ['config', '--quiet'], check=True)

        # A database migration may make an image-only rollback unsafe. Keep a private dump.
        backups = ROOT / 'backups'
        backups.mkdir(mode=0o700, exist_ok=True)
        backup = backups / (run_id + '-' + sha + '.dump')
        pending_backup = backup.with_suffix('.pending')
        with pending_backup.open('wb') as stream:
            result = subprocess.run(
                ['docker', 'exec', 'mozia-newapi-test-postgres-1', 'pg_dump', '-U', 'newapi', '-d', 'newapi', '-Fc'],
                stdout=stream, stderr=subprocess.PIPE,
            )
            stream.flush()
            os.fsync(stream.fileno())
        if result.returncode or pending_backup.stat().st_size == 0:
            pending_backup.unlink()
            raise RuntimeError('Database backup failed; application was not restarted')
        pending_backup.replace(backup)
        write_record(marker, {'commit': sha, 'runId': int(run_id), 'previousImage': existing['Config']['Image'], 'backup': backup.name})
        # Never recreate Postgres, Redis or any Matrix application service.
        subprocess.run(compose + ['up', '-d', '--no-deps', '--no-build', '--pull', 'never', '--wait', '--wait-timeout', '180', 'newapi'], check=True)
        running = inspect('inspect', CONTAINER)
        if running['Image'] != candidate['Id'] or running['State'].get('Health', {}).get('Status') != 'healthy':
            raise RuntimeError('New gateway image or health could not be confirmed; deployment remains pending')
        pending_link = ROOT / ('.current-' + run_id)
        pending_link.symlink_to(release)
        pending_link.replace(ROOT / 'current')
        write_record(state_file, {
            **previous, 'branch': 'mozia', 'commit': sha, 'version': 'git-' + sha[:12],
            'image': image, 'imageId': candidate['Id'], 'runId': int(run_id),
            'deployedAt': time.time(), 'backup': backup.name,
        })
        marker.unlink()
        print('Deployed verified mozia commit ' + sha)


if __name__ == '__main__':
    if len(sys.argv) != 3:
        raise SystemExit('Usage: test.py COMMIT_SHA WORKFLOW_RUN_ID')
    deploy(sys.argv[1], sys.argv[2])
