"""Bounded, active-reader ZIP buffers. Listing archives never populates this pool."""
from __future__ import annotations

import io
import os
import threading


class ArchiveMemoryBudget:
    def __init__(self, limit_bytes: int = 256 * 1024**2):
        self.limit_bytes = limit_bytes
        self.used_bytes = 0
        self._lock = threading.Lock()

    def load(self, path: str):
        # Reserve before allocating, including concurrent readers. Large books
        # continue through the existing streaming backend without partial copies.
        if self.limit_bytes <= 0:
            return None
        with open(path, 'rb') as source:
            before = os.fstat(source.fileno())
            size = before.st_size
            with self._lock:
                if size <= 0 or size > self.limit_bytes - self.used_bytes:
                    return None
                self.used_bytes += size
            buffer = None
            try:
                buffer = io.BytesIO()
                remaining = size
                while remaining:
                    chunk = source.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise OSError('Archive changed while loading')
                    buffer.write(chunk)
                    remaining -= len(chunk)
                after = os.fstat(source.fileno())
                if (after.st_size, after.st_mtime_ns) != (size, before.st_mtime_ns):
                    raise OSError('Archive changed while loading')
                buffer.seek(0)
                return buffer, size
            except BaseException:
                if buffer is not None:
                    buffer.close()
                self.release(size)
                raise

    def release(self, size: int):
        with self._lock:
            self.used_bytes -= size
