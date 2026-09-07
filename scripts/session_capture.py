#!/usr/bin/env python3
"""Capture explicit client lifecycle evidence without reading credential stores."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import uuid

from router_collector.storage import StorageBudget, CompressedWriter

SECRET_KEYS = {'authorization', 'proxyauthorization', 'cookie', 'setcookie', 'apikey',
               'accesstoken', 'refreshtoken', 'idtoken', 'password', 'clientsecret',
               'xapikey', 'anthropicapikey', 'anthropicauthtoken', 'zaiapikey',
               'openaiapikey', 'glmapikey'}

def sanitize(value):
    if isinstance(value, dict):
        return {k: '[REDACTED]' if ''.join(c for c in k.lower() if c.isalnum()) in SECRET_KEYS
                else sanitize(v) for k, v in value.items()}
    if isinstance(value, list):
        return [sanitize(v) for v in value]
    return value

def root():
    p = Path(os.environ.get('ROUTER_COLLECTOR_DATA_DIR', '~/datasets/router-collector')).expanduser()
    p.mkdir(parents=True, exist_ok=True, mode=0o700)
    p.chmod(0o700)
    return p

def save_event(kind, payload, run_id=None):
    directory = root()/'events'
    directory.mkdir(exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    event = {'schema_version': 1, 'id': str(uuid.uuid4()), 'time': datetime.now(timezone.utc).isoformat(),
             'run_id': run_id or os.environ.get('ROUTER_COLLECTOR_RUN_ID'),
             'kind': kind, 'payload': sanitize(payload)}
    dest = directory/(event['id']+'.json.gz')
    temp = dest.with_suffix('.tmp')
    writer = CompressedWriter(temp, StorageBudget(root()))
    try:
        for part in json.JSONEncoder(ensure_ascii=False).iterencode(event):
            writer.write(part.encode('utf-8'))
        writer.close()
        temp.replace(dest)
    except BaseException:
        writer.abort()
        temp.unlink(missing_ok=True)
        raise
    return event['id']

def snapshot(path, client, run_id, session_id):
    path = Path(path).expanduser()
    if path.is_symlink() or not path.is_file() or path.suffix not in {'.json', '.jsonl'}:
        raise ValueError('Choose a regular .json or .jsonl session export, not a symlink')
    if any(part.lower() in {'auth.json', 'credentials.json', '.credentials.json'} for part in path.parts):
        raise ValueError('Credential files must not be imported')
    directory = root()/'attachments'
    directory.mkdir(exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    # Hash first: identical transcripts reuse one compressed file without rewriting it.
    digest = hashlib.sha256()
    size = 0
    with path.open('rb') as src:
        while chunk := src.read(1024*1024):
            digest.update(chunk)
            size += len(chunk)
    dest = directory/(digest.hexdigest()+path.suffix+'.gz')
    if not dest.exists():
        temp = directory/(str(uuid.uuid4())+'.tmp')
        writer = CompressedWriter(temp, StorageBudget(root()))
        copied = hashlib.sha256()
        try:
            with path.open('rb') as src:
                while chunk := src.read(1024*1024):
                    copied.update(chunk)
                    writer.write(chunk)
            writer.close()
            if copied.digest() != digest.digest():
                raise ValueError('Session changed during import; retry after exiting the client')
            temp.replace(dest)
        except BaseException:
            writer.abort()
            temp.unlink(missing_ok=True)
            raise
    save_event('session_snapshot', {'client': client, 'session_id': session_id,
               'attachment': 'attachments/'+dest.name, 'sha256': digest.hexdigest(),
               'bytes': size, 'stored_bytes': dest.stat().st_size,
               'storage_encoding': 'gzip', 'outcome': 'unknown'}, run_id)
    return dest

def main():
    os.umask(0o077)
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='command', required=True)
    sub.add_parser('claude-hook')
    imp = sub.add_parser('import-session')
    imp.add_argument('file')
    imp.add_argument('--client', choices=['claude', 'opencode'], required=True)
    imp.add_argument('--run-id', required=True)
    imp.add_argument('--session-id', required=True)
    args = p.parse_args()
    if args.command == 'claude-hook':
        try:
            # Hook evidence can include final tools and task boundaries absent from API logs.
            value = json.load(sys.stdin)
            save_event('claude_hook', value)
            if value.get('hook_event_name') in {'SessionEnd', 'SubagentStop'}:
                file = value.get('agent_transcript_path') or value.get('transcript_path')
                if file:
                    resolved = Path(file).expanduser().resolve()
                    allowed = (Path.home()/'.claude/projects').resolve()
                    if resolved.is_relative_to(allowed):
                        snapshot(resolved, 'claude', os.environ.get('ROUTER_COLLECTOR_RUN_ID'), value.get('session_id'))
        except Exception as exc:
            # A collection failure must not block work or expose payloads in stderr.
            print('Collector hook failed: '+type(exc).__name__, file=sys.stderr)
    else:
        print(snapshot(args.file, args.client, args.run_id, args.session_id))

if __name__ == '__main__':
    main()
