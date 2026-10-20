"""SQLite永続化層。履歴・読書位置・動画位置・キャッシュ台帳を保存する。"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS library_roots (
    id TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'any',
    display_name TEXT,
    added_at REAL NOT NULL,
    UNIQUE(path, kind)
);
CREATE TABLE IF NOT EXISTS media_items (
    id TEXT PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,
    media_type TEXT NOT NULL,
    rating INTEGER,
    modified_at REAL,
    size INTEGER,
    last_seen REAL
);
CREATE TABLE IF NOT EXISTS comic_entries (
    id TEXT PRIMARY KEY,
    root_item_id TEXT,
    label TEXT,
    payload TEXT,
    updated_at REAL
);
CREATE TABLE IF NOT EXISTS reading_progress (
    entry_id TEXT PRIMARY KEY,
    page_index INTEGER NOT NULL,
    page_count INTEGER NOT NULL,
    updated_at REAL NOT NULL,
    completed INTEGER NOT NULL DEFAULT 0,
    reader_mode TEXT,
    reading_direction TEXT
);
CREATE TABLE IF NOT EXISTS video_progress (
    media_id TEXT PRIMARY KEY,
    position_seconds REAL NOT NULL,
    duration_seconds REAL NOT NULL,
    updated_at REAL NOT NULL,
    completed INTEGER NOT NULL DEFAULT 0,
    playback_speed REAL NOT NULL DEFAULT 1.0,
    audio_track INTEGER,
    subtitle_track INTEGER
);
CREATE TABLE IF NOT EXISTS favorites (
    media_id TEXT PRIMARY KEY,
    added_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS bookmarks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id TEXT NOT NULL,
    page_index INTEGER NOT NULL,
    note TEXT,
    added_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS recent_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    media_id TEXT NOT NULL,
    path TEXT NOT NULL,
    media_type TEXT NOT NULL,
    opened_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS cache_entries (
    key TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    size INTEGER NOT NULL DEFAULT 0,
    last_used REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS scan_state (
    root_id TEXT PRIMARY KEY,
    scanned_at REAL,
    item_count INTEGER
);
"""


class Storage:
    """スレッドセーフな薄いSQLiteラッパー。"""

    def __init__(self, db_path: str | Path):
        self._lock = threading.RLock()
        self.reading_flush_seconds = 0.0
        self._pending_reading: dict[str, tuple] = {}
        self._reading_timer = None
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._ensure_library_root_display_name_column()
            self._migrate_library_roots()
            self._ensure_library_root_display_name_column()
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self.flush_reading_progress()
            self._conn.close()

    def _execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cursor = self._conn.execute(sql, params)
            self._conn.commit()
            return cursor

    def _query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def _migrate_library_roots(self) -> None:
        """Allow the same path to be registered once per media kind."""
        indexes = self._conn.execute(
            "PRAGMA index_list(library_roots)"
        ).fetchall()
        has_path_only_unique = False
        for index in indexes:
            if not index["unique"]:
                continue
            cols = [
                row["name"]
                for row in self._conn.execute(
                    f"PRAGMA index_info({index['name']})"
                ).fetchall()
            ]
            if cols == ["path"]:
                has_path_only_unique = True
                break
        if not has_path_only_unique:
            return
        rows = self._conn.execute(
            "SELECT id,path,kind,display_name,added_at FROM library_roots ORDER BY added_at"
        ).fetchall()
        self._conn.execute("ALTER TABLE library_roots RENAME TO library_roots_old")
        self._conn.execute(
            "CREATE TABLE library_roots ("
            "id TEXT PRIMARY KEY, "
            "path TEXT NOT NULL, "
            "kind TEXT NOT NULL DEFAULT 'any', "
            "display_name TEXT, "
            "added_at REAL NOT NULL, "
            "UNIQUE(path, kind))"
        )
        for row in rows:
            self._conn.execute(
                "INSERT OR IGNORE INTO library_roots(id,path,kind,display_name,added_at) "
                "VALUES(?,?,?,?,?)",
                (row["id"], row["path"], row["kind"], row["display_name"] if "display_name" in row.keys() else None, row["added_at"]),
            )
        self._conn.execute("DROP TABLE library_roots_old")

    # ---------------- settings(補助KV) ----------------
    def _ensure_library_root_display_name_column(self) -> None:
        cols = [
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(library_roots)").fetchall()
        ]
        if "display_name" not in cols:
            self._conn.execute("ALTER TABLE library_roots ADD COLUMN display_name TEXT")


    def get_state(self, key: str, default: Any = None) -> Any:
        rows = self._query("SELECT value FROM settings WHERE key=?", (key,))
        if not rows:
            return default
        try:
            return json.loads(rows[0]["value"])
        except ValueError:
            return default

    def set_state(self, key: str, value: Any) -> None:
        self._execute(
            "INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value, ensure_ascii=False)),
        )

    # ---------------- library roots ----------------

    def add_root(self, root_id: str, path: str, kind: str = "any", display_name: str | None = None) -> None:
        self._execute(
            "INSERT INTO library_roots(id,path,kind,display_name,added_at) VALUES(?,?,?,?,?)",
            (root_id, path, kind, display_name, time.time()),
        )

    def update_root(self, root_id: str, display_name: str | None = None) -> None:
        self._execute(
            "UPDATE library_roots SET display_name=? WHERE id=?",
            (display_name, root_id),
        )

    def remove_root(self, root_id: str) -> None:
        self._execute("DELETE FROM library_roots WHERE id=?", (root_id,))

    def list_roots(self) -> list[dict]:
        return [
            {"id": r["id"], "path": r["path"], "kind": r["kind"], "display_name": r["display_name"]}
            for r in self._query(
                "SELECT id,path,kind,display_name FROM library_roots ORDER BY added_at"
            )
        ]

    # ---------------- media items ----------------

    def upsert_media_item(self, item_id: str, path: str, media_type: str,
                          rating: int | None, modified_at: float | None,
                          size: int | None) -> None:
        self.upsert_media_items([(item_id, path, media_type, rating, modified_at, size)])

    def upsert_media_items(self, items: list[tuple]) -> None:
        """Persist a folder listing in one transaction instead of one fsync per item."""
        if not items:
            return
        now = time.time()
        sql = (
            "INSERT INTO media_items(id,path,media_type,rating,modified_at,size,last_seen) "
            "VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET path=excluded.path, "
            "media_type=excluded.media_type, rating=excluded.rating, "
            "modified_at=excluded.modified_at, size=excluded.size, "
            "last_seen=excluded.last_seen"
        )
        with self._lock:
            try:
                self._conn.executemany(sql, (item + (now,) for item in items))
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def get_media_item(self, item_id: str) -> dict | None:
        rows = self._query("SELECT * FROM media_items WHERE id=?", (item_id,))
        return dict(rows[0]) if rows else None

    def get_media_item_by_path(self, path: str) -> dict | None:
        rows = self._query("SELECT * FROM media_items WHERE path=?", (path,))
        return dict(rows[0]) if rows else None

    def delete_media_item(self, item_id: str) -> None:
        self._execute("DELETE FROM media_items WHERE id=?", (item_id,))

    def remap_rated_folder(self, old_path: str, new_path: str,
                           entry_ids: dict[str, str]) -> None:
        """Move saved identities with a rated folder in one transaction.

        Retain source progress rows as recovery history. If a target already has
        progress (e.g. an earlier rating), preserve the most recently saved row.
        Match directory boundaries literally, never with SQL LIKE wildcards.
        """
        import os
        from .library_service import item_id_for
        prefix = old_path + os.sep
        with self._lock:
            self.flush_reading_progress()
            try:
                for old_id, new_id in entry_ids.items():
                    if old_id == new_id:
                        continue
                    self._conn.execute(
                        "INSERT INTO reading_progress SELECT ?,page_index,page_count,"
                        "updated_at,completed,reader_mode,reading_direction "
                        "FROM reading_progress WHERE entry_id=? "
                        "ON CONFLICT(entry_id) DO UPDATE SET "
                        "page_index=excluded.page_index,page_count=excluded.page_count,"
                        "updated_at=excluded.updated_at,completed=excluded.completed,"
                        "reader_mode=excluded.reader_mode,reading_direction=excluded.reading_direction "
                        "WHERE excluded.updated_at > reading_progress.updated_at",
                        (new_id, old_id),
                    )
                    self._conn.execute("UPDATE bookmarks SET entry_id=? WHERE entry_id=?",
                                       (new_id, old_id))
                rows = self._conn.execute("SELECT * FROM media_items").fetchall()
                for row in rows:
                    if row["path"] != old_path and not row["path"].startswith(prefix):
                        continue
                    path = new_path + row["path"][len(old_path):]
                    new_id = item_id_for(path)
                    old_id = row["id"]
                    if old_id != new_id:
                        self._conn.execute(
                            "INSERT INTO video_progress SELECT ?,position_seconds,duration_seconds,"
                            "updated_at,completed,playback_speed,audio_track,subtitle_track "
                            "FROM video_progress WHERE media_id=? "
                            "ON CONFLICT(media_id) DO UPDATE SET "
                            "position_seconds=excluded.position_seconds,duration_seconds=excluded.duration_seconds,"
                            "updated_at=excluded.updated_at,completed=excluded.completed,"
                            "playback_speed=excluded.playback_speed,audio_track=excluded.audio_track,"
                            "subtitle_track=excluded.subtitle_track "
                            "WHERE excluded.updated_at > video_progress.updated_at",
                            (new_id, old_id),
                        )
                        self._conn.execute("INSERT OR IGNORE INTO favorites SELECT ?,added_at FROM favorites WHERE media_id=?", (new_id, old_id))
                        self._conn.execute("DELETE FROM media_items WHERE id=?", (old_id,))
                    self._conn.execute(
                        "INSERT INTO media_items VALUES(?,?,?,?,?,?,?) "
                        "ON CONFLICT(id) DO UPDATE SET path=excluded.path",
                        (new_id, path, row["media_type"], row["rating"], row["modified_at"], row["size"], row["last_seen"]),
                    )
                    self._conn.execute("UPDATE recent_items SET media_id=?,path=? WHERE media_id=?", (new_id, path, old_id))
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    # ---------------- reading progress ----------------

    def save_reading_progress(self, entry_id: str, page_index: int,
                              page_count: int, completed: bool = False,
                              reader_mode: str | None = None,
                              reading_direction: str | None = None) -> None:
        row = (entry_id, page_index, page_count, time.time(),
               1 if completed else 0, reader_mode, reading_direction)
        with self._lock:
            self._pending_reading[entry_id] = row
            if completed or self.reading_flush_seconds <= 0:
                self.flush_reading_progress()
            elif self._reading_timer is None:
                self._schedule_reading_flush()

    def _schedule_reading_flush(self) -> None:
        self._reading_timer = threading.Timer(
            max(0.1, self.reading_flush_seconds), self._timed_reading_flush)
        self._reading_timer.daemon = True
        self._reading_timer.start()

    def _timed_reading_flush(self) -> None:
        import logging
        try:
            self.flush_reading_progress()
        except Exception:
            logging.getLogger("framedeck").exception("Reading progress flush failed")
            with self._lock:
                if self._pending_reading and self._reading_timer is None:
                    self._schedule_reading_flush()

    def flush_reading_progress(self) -> None:
        with self._lock:
            if self._reading_timer is not None:
                self._reading_timer.cancel()
                self._reading_timer = None
            if not self._pending_reading:
                return
            try:
                self._conn.executemany(
                    "INSERT INTO reading_progress"
                    "(entry_id,page_index,page_count,updated_at,completed,reader_mode,reading_direction) "
                    "VALUES(?,?,?,?,?,?,?) "
                    "ON CONFLICT(entry_id) DO UPDATE SET page_index=excluded.page_index, "
                    "page_count=excluded.page_count, updated_at=excluded.updated_at, "
                    "completed=excluded.completed, reader_mode=excluded.reader_mode, "
                    "reading_direction=excluded.reading_direction",
                    list(self._pending_reading.values()),
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
            self._pending_reading.clear()

    @staticmethod
    def _reading_row(row: tuple) -> dict:
        return dict(zip(("entry_id", "page_index", "page_count", "updated_at",
                         "completed", "reader_mode", "reading_direction"), row))

    def get_reading_progress(self, entry_id: str) -> dict | None:
        with self._lock:
            pending = self._pending_reading.get(entry_id)
            if pending is not None:
                return self._reading_row(pending)
            rows = self._query(
                "SELECT * FROM reading_progress WHERE entry_id=?", (entry_id,)
            )
            return dict(rows[0]) if rows else None

    def get_reading_progress_many(self, entry_ids: list[str]) -> dict[str, dict]:
        """Return saved reading state keyed by entry id.

        SQLite commonly limits bound parameters to 999, so large library
        folders are queried in bounded chunks rather than issuing one query per
        comic or relying on a platform-specific variable limit.
        """
        with self._lock:
            unique_ids = list(dict.fromkeys(entry_ids))
            result: dict[str, dict] = {}
            for offset in range(0, len(unique_ids), 500):
                chunk = unique_ids[offset:offset + 500]
                placeholders = ",".join("?" for _ in chunk)
                rows = self._query(
                    f"SELECT * FROM reading_progress WHERE entry_id IN ({placeholders})",
                    tuple(chunk),
                )
                result.update((row["entry_id"], dict(row)) for row in rows)
            for entry_id in unique_ids:
                pending = self._pending_reading.get(entry_id)
                if pending is not None:
                    result[entry_id] = self._reading_row(pending)
            return result

    # ---------------- video progress ----------------

    def save_video_progress(self, media_id: str, position: float,
                            duration: float, completed: bool = False,
                            speed: float = 1.0,
                            audio_track: int | None = None,
                            subtitle_track: int | None = None) -> None:
        self._execute(
            "INSERT INTO video_progress"
            "(media_id,position_seconds,duration_seconds,updated_at,completed,"
            "playback_speed,audio_track,subtitle_track) "
            "VALUES(?,?,?,?,?,?,?,?) "
            "ON CONFLICT(media_id) DO UPDATE SET "
            "position_seconds=excluded.position_seconds, "
            "duration_seconds=excluded.duration_seconds, "
            "updated_at=excluded.updated_at, completed=excluded.completed, "
            "playback_speed=excluded.playback_speed, "
            "audio_track=excluded.audio_track, "
            "subtitle_track=excluded.subtitle_track",
            (media_id, position, duration, time.time(), 1 if completed else 0,
             speed, audio_track, subtitle_track),
        )

    def get_video_progress(self, media_id: str) -> dict | None:
        rows = self._query(
            "SELECT * FROM video_progress WHERE media_id=?", (media_id,)
        )
        return dict(rows[0]) if rows else None

    # ---------------- recent ----------------

    def add_recent(self, media_id: str, path: str, media_type: str) -> None:
        self._execute("DELETE FROM recent_items WHERE media_id=?", (media_id,))
        self._execute(
            "INSERT INTO recent_items(media_id,path,media_type,opened_at) VALUES(?,?,?,?)",
            (media_id, path, media_type, time.time()),
        )
        self._execute(
            "DELETE FROM recent_items WHERE id NOT IN "
            "(SELECT id FROM recent_items ORDER BY opened_at DESC LIMIT 100)"
        )

    def list_recent(self, limit: int = 30) -> list[dict]:
        return [
            dict(r) for r in self._query(
                "SELECT * FROM recent_items ORDER BY opened_at DESC LIMIT ?",
                (limit,),
            )
        ]

    # ---------------- cache entries ----------------

    def touch_cache_entry(self, key: str, path: str, size: int) -> None:
        self._execute(
            "INSERT INTO cache_entries(key,path,size,last_used) VALUES(?,?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET last_used=excluded.last_used, "
            "size=excluded.size, path=excluded.path",
            (key, path, size, time.time()),
        )

    def list_cache_entries(self) -> list[dict]:
        return [
            dict(r) for r in self._query(
                "SELECT * FROM cache_entries ORDER BY last_used ASC"
            )
        ]

    def delete_cache_entry(self, key: str) -> None:
        self._execute("DELETE FROM cache_entries WHERE key=?", (key,))
