import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import launch
import session_capture as capture

class SessionTests(unittest.TestCase):
    def test_snapshot(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {'ROUTER_COLLECTOR_DATA_DIR': temp}):
            source = Path(temp) / 'session.json'
            raw = b'{"text":"full text"}'
            source.write_bytes(raw)
            saved = capture.snapshot(source, 'opencode', 'run1', 'session1')
            self.assertEqual(saved.read_bytes(), raw)
            event = json.loads(next((Path(temp) / 'events').glob('*.json')).read_text())
            self.assertEqual(event['run_id'], 'run1')
            self.assertEqual(event['payload']['sha256'], hashlib.sha256(raw).hexdigest())
            credentials = Path(temp) / 'auth.json'
            credentials.write_text('{}')
            with self.assertRaises(ValueError):
                capture.snapshot(credentials, 'opencode', 'r', 's')

    def test_credentials(self):
        for key in ['api_key', 'Authorization', 'x-api-key', 'ANTHROPIC_API_KEY', 'ZAI_API_KEY', 'ANTHROPIC_AUTH_TOKEN']:
            self.assertEqual(capture.sanitize({'nested': [{key: 'secret', 'text': 'full'}]})['nested'][0], {key: '[REDACTED]', 'text': 'full'})

    def test_opencode_preserves_config(self):
        env = {'OPENCODE_CONFIG_CONTENT': json.dumps({'model': 'zai-coding-plan/example', 'provider': {'zai-coding-plan': {'options': {'apiKey': 'secret'}}}})}
        self.assertEqual(launch.client_command('opencode', ['--help'], 8888, env), ['opencode', '--help'])
        config = json.loads(env['OPENCODE_CONFIG_CONTENT'])
        self.assertEqual(config['model'], 'zai-coding-plan/example')
        self.assertEqual(config['provider']['zai-coding-plan']['options'], {'apiKey': 'secret', 'baseURL': 'http://127.0.0.1:8888'})

    def test_claude_config(self):
        env = {'ANTHROPIC_AUTH_TOKEN': 'existing'}
        command = launch.client_command('claude', ['--continue'], 8888, env)
        self.assertEqual(env['ANTHROPIC_AUTH_TOKEN'], 'existing')
        self.assertIn('SessionEnd', json.loads(command[2])['hooks'])
        with self.assertRaises(ValueError):
            launch.client_command('claude', ['--settings=x'], 8888, env)
