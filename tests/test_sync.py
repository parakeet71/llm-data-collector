import fcntl
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch
import uuid

from router_collector.sync import sync_once


class Operation:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class FakeHub:
    def __init__(self, private=True, fail=False):
        self.private, self.fail = private, fail
        self.calls = []
        self.checks = 0

    def repo_info(self, **kwargs):
        assert kwargs['repo_type'] == 'dataset'
        self.checks += 1
        return SimpleNamespace(private=self.private)

    def create_commit(self, **kwargs):
        assert kwargs['num_threads'] == 1
        data = {op.path_in_repo: op.path_or_fileobj.read() for op in kwargs['operations']}
        self.calls.append(data)
        if self.fail:
            raise RuntimeError('Offline simulated failure')


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.api = FakeHub()
        fake = SimpleNamespace(HfApi=lambda **kw: self.api, CommitOperationAdd=Operation)
        self.patch = patch.dict('sys.modules', {'huggingface_hub': fake})
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def file(self, relative, content=b'compressed bytes'):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def record(self):
        folder = 'records/' + str(uuid.uuid4())
        for name in ('metadata.json', 'request.bin.gz', 'response.bin.gz'):
            self.file(folder + '/' + name)
        return folder

    def sync(self, **kwargs):
        return sync_once(self.root, 'owner/private-data', api=self.api, **kwargs)

    def test_incremental_noop_and_changed_record(self):
        folder = self.record()
        self.assertEqual(self.sync()['uploaded'], 3)
        ledger = self.root / '.sync/uploads.sqlite3'
        before = ledger.stat().st_mtime_ns
        self.assertEqual(self.sync()['uploaded'], 0)
        self.assertEqual(before, ledger.stat().st_mtime_ns)
        self.assertEqual(self.api.checks, 1)
        self.file(folder + '/metadata.json', b'changed')
        self.assertEqual(self.sync()['uploaded'], 3)
        self.assertEqual(self.api.checks, 2)

    def test_public_refused_then_private_works(self):
        self.record()
        self.api.private = False
        with self.assertRaisesRegex(ValueError, 'private'):
            self.sync()
        self.assertEqual(self.api.calls, [])
        self.api.private = True
        self.assertEqual(self.sync()['uploaded'], 3)

    def test_failure_does_not_mark_uploaded(self):
        self.record()
        self.api.fail = True
        with self.assertRaises(RuntimeError):
            self.sync()
        self.api.fail = False
        self.assertEqual(self.sync()['uploaded'], 3)

    def test_only_allowed_final_files(self):
        folder = self.record()
        self.file(folder + '/auth.json')
        for name in ('active/' + str(uuid.uuid4()) + '/metadata.json', '.sync/config.json',
                     '.env', 'events/auth.json', 'attachments/credentials.json',
                     'events/' + str(uuid.uuid4()) + '.tmp'):
            self.file(name)
        self.file('records/' + str(uuid.uuid4()) + '/metadata.json')
        event = self.file('events/' + str(uuid.uuid4()) + '.json.gz')
        attach = self.file('attachments/' + 'a' * 64 + '.jsonl.gz')
        self.file('outcomes/' + str(uuid.uuid4()) + '.json')
        (self.root / 'events' / (str(uuid.uuid4()) + '.json')).symlink_to(event)
        self.assertEqual(self.sync()['uploaded'], 6)
        paths = self.api.calls[0]
        self.assertIn(attach.relative_to(self.root).as_posix(), paths)
        self.assertFalse(any('auth' in p or '.sync' in p for p in paths))

    def test_linked_category_and_root_refused(self):
        self.file('secrets/' + str(uuid.uuid4()) + '.json')
        (self.root / 'events').symlink_to(self.root / 'secrets')
        self.assertEqual(self.sync()['uploaded'], 0)
        (self.root / 'linked').symlink_to(self.root)
        with self.assertRaises(ValueError):
            sync_once(self.root / 'linked', 'owner/data', api=self.api)

    def test_atomic_batches_and_privacy_recheck(self):
        self.record()
        self.record()
        result = self.sync(max_files=5)
        self.assertEqual((result['uploaded'], result['pending']), (3, 3))
        self.api.private = False
        with self.assertRaises(ValueError):
            self.sync(max_files=5)
        self.api.private = True
        self.assertEqual(self.sync(max_files=5)['uploaded'], 3)
        self.assertEqual(self.sync()['pending'], 0)

    def test_lock_prevents_concurrent_sync(self):
        state = self.root / '.sync'
        state.mkdir()
        with (state / 'lock').open('wb') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertTrue(self.sync()['busy'])
        self.assertEqual(self.api.calls, [])

    def test_repo_has_independent_ledger(self):
        self.record()
        self.sync()
        result = sync_once(self.root, 'owner/other', api=self.api)
        self.assertEqual(result['uploaded'], 3)

    def test_oversized_single_group_allowed(self):
        self.record()
        self.record()
        with patch('router_collector.sync.MAX_BATCH_BYTES', 1):
            self.assertEqual(self.sync()['uploaded'], 3)

    def test_symlink_ledger_refused(self):
        self.file('.sync/other')
        (self.root / '.sync/uploads.sqlite3').symlink_to(self.root / '.sync/other')
        with self.assertRaises(ValueError):
            self.sync()


if __name__ == '__main__':
    unittest.main()
