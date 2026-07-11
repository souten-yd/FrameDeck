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
    path TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL DEFAULT 'any',
    added_at REAL NOT NULL
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
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cursor = self._conn.execute(sql, params)
            self._conn.commit()
            return cursor

    def _query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    # ---------------- settings(補助KV) ----------------

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

    def add_root(self, root_id: str, path: str, kind: str = "any") -> None:
        self._execute(
            "INSERT OR IGNORE INTO library_roots(id,path,kind,added_at) VALUES(?,?,?,?)",
            (root_id, path, kind, time.time()),
        )

    def remove_root(self, root_id: str) -> None:
        self._execute("DELETE FROM library_roots WHERE id=?", (root_id,))

    def list_roots(self) -> list[dict]:
        return [
            {"id": r["id"], "path": r["path"], "kind": r["kind"]}
            for r in self._query(
                "SELECT id,path,kind FROM library_roots ORDER BY added_at"
            )
        ]

    # ---------------- media items ----------------

    def upsert_media_item(self, item_id: str, path: str, media_type: str,
                          rating: int | None, modified_at: float | None,
                          size: int | None) -> None:
        self._execute(
            "INSERT INTO media_items(id,path,media_type,rating,modified_at,size,last_seen) "
            "VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET path=excluded.path, "
            "media_type=excluded.media_type, rating=excluded.rating, "
            "modified_at=excluded.modified_at, size=excluded.size, "
            "last_seen=excluded.last_seen",
            (item_id, path, media_type, rating, modified_at, size, time.time()),
        )

    def get_media_item(self, item_id: str) -> dict | None:
        rows = self._query("SELECT * FROM media_items WHERE id=?", (item_id,))
        return dict(rows[0]) if rows else None

    def get_media_item_by_path(self, path: str) -> dict | None:
        rows = self._query("SELECT * FROM media_items WHERE path=?", (path,))
        return dict(rows[0]) if rows else None

    def delete_media_item(self, item_id: str) -> None:
        self._execute("DELETE FROM media_items WHERE id=?", (item_id,))

    # ---------------- reading progress ----------------

    def save_reading_progress(self, entry_id: str, page_index: int,
                              page_count: int, completed: bool = False,
                              reader_mode: str | None = None,
                              reading_direction: str | None = None) -> None:
        self._execute(
            "INSERT INTO reading_progress"
            "(entry_id,page_index,page_count,updated_at,completed,reader_mode,reading_direction) "
            "VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(entry_id) DO UPDATE SET page_index=excluded.page_index, "
            "page_count=excluded.page_count, updated_at=excluded.updated_at, "
            "completed=excluded.completed, reader_mode=excluded.reader_mode, "
            "reading_direction=excluded.reading_direction",
            (entry_id, page_index, page_count, time.time(),
             1 if completed else 0, reader_mode, reading_direction),
        )

    def get_reading_progress(self, entry_id: str) -> dict | None:
        rows = self._query(
            "SELECT * FROM reading_progress WHERE entry_id=?", (entry_id,)
        )
        return dict(rows[0]) if rows else None

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
