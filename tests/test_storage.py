import gzip
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from router_collector.storage import CompressedWriter, StorageBudget, StorageLimit


class StorageTests(unittest.TestCase):
    def test_compression_exact_bounded_disk_output(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'body.gz'
            budget = StorageBudget(root, max_bytes=100000, min_free_bytes=0)
            payload = b'conversation text ' * 100000
            with CompressedWriter(path, budget) as out:
                for start in range(0, len(payload), 1000):
                    out.write(payload[start:start + 1000])
            self.assertEqual(gzip.decompress(path.read_bytes()), payload)
            self.assertLess(path.stat().st_size, len(payload) / 50)
            self.assertEqual(budget.used, path.stat().st_size)

    def test_limit_before_write_deletes_partial(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'body.gz'
            budget = StorageBudget(root, max_bytes=1, min_free_bytes=0)
            with self.assertRaises(StorageLimit):
                with CompressedWriter(path, budget) as out:
                    out.write(b'hello')
            self.assertFalse(path.exists())
            self.assertEqual(budget.used, 0)
            self.assertTrue(budget.paused)

    def test_existing_data_and_environment_limits(self):
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / 'existing').write_bytes(b'x' * 10)
            with patch.dict('os.environ', {'ROUTER_COLLECTOR_MAX_BYTES': '10', 'ROUTER_COLLECTOR_MIN_FREE_BYTES': '0'}):
                with self.assertRaises(StorageLimit):
                    StorageBudget(root).check()

    def test_free_space_floor(self):
        with tempfile.TemporaryDirectory() as root:
            budget = StorageBudget(root, min_free_bytes=2**63)
            with self.assertRaises(StorageLimit):
                budget.check()

    def test_missing_root_and_initial_paused_status(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'missing' / 'nested'
            budget = StorageBudget(path, max_bytes=0, min_free_bytes=0)
            self.assertEqual(budget.status()['recording'], 'paused')
            self.assertFalse(path.exists())

    def test_short_writes(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'body.gz'
            out = CompressedWriter(path, StorageBudget(root, min_free_bytes=0))
            actual = out.file
            class ShortWriter:
                def write(self, data):
                    return actual.write(data[:3])
                def close(self):
                    actual.close()
            out.file = ShortWriter()
            out.write(b'hello world' * 100)
            out.close()
            self.assertEqual(gzip.decompress(path.read_bytes()), b'hello world' * 100)
