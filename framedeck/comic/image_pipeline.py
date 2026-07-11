"""画像パイプライン:

  decode → EXIF orientation → resize → encode

- 生データのメモリLRUキャッシュ
- 表示サイズ別のディスクキャッシュ
- ThreadPoolExecutorによる先読み(プリフェッチ)

UIスレッド・イベントループでの同期デコードを避けるための共有部品。
"""
from __future__ import annotations

import hashlib
import io
import os
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PIL import Image, ImageOps

from ..models import ComicEntry, PageRef

_RESAMPLE = {
    "lanczos": Image.Resampling.LANCZOS,
    "bicubic": Image.Resampling.BICUBIC,
    "bilinear": Image.Resampling.BILINEAR,
    "nearest": Image.Resampling.NEAREST,
}

_MIME_BY_EXT = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".webp": "image/webp", ".bmp": "image/bmp", ".gif": "image/gif",
}


class MemoryLRU:
    """バイト数上限つきのスレッドセーフLRU。"""

    def __init__(self, max_bytes: int):
        self._max = max_bytes
        self._lock = threading.Lock()
        self._data: OrderedDict[str, bytes] = OrderedDict()
        self._size = 0

    def get(self, key: str) -> bytes | None:
        with self._lock:
            value = self._data.get(key)
            if value is not None:
                self._data.move_to_end(key)
            return value

    def put(self, key: str, value: bytes) -> None:
        with self._lock:
            old = self._data.pop(key, None)
            if old is not None:
                self._size -= len(old)
            self._data[key] = value
            self._size += len(value)
            while self._size > self._max and self._data:
                _, evicted = self._data.popitem(last=False)
                self._size -= len(evicted)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()
            self._size = 0


class ImagePipeline:
    def __init__(self, page_cache_dir: Path, thumb_cache_dir: Path,
                 memory_limit_bytes: int = 512 * 1024**2,
                 resize_filter: str = "lanczos",
                 max_workers: int = 4):
        self._page_cache_dir = Path(page_cache_dir)
        self._thumb_cache_dir = Path(thumb_cache_dir)
        self._raw_cache = MemoryLRU(memory_limit_bytes)
        self._size_cache: dict[str, tuple[int, int]] = {}
        self._size_lock = threading.Lock()
        self._read_locks: dict[int, threading.Lock] = {}
        self._read_locks_lock = threading.Lock()
        self.resize_filter = resize_filter
        self.executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="framedeck-img"
        )

    def shutdown(self) -> None:
        self.executor.shutdown(wait=False, cancel_futures=True)

    # ---------------- 生データ ----------------

    def _source_lock(self, source) -> threading.Lock:
        key = id(source)
        with self._read_locks_lock:
            lock = self._read_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._read_locks[key] = lock
            return lock

    def release_source(self, source) -> None:
        with self._read_locks_lock:
            self._read_locks.pop(id(source), None)

    @staticmethod
    def _raw_key(entry: ComicEntry, page: PageRef) -> str:
        return f"raw:{entry.id}:{page.index}"

    def get_raw(self, source, entry: ComicEntry, page: PageRef) -> bytes:
        key = self._raw_key(entry, page)
        cached = self._raw_cache.get(key)
        if cached is not None:
            return cached
        with self._source_lock(source):
            cached = self._raw_cache.get(key)
            if cached is not None:
                return cached
            data = source.read_page(page)
        self._raw_cache.put(key, data)
        return data

    def get_page_size(self, source, entry: ComicEntry,
                      page: PageRef) -> tuple[int, int]:
        key = f"{entry.id}:{page.index}"
        with self._size_lock:
            if key in self._size_cache:
                return self._size_cache[key]
        data = self.get_raw(source, entry, page)
        with Image.open(io.BytesIO(data)) as img:
            size = img.size
            try:
                transposed = ImageOps.exif_transpose(img)
                if transposed is not None:
                    size = transposed.size
            except Exception:
                pass
        with self._size_lock:
            self._size_cache[key] = size
        return size

    def is_landscape(self, source, entry: ComicEntry, page: PageRef,
                     threshold: float = 1.25) -> bool:
        try:
            w, h = self.get_page_size(source, entry, page)
        except Exception:
            return False
        return h > 0 and (w / h) >= threshold

    # ---------------- プリフェッチ ----------------

    def prefetch(self, source, entry: ComicEntry, pages: list[PageRef],
                 center: int, ahead: int = 6, behind: int = 2) -> None:
        targets: list[PageRef] = []
        for offset in range(1, ahead + 1):
            idx = center + offset
            if 0 <= idx < len(pages):
                targets.append(pages[idx])
        for offset in range(1, behind + 1):
            idx = center - offset
            if 0 <= idx < len(pages):
                targets.append(pages[idx])
        for page in targets:
            if self._raw_cache.get(self._raw_key(entry, page)) is not None:
                continue
            self.executor.submit(self._prefetch_one, source, entry, page)

    def _prefetch_one(self, source, entry: ComicEntry, page: PageRef) -> None:
        try:
            self.get_raw(source, entry, page)
            self.get_page_size(source, entry, page)
        except Exception:
            pass

    # ---------------- エンコード済み画像(Web配信用) ----------------

    @staticmethod
    def mime_for_name(name: str) -> str:
        return _MIME_BY_EXT.get(os.path.splitext(name)[1].lower(),
                                "application/octet-stream")

    def _disk_key(self, entry: ComicEntry, page: PageRef,
                  width: int | None, height: int | None,
                  kind: str) -> str:
        try:
            mtime = os.path.getmtime(entry.physical_path)
        except OSError:
            mtime = 0
        raw = (f"{kind}|{entry.id}|{page.index}|{mtime}|"
               f"{width}|{height}|{self.resize_filter}")
        return hashlib.sha1(raw.encode()).hexdigest()

    def render_page(self, source, entry: ComicEntry, page: PageRef,
                    max_width: int | None = None,
                    max_height: int | None = None) -> tuple[bytes, str, str]:
        """ページ画像を返す。(bytes, mime, etag)

        リサイズ不要ならアーカイブ内の元データをそのまま返す。
        リサイズ時はディスクキャッシュを使う。
        """
        etag = self._disk_key(entry, page, max_width, max_height, "page")
        if not max_width and not max_height:
            data = self.get_raw(source, entry, page)
            return data, self.mime_for_name(page.name), etag

        cache_file = self._page_cache_dir / f"{etag}.jpg"
        if cache_file.exists():
            return cache_file.read_bytes(), "image/jpeg", etag

        data = self.get_raw(source, entry, page)
        with Image.open(io.BytesIO(data)) as img:
            img = ImageOps.exif_transpose(img) or img
            img = img.convert("RGB")
            ratio = min(
                (max_width or img.width) / img.width,
                (max_height or img.height) / img.height,
                1.0,
            )
            if ratio < 1.0:
                new_size = (max(1, int(img.width * ratio)),
                            max(1, int(img.height * ratio)))
                img = img.resize(
                    new_size, _RESAMPLE.get(self.resize_filter,
                                            Image.Resampling.LANCZOS)
                )
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=88)
        encoded = buf.getvalue()
        try:
            self._page_cache_dir.mkdir(parents=True, exist_ok=True)
            tmp = cache_file.with_suffix(".tmp")
            tmp.write_bytes(encoded)
            tmp.replace(cache_file)
        except OSError:
            pass
        return encoded, "image/jpeg", etag

    def render_thumbnail(self, source, entry: ComicEntry, page: PageRef,
                         size: int = 320) -> tuple[bytes, str, str]:
        etag = self._disk_key(entry, page, size, size, "thumb")
        cache_file = self._thumb_cache_dir / f"{etag}.jpg"
        if cache_file.exists():
            return cache_file.read_bytes(), "image/jpeg", etag
        data = self.get_raw(source, entry, page)
        with Image.open(io.BytesIO(data)) as img:
            img = ImageOps.exif_transpose(img) or img
            img = img.convert("RGB")
            img.thumbnail((size, size), Image.Resampling.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=80)
        encoded = buf.getvalue()
        try:
            self._thumb_cache_dir.mkdir(parents=True, exist_ok=True)
            tmp = cache_file.with_suffix(".tmp")
            tmp.write_bytes(encoded)
            tmp.replace(cache_file)
        except OSError:
            pass
        return encoded, "image/jpeg", etag
