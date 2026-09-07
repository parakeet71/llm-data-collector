"""Bounded, compressed writes. Limits are approximate across separate processes."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import time
import zlib

DEFAULT_MAX_BYTES = 5 * 1024**3
DEFAULT_MIN_FREE_BYTES = 1024**3
BUFFER_BYTES = 256 * 1024


class StorageLimit(OSError):
    pass


def usage_bytes(root):
    total = 0
    for directory, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [d for d in dirs if not (Path(directory) / d).is_symlink()]
        for name in files:
            path = Path(directory) / name
            try:
                if not path.is_symlink():
                    total += path.stat().st_size
            except FileNotFoundError:
                pass
    return total


class StorageBudget:
    def __init__(self, root, max_bytes=None, min_free_bytes=None):
        self.root = Path(root)
        self.max_bytes = int(os.environ.get("ROUTER_COLLECTOR_MAX_BYTES", DEFAULT_MAX_BYTES) if max_bytes is None else max_bytes)
        self.min_free_bytes = int(os.environ.get("ROUTER_COLLECTOR_MIN_FREE_BYTES", DEFAULT_MIN_FREE_BYTES) if min_free_bytes is None else min_free_bytes)
        if self.max_bytes < 0 or self.min_free_bytes < 0:
            raise ValueError('Storage limits must be non-negative')
        self.used = usage_bytes(self.root)
        self.disk_root = self.root
        while not self.disk_root.exists():
            self.disk_root = self.disk_root.parent
        self.free = shutil.disk_usage(self.disk_root).free
        self.checked = time.monotonic()
        self.paused = False

    def check(self):
        # Pause is sticky for this process; restart after making space.
        if self.paused:
            raise StorageLimit('Recording paused: storage limit reached')
        if time.monotonic() - self.checked >= 30:
            self.used = usage_bytes(self.root)
            self.free = shutil.disk_usage(self.disk_root).free
            self.checked = time.monotonic()
        if self.used >= self.max_bytes or self.free <= self.min_free_bytes:
            self.paused = True
            raise StorageLimit('Recording paused: storage limit reached')

    def reserve(self, size):
        self.check()
        if self.used + size > self.max_bytes or self.free - size < self.min_free_bytes:
            self.paused = True
            raise StorageLimit('Recording paused: storage limit reached')
        self.used += size
        self.free -= size

    def status(self):
        try:
            self.check()
        except StorageLimit:
            pass
        return {'used_bytes': self.used, 'max_bytes': self.max_bytes,
                'min_free_bytes': self.min_free_bytes,
                'recording': 'paused' if self.paused else 'active'}


class CompressedWriter:
    """Compress before disk writes, holding at most roughly one output buffer."""
    def __init__(self, path, budget):
        self.path = Path(path)
        self.budget = budget
        budget.check()
        self.file = self.path.open('xb', buffering=0)
        os.chmod(self.path, 0o600)
        self.compressor = zlib.compressobj(1, zlib.DEFLATED, 31)
        self.pending = bytearray()
        self.closed = False
        self.stored_bytes = 0

    def _emit(self, data):
        self.pending.extend(data)
        while len(self.pending) >= BUFFER_BYTES:
            self._flush(BUFFER_BYTES)

    def _flush(self, size):
        self.budget.reserve(size)
        view = memoryview(self.pending)[:size]
        try:
            offset = 0
            while offset < size:
                written = self.file.write(view[offset:])
                if not written:
                    raise OSError("Recording write made no progress")
                offset += written
        finally:
            view.release()
        del self.pending[:size]
        self.stored_bytes += size

    def write(self, data):
        if self.closed:
            raise ValueError('Write to closed recording')
        # Large callers cannot force an unbounded compressed output allocation.
        for start in range(0, len(data), 65536):
            self._emit(self.compressor.compress(data[start:start + 65536]))
        return len(data)

    def close(self):
        if not self.closed:
            try:
                self._emit(self.compressor.flush())
                if self.pending:
                    self._flush(len(self.pending))
            finally:
                self.file.close()
                self.closed = True

    def abort(self):
        # Cleanup must never turn a recording failure into an upstream failure.
        try:
            self.file.close()
        except OSError:
            pass
        self.closed = True
        self.pending.clear()
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            pass

    def __enter__(self):
        return self

    def __exit__(self, kind, value, traceback):
        if kind is not None:
            self.abort()
        else:
            try:
                self.close()
            except BaseException:
                self.abort()
                raise


def open_compressed(path, budget):
    return CompressedWriter(path, budget)
