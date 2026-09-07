"""Incremental private Hub uploads of finalized collector files only."""
from __future__ import annotations

from contextlib import ExitStack
import fcntl
import os
from pathlib import Path
import re
import sqlite3
import stat

UUID = r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'
MAX_BATCH_BYTES = 64 * 1024**2


def _safe(path, *, directory=False):
    """Refuse links in the file and every ancestor, including the data root."""
    for parent in (path, *path.parents):
        if parent.is_symlink():
            return False
    try:
        mode = path.stat().st_mode
        return stat.S_ISDIR(mode) if directory else stat.S_ISREG(mode)
    except FileNotFoundError:
        return False


def _groups(root):
    records = root / 'records'
    if _safe(records, directory=True):
        for folder in sorted(records.iterdir()):
            if not re.fullmatch(UUID, folder.name) or not _safe(folder, directory=True):
                continue
            files = [folder / 'metadata.json']
            for body in ('request', 'response'):
                candidates = [folder / (body + suffix) for suffix in ('.bin', '.bin.gz')]
                files.extend(p for p in candidates if _safe(p))
            # A finalized record contains metadata and both body files. Partial API
            # responses are still valid finalized captures; active folders never are.
            if (_safe(files[0]) and any(p.name.startswith('request.') for p in files)
                    and any(p.name.startswith('response.') for p in files)):
                yield files
    for category in ('events', 'outcomes', 'attachments'):
        folder = root / category
        if not _safe(folder, directory=True):
            continue
        stem = f'(?:{UUID}|[0-9a-f]{{64}})' if category == 'attachments' else UUID
        extension = r'\.jsonl?(?:\.gz)?' if category == 'attachments' else r'\.json(?:\.gz)?'
        for path in sorted(folder.iterdir()):
            if re.fullmatch(stem + extension, path.name) and _safe(path):
                yield [path]


def _signature(info):
    return ':'.join(str(v) for v in (info.st_dev, info.st_ino, info.st_size,
                                    info.st_mtime_ns, info.st_ctime_ns))


def sync_once(root, repo_id, *, api=None, max_files=90):
    """Upload at most one batch; never create a repository or send to a public one.

    Returns uploaded (file count), uploaded_bytes, pending (file count), and busy.
    Existing Hub login or HF_TOKEN supplies credentials; none are stored here.
    """
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*', repo_id):
        raise ValueError('Use an owner/name dataset repository ID')
    if not 5 <= max_files <= 90:
        raise ValueError('max_files must be between 5 and 90')
    root = Path(os.path.abspath(Path(root).expanduser()))
    if not _safe(root, directory=True):
        raise ValueError('Data directory must exist without symlink ancestors')
    state = root / '.sync'
    if state.is_symlink():
        raise ValueError('Sync state cannot be a symlink')
    state.mkdir(mode=0o700, exist_ok=True)
    state.chmod(0o700)
    with ExitStack() as stack:
        lock = stack.enter_context(os.fdopen(os.open(state / 'lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600), 'rb+'))
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return dict(uploaded=0, uploaded_bytes=0, pending=None, busy=True)
        ledger = state / 'uploads.sqlite3'
        for suffix in ('', '-journal', '-wal', '-shm'):
            if Path(str(ledger) + suffix).is_symlink():
                raise ValueError('Sync ledger cannot be a symlink')
        db = sqlite3.connect(ledger)
        stack.callback(db.close)
        ledger.chmod(0o600)
        db.execute('CREATE TABLE IF NOT EXISTS uploads (repo TEXT, path TEXT, signature TEXT, PRIMARY KEY (repo,path))')
        known = dict(db.execute('SELECT path,signature FROM uploads WHERE repo=?', (repo_id,)))
        selected, pending, byte_count = [], 0, 0
        for group in _groups(root):
            entries = [(p, p.relative_to(root).as_posix(), p.stat()) for p in group]
            if all(known.get(rel) == _signature(info) for _, rel, info in entries):
                continue
            # Reupload a changed record as one atomic group, including metadata.
            pending += len(entries)
            size = sum(info.st_size for _, _, info in entries)
            if len(selected) + len(entries) <= max_files and (not selected or byte_count + size <= MAX_BATCH_BYTES):
                selected.extend(entries)
                byte_count += size
        if not selected:
            return dict(uploaded=0, uploaded_bytes=0, pending=pending, busy=False)
        os.environ['HF_HUB_DISABLE_XET'] = '1'
        from huggingface_hub import HfApi, CommitOperationAdd
        if api is None:
            api = HfApi(endpoint='https://huggingface.co')
        if api.repo_info(repo_id=repo_id, repo_type='dataset').private is not True:
            raise ValueError('Uploads require a private Hugging Face dataset repository')
        operations = []
        for path, relative, before in selected:
            if not _safe(path):
                raise ValueError('Recording changed during upload preparation')
            handle = stack.enter_context(os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), 'rb'))
            if _signature(os.fstat(handle.fileno())) != _signature(before):
                raise ValueError('Recording changed during upload preparation')
            operations.append(CommitOperationAdd(path_in_repo=relative, path_or_fileobj=handle))
        api.create_commit(repo_id=repo_id, repo_type='dataset', operations=operations,
                          commit_message='Add completed collector recordings', num_threads=1)
        # If a file changed during upload, do not acknowledge it: retry next time.
        acknowledged = []
        for path, relative, before in selected:
            if _safe(path) and _signature(path.stat()) == _signature(before):
                acknowledged.append((repo_id, relative, _signature(before)))
        with db:
            db.executemany('INSERT OR REPLACE INTO uploads VALUES (?,?,?)', acknowledged)
        return dict(uploaded=len(selected), uploaded_bytes=byte_count,
                    pending=pending - len(acknowledged), busy=False)
