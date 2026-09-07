#!/usr/bin/env python3
"""Launch a recorded client session without changing persistent client settings."""
import argparse
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
import uuid

from session_capture import save_event

HOOKS = ['SessionStart', 'UserPromptSubmit', 'PreToolUse', 'PostToolUse',
         'PostToolUseFailure', 'Stop', 'SubagentStart', 'SubagentStop',
         'PreCompact', 'SessionEnd']

def client_command(client, extra, port, env):
    base = 'http://127.0.0.1:'+str(port)
    if client == 'claude':
        if any(a == '--settings' or a.startswith('--settings=') for a in extra):
            raise ValueError('Use ordinary Claude settings files; this launcher supplies --settings for recording hooks')
        command = shlex.join([sys.executable, str(Path(__file__).with_name('session_capture.py').resolve()), 'claude-hook'])
        config = {'hooks': {event: [{'hooks': [{'type': 'command', 'command': command, 'timeout': 15}]}]
                            for event in HOOKS}}
        env['ANTHROPIC_BASE_URL'] = base
        return ['claude', '--settings', json.dumps(config), *extra]
    config = json.loads(env.get('OPENCODE_CONFIG_CONTENT') or '{}')
    provider = config.setdefault('provider', {}).setdefault('zai-coding-plan', {})
    provider.setdefault('options', {})['baseURL'] = base
    env['OPENCODE_CONFIG_CONTENT'] = json.dumps(config)
    return ['opencode', *extra]

def main():
    os.umask(0o077)
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('client', choices=['claude', 'opencode'])
    p.add_argument('--port', type=int, default=8787)
    p.add_argument('--data-dir', default='~/datasets/router-collector')
    # Pass client arguments after -- to avoid consuming collector arguments.
    raw = sys.argv[1:]
    if '--' in raw:
        index = raw.index('--'); raw, extra = raw[:index], raw[index+1:]
    else:
        extra = []
    args = p.parse_args(raw)
    if not shutil.which(args.client):
        p.error(args.client+' is not installed or not on PATH')
    run_id = str(uuid.uuid4())
    data_dir = str(Path(args.data_dir).expanduser().resolve())
    env = os.environ.copy()
    env['ROUTER_COLLECTOR_RUN_ID'] = run_id
    env['ROUTER_COLLECTOR_DATA_DIR'] = data_dir
    os.environ.update({k: env[k] for k in ['ROUTER_COLLECTOR_RUN_ID', 'ROUTER_COLLECTOR_DATA_DIR']})
    try:
        command = client_command(args.client, extra, args.port, env)
    except (ValueError, TypeError, AttributeError):
        p.error('Invalid client configuration; check OPENCODE_CONFIG_CONTENT or --settings arguments')
    try:
        version = subprocess.run([args.client, '--version'], capture_output=True, text=True, timeout=10).stdout.strip()[:500]
    except (OSError, subprocess.TimeoutExpired):
        version = 'unavailable'
    proxy = subprocess.Popen([sys.executable, '-m', 'router_collector', '--data-dir', data_dir,
                              'serve', '--provider', 'anthropic' if args.client == 'claude' else 'glm',
                              '--port', str(args.port), '--run-id', run_id],
                             stdout=subprocess.DEVNULL, start_new_session=True)
    child = None
    try:
        ready = False
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for _ in range(100):
            if proxy.poll() is not None:
                break
            try:
                with opener.open(f'http://127.0.0.1:{args.port}/health', timeout=.2) as response:
                    if json.load(response).get('run_id') == run_id:
                        ready = True; break
            except Exception:
                pass
            time.sleep(.1)
        if not ready:
            raise RuntimeError('Collector did not start; check the port and data directory')
        save_event('client_start', {'client': args.client, 'client_version': version,
                   'cwd': str(Path.cwd()), 'provider': 'anthropic' if args.client == 'claude' else 'glm',
                   'outcome': 'unknown'})
        print(f'Recording locally. Run ID: {run_id}', flush=True)
        if args.client == 'opencode':
            print('Select a zai-coding-plan model. After exit, export this session for final tool/outcome evidence.', flush=True)
        child = subprocess.Popen(command, env=env)
        while child.poll() is None:
            if proxy.poll() is not None:
                print('Collector stopped: exit the client; requests can no longer pass through it.', file=sys.stderr)
                child.terminate()
                break
            time.sleep(.25)
        code = child.wait()
        save_event('client_exit', {'client': args.client, 'exit_code': code, 'outcome': 'unknown'})
        return code
    except KeyboardInterrupt:
        if child and child.poll() is None:
            try:
                child.wait(timeout=3)
            except subprocess.TimeoutExpired:
                child.terminate(); child.wait(timeout=5)
        return 130
    finally:
        proxy.send_signal(signal.SIGTERM) if proxy.poll() is None else None
        try:
            proxy.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proxy.kill(); proxy.wait()
        print(f'Recording ended. Run ID: {run_id}', flush=True)

if __name__ == '__main__':
    sys.exit(main())
