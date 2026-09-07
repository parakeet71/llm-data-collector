import gzip
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
            self.assertEqual(gzip.decompress(saved.read_bytes()), raw)
            event = json.loads(gzip.decompress(next((Path(temp) / 'events').glob('*.json.gz')).read_bytes()))
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

    def test_identical_transcript_not_rewritten(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {'ROUTER_COLLECTOR_DATA_DIR': temp}):
            source = Path(temp) / 'session.jsonl'
            source.write_bytes(b'{"text":"repeated text"}\n' * 10000)
            first = capture.snapshot(source, 'claude', 'r', 's')
            before = first.stat().st_mtime_ns
            second = capture.snapshot(source, 'claude', 'r', 's')
            self.assertEqual(first, second)
            self.assertEqual(before, second.stat().st_mtime_ns)
            self.assertLess(first.stat().st_size, source.stat().st_size // 10)

    def test_event_storage_failure_does_not_block_launcher(self):
        with patch.object(launch, '_save_event', side_effect=OSError('full')):
            self.assertIsNone(launch.save_event('client_start', {}))

    def test_uploader_absent_without_opt_in(self):
        with tempfile.TemporaryDirectory() as root, patch.object(launch.subprocess, 'Popen') as process:
            self.assertIsNone(launch.start_uploader(root))
            process.assert_not_called()

    def test_upload_start_failure_keeps_launcher_alive(self):
        from router_collector.sync_runner import configure
        with tempfile.TemporaryDirectory() as root:
            configure(root, 'owner/private-data')
            with patch.object(launch.importlib.util, 'find_spec', return_value=True), patch.object(launch.subprocess, 'Popen', side_effect=OSError('unavailable')):
                self.assertIsNone(launch.start_uploader(root))

    def test_disabled_uploads_skip_final_batch(self):
        from unittest.mock import Mock
        from router_collector.sync_runner import disable
        with tempfile.TemporaryDirectory() as root:
            disable(root)
            worker = Mock()
            worker.poll.return_value = None
            with patch.object(launch.subprocess, 'Popen') as process:
                launch.stop_uploader(worker, root)
                worker.terminate.assert_called_once()
                process.assert_not_called()

    def test_malformed_upload_config_does_not_block_launcher(self):
        with tempfile.TemporaryDirectory() as root:
            state = Path(root) / '.sync'
            state.mkdir()
            for value in ['[]', 'null', '{"enabled":true,"repo_id":123}']:
                (state / 'config.json').write_text(value)
                self.assertIsNone(launch.start_uploader(root))
