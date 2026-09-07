"""Explicit opt-in configuration and an independent, retrying upload worker."""
import json
import os
from pathlib import Path
import re
import sys
import time

from .core import private_dir, write_json


def config_path(root):
    root = Path(root).expanduser()
    if (root / '.sync').is_symlink():
        raise ValueError('Upload state directory cannot be a symlink')
    path = root / '.sync' / 'config.json'
    if path.is_symlink():
        raise ValueError('Upload configuration cannot be a symlink')
    return path


def validate_repo(repo):
    if not isinstance(repo, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*', repo):
        raise ValueError('Use a dataset ID such as username/dataset-name')
    return repo


def load_config(root):
    path = config_path(root)
    if not path.exists():
        return {}
    value = json.loads(path.read_text())
    if not isinstance(value, dict) or not isinstance(value.get('enabled'), bool):
        raise ValueError('Invalid upload configuration')
    if value.get('enabled'):
        validate_repo(value.get('repo_id'))
    return value


def configure(root, repo):
    repo = validate_repo(repo)
    path = config_path(root)
    private_dir(path.parent)
    write_json(path, {'enabled': True, 'repo_id': repo})
    return {'enabled': True, 'repo_id': repo}


def disable(root):
    path = config_path(root)
    private_dir(path.parent)
    write_json(path, {'enabled': False})


def worker_environment(env=None):
    value = dict(os.environ if env is None else env)
    # Direct HTTP/LFS uploads avoid a separate local Xet shard/chunk cache.
    value['HF_HUB_DISABLE_XET'] = '1'
    value['HF_HUB_DISABLE_PROGRESS_BARS'] = '1'
    return value


def run(root, repo=None, watch=False, interval=300):
    if interval < 10:
        raise ValueError('Upload interval must be at least 10 seconds')
    os.environ.update({key: value for key, value in worker_environment().items()
                       if key in {'HF_HUB_DISABLE_XET', 'HF_HUB_DISABLE_PROGRESS_BARS'}})
    from .sync import sync_once
    explicit_repo = validate_repo(repo) if repo else None
    failed = False
    while True:
        try:
            config = load_config(root)
            # Explicit --repo enables this invocation; configured workers honor disable.
            target = explicit_repo or (config.get('repo_id') if config.get('enabled') else None)
            if not target:
                if not watch:
                    print('Uploads disabled. Run sync-setup --repo OWNER/DATASET first.', file=sys.stderr)
                    return 1
                return 0
            result = sync_once(root, target)
            if result.get('uploaded'):
                print(json.dumps({'upload': result}), flush=True)
            elif not watch:
                print(json.dumps({'upload': result}), flush=True)
            if failed:
                print('Remote upload connection recovered.', file=sys.stderr, flush=True)
            failed = False
            if not watch:
                return 0
            delay = 1 if result.get('pending', 0) else interval
        except KeyboardInterrupt:
            return 130
        except Exception as exc:
            # Never emit SDK exception strings: they may contain credentials/URLs.
            if not failed or not watch:
                print('Remote upload failed (' + type(exc).__name__ +
                      '). Check login, private dataset access, and connection; local recordings remain.',
                      file=sys.stderr, flush=True)
            if not watch:
                return 1
            failed = True
            delay = interval
        time.sleep(delay)
