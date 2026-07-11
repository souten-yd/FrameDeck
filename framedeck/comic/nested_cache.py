"""親アーカイブ内の子アーカイブを内容キーで永続キャッシュへ抽出する。

キャッシュキー: 親の絶対パス + 更新時刻 + サイズ + 子の内部パス。
LRU(DBのlast_used)と最大容量・最終使用期限で整理する。
"""
from __future__ import annotations

import hashlib
import os
import shutil
import threading
import time
from pathlib import Path

from ..core.storage import Storage
from .archive_backend import ArchiveReader


class NestedArchiveCache:
    def __init__(self, cache_dir: Path, storage: Storage,
                 max_bytes: int = 10 * 1024**3,
                 max_age_days: float = 30.0):
        self._dir = Path(cache_dir)
        self._storage = storage
        self._max_bytes = max_bytes
        self._max_age = max_age_days * 86400
        self._lock = threading.Lock()

    def _key(self, container_path: str, inner_name: str) -> str:
        stat = os.stat(container_path)
        raw = f"{os.path.abspath(container_path)}|{stat.st_mtime}|{stat.st_size}|{inner_name}"
        return hashlib.sha256(raw.encode("utf-8", "surrogatepass")).hexdigest()

    def get_extracted_path(self, container_path: str, inner_name: str) -> str:
        """子アーカイブを抽出済みファイルとして取得する(キャッシュ再利用)。"""
        key = self._key(container_path, inner_name)
        base = os.path.basename(inner_name.replace("\\", "/")) or "nested"
        if not os.path.splitext(base)[1]:
            base += os.path.splitext(container_path)[1] or ".zip"
        target_dir = self._dir / key
        target = target_dir / base

        with self._lock:
            if target.exists() and target.stat().st_size > 0:
                self._storage.touch_cache_entry(
                    f"nested:{key}", str(target_dir), target.stat().st_size
                )
                return str(target)

            with ArchiveReader(container_path) as parent:
                data = parent.read(inner_name)
            target_dir.mkdir(parents=True, exist_ok=True)
            tmp = target.with_suffix(target.suffix + ".part")
            tmp.write_bytes(data)
            tmp.replace(target)
            self._storage.touch_cache_entry(
                f"nested:{key}", str(target_dir), len(data)
            )
            return str(target)

    def prune(self) -> None:
        """容量・期限超過分を最終使用の古い順に削除する。"""
        entries = [
            e for e in self._storage.list_cache_entries()
            if e["key"].startswith("nested:")
        ]
        now = time.time()
        total = sum(e["size"] for e in entries)
        for entry in entries:  # last_used 昇順
            expired = (now - entry["last_used"]) > self._max_age
            if not expired and total <= self._max_bytes:
                continue
            shutil.rmtree(entry["path"], ignore_errors=True)
            self._storage.delete_cache_entry(entry["key"])
            total -= entry["size"]
