import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from router_collector import sync_runner


class SyncRunnerTests(unittest.TestCase):
    def test_config_enable_disable(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertEqual(sync_runner.load_config(root), {})
            sync_runner.configure(root, 'owner/private-data')
            self.assertEqual(sync_runner.load_config(root), {'enabled': True, 'repo_id': 'owner/private-data'})
            self.assertEqual((Path(root) / '.sync/config.json').stat().st_mode & 0o777, 0o600)
            sync_runner.disable(root)
            self.assertFalse(sync_runner.load_config(root)['enabled'])

    def test_refuse_config_symlink(self):
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / '.sync').symlink_to(Path(root))
            with self.assertRaises(ValueError):
                sync_runner.configure(root, 'owner/data')

    def test_once_uses_configured_repo(self):
        with tempfile.TemporaryDirectory() as root:
            sync_runner.configure(root, 'owner/data')
            with patch('router_collector.sync.sync_once', return_value={'uploaded': 3, 'pending': 0}) as upload:
                self.assertEqual(sync_runner.run(root), 0)
                upload.assert_called_once_with(root, 'owner/data')

    def test_disabled_does_not_upload(self):
        with tempfile.TemporaryDirectory() as root, patch('router_collector.sync.sync_once') as upload:
            self.assertEqual(sync_runner.run(root, watch=True), 0)
            upload.assert_not_called()

    def test_watch_retries_then_honors_disable(self):
        with tempfile.TemporaryDirectory() as root:
            sync_runner.configure(root, 'owner/data')
            sleeps = []
            def sleep(seconds):
                sleeps.append(seconds)
                if len(sleeps) == 2:
                    sync_runner.disable(root)
            with patch('router_collector.sync.sync_once', side_effect=[OSError('secret token'), {'uploaded': 1, 'pending': 0}]) as upload, patch.object(sync_runner.time, 'sleep', side_effect=sleep), patch('sys.stderr') as stderr:
                self.assertEqual(sync_runner.run(root, watch=True), 0)
                self.assertEqual(upload.call_count, 2)
                self.assertEqual(sleeps, [300, 300])
                self.assertNotIn('secret token', str(stderr.write.call_args_list))

    def test_worker_environment_reduces_cache_writes(self):
        env = sync_runner.worker_environment({'HF_TOKEN': 'existing'})
        self.assertEqual(env['HF_TOKEN'], 'existing')
        self.assertEqual(env['HF_HUB_DISABLE_XET'], '1')
