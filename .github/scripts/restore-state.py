"""Restore compressed/legacy checkpoints, preferring newer recovery artifacts."""
import gzip
import io
import json
import os
from pathlib import Path
import subprocess
import zipfile

import requests


def git(*args):
    return subprocess.check_output(['git', *args])


def validate(raw):
    state = json.loads(raw)
    if not isinstance(state, dict) or not isinstance(state.get('boards'), dict):
        raise ValueError('Invalid checkpoint: missing boards')
    return raw


def restore():
    raw = None
    checkpoint_time = ''
    if git('ls-remote', 'origin', 'refs/heads/state').strip():
        git('fetch', 'origin', 'state', '--depth', '1')
        files = git('ls-tree', '--name-only', 'FETCH_HEAD').decode().splitlines()
        checkpoint_time = git('show', '-s', '--format=%cI', 'FETCH_HEAD').decode().strip()
        if 'state.json.gz' in files:
            raw = gzip.decompress(git('show', 'FETCH_HEAD:state.json.gz'))
        else:
            raw = git('show', 'FETCH_HEAD:state.json')
        validate(raw)
    repo = os.environ.get('GITHUB_REPOSITORY')
    token = os.environ.get('GH_TOKEN')
    if repo and token:
        # Successful artifact uploads may contain deliveries newer than the
        # last persisted branch. Recover them before sending anything again.
        session = requests.Session()
        session.headers.update(Authorization=f'Bearer {token}', Accept='application/vnd.github+json')
        response = session.get(f'https://api.github.com/repos/{repo}/actions/artifacts',
                               params={'name': 'jobwatch-state-recovery', 'per_page': 30}, timeout=30)
        response.raise_for_status()
        from datetime import datetime
        candidates = [a for a in response.json()['artifacts'] if not a['expired']
                      and (not checkpoint_time or datetime.fromisoformat(a['created_at'].replace('Z', '+00:00'))
                           > datetime.fromisoformat(checkpoint_time))]
        if candidates:
            artifact = max(candidates, key=lambda a: a['created_at'])
            archive = session.get(artifact['archive_download_url'], timeout=60)
            archive.raise_for_status()
            with zipfile.ZipFile(io.BytesIO(archive.content)) as bundle:
                raw = validate(bundle.read('state.json'))
            print(f"Recovered newer checkpoint from artifact {artifact['id']}")
    if raw is None:
        print('No prior checkpoint; first run will baseline silently')
        return
    target = Path('state.json')
    target.with_suffix('.restore.tmp').write_bytes(raw)
    target.with_suffix('.restore.tmp').replace(target)
    print(f'Restored validated checkpoint ({len(raw)/1024/1024:.1f} MiB)')


if __name__ == '__main__':
    restore()
