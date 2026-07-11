"""設定・パス管理。

永続データはすべて `<スクリプト名>_venv/` 配下へ保存する:
    config/  settings.json
    data/    framedeck.db
    cache/   comic_pages / nested_archives / thumbnails / transcodes
    logs/    framedeck.log
    runtime/ mpv / locks
"""
from __future__ import annotations

import json
import os
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

APP_NAME = "FrameDeck"

VIDEO_EXTENSIONS = {
    ".mp4", ".mkv", ".avi", ".mov", ".wmv",
    ".flv", ".webm", ".m4v", ".mpg", ".mpeg", ".ts",
}
COMIC_EXTENSIONS = {".zip", ".cbz", ".rar", ".cbr"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}
MAX_FOLDER_NEST_DEPTH = 2

# 外部コマンドの標準タイムアウト(秒)
SUBPROCESS_TIMEOUT_LIST = 60
SUBPROCESS_TIMEOUT_READ = 300

DEFAULT_SETTINGS: dict[str, Any] = {
    # フォルダ
    "default_folder_video": "/mnt/Download/Temp",
    "default_folder_comic": "/mnt/Download/Manga",
    # 漫画ナビゲーション
    "comic_sequence_end_behavior": "stop",   # stop | wrap | prompt
    "reading_direction": "rtl",              # rtl | ltr
    "view_mode": "spread",                   # single | spread
    "cover_as_single_page": True,
    "landscape_threshold": 1.25,
    "previous_entry_start": "first",         # first | last | saved
    "include_parent_direct_images": True,
    # 漫画パフォーマンス
    "prefetch_ahead": 6,
    "prefetch_behind": 2,
    "memory_cache_mb": 512,
    "nested_cache_max_gb": 10,
    "nested_cache_max_age_days": 30,
    "resize_filter": "lanczos",              # lanczos | bicubic | bilinear | nearest
    # 動画
    "video_wheel_action": "seek",            # seek | volume
    "resume_playback": True,
    "default_volume": 0,
    # 削除
    "delete_to_trash": True,
    # Web
    "web_pin": "",
}

_VALID_ENUMS = {
    "comic_sequence_end_behavior": {"stop", "wrap", "prompt"},
    "reading_direction": {"rtl", "ltr"},
    "view_mode": {"single", "spread"},
    "previous_entry_start": {"first", "last", "saved"},
    "resize_filter": {"lanczos", "bicubic", "bilinear", "nearest"},
    "video_wheel_action": {"seek", "volume"},
}


@dataclass(frozen=True)
class AppPaths:
    base: Path
    config_dir: Path
    data_dir: Path
    cache_dir: Path
    log_dir: Path
    runtime_dir: Path

    @property
    def settings_file(self) -> Path:
        return self.config_dir / "settings.json"

    @property
    def database_file(self) -> Path:
        return self.data_dir / "framedeck.db"

    @property
    def comic_page_cache(self) -> Path:
        return self.cache_dir / "comic_pages"

    @property
    def nested_archive_cache(self) -> Path:
        return self.cache_dir / "nested_archives"

    @property
    def thumbnail_cache(self) -> Path:
        return self.cache_dir / "thumbnails"

    @property
    def transcode_cache(self) -> Path:
        return self.cache_dir / "transcodes"

    @property
    def mpv_runtime(self) -> Path:
        return self.runtime_dir / "mpv"

    @property
    def log_file(self) -> Path:
        return self.log_dir / "framedeck.log"


def resolve_app_paths(base_dir: str | os.PathLike | None = None) -> AppPaths:
    """データ保存ベースディレクトリを決定する。

    優先順位:
      1. 引数 base_dir (ランチャーがvenvディレクトリを渡す)
      2. 環境変数 FRAMEDECK_HOME (テスト用)
      3. 実行中のvenvルート (sys.executable の2つ上に pyvenv.cfg がある場合)
      4. カレントディレクトリ配下の FrameDeck_venv
    """
    if base_dir is None:
        base_dir = os.environ.get("FRAMEDECK_HOME")
    if base_dir is None:
        exe = Path(sys.executable).resolve()
        candidate = exe.parent.parent
        if (candidate / "pyvenv.cfg").exists():
            base_dir = candidate
    if base_dir is None:
        base_dir = Path.cwd() / "FrameDeck_venv"
    base = Path(base_dir)
    return AppPaths(
        base=base,
        config_dir=base / "config",
        data_dir=base / "data",
        cache_dir=base / "cache",
        log_dir=base / "logs",
        runtime_dir=base / "runtime",
    )


def ensure_runtime_directories(paths: AppPaths) -> None:
    for d in (
        paths.config_dir,
        paths.data_dir,
        paths.data_dir / "sessions",
        paths.data_dir / "indexes",
        paths.cache_dir,
        paths.comic_page_cache,
        paths.nested_archive_cache,
        paths.thumbnail_cache,
        paths.transcode_cache,
        paths.log_dir,
        paths.runtime_dir,
        paths.mpv_runtime,
        paths.runtime_dir / "locks",
    ):
        d.mkdir(parents=True, exist_ok=True)


class Settings:
    """settings.json を正とするスレッドセーフな設定ストア。"""

    def __init__(self, paths: AppPaths):
        self._paths = paths
        self._lock = threading.RLock()
        self._values: dict[str, Any] = dict(DEFAULT_SETTINGS)
        self._listeners: list = []
        self.load()
        if not self._paths.settings_file.exists():
            try:
                self.save()  # 初回起動時に既定値を書き出して編集可能にする
            except OSError:
                pass

    def load(self) -> None:
        with self._lock:
            try:
                raw = json.loads(self._paths.settings_file.read_text("utf-8"))
                if isinstance(raw, dict):
                    for key, value in raw.items():
                        if key in DEFAULT_SETTINGS:
                            self._values[key] = value
            except (OSError, ValueError):
                pass
            self._validate()

    def _validate(self) -> None:
        for key, allowed in _VALID_ENUMS.items():
            if self._values.get(key) not in allowed:
                self._values[key] = DEFAULT_SETTINGS[key]

    def save(self) -> None:
        with self._lock:
            self._paths.settings_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._paths.settings_file.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(self._values, ensure_ascii=False, indent=2), "utf-8"
            )
            tmp.replace(self._paths.settings_file)

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._values.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.update({key: value})

    def update(self, values: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            for key, value in values.items():
                if key not in DEFAULT_SETTINGS:
                    continue
                if key in _VALID_ENUMS and value not in _VALID_ENUMS[key]:
                    raise ValueError(f"Invalid value for {key}: {value!r}")
                expected = type(DEFAULT_SETTINGS[key])
                if expected in (int, float) and isinstance(value, (int, float)):
                    value = expected(value)
                self._values[key] = value
            self._validate()
            self.save()
            snapshot = dict(self._values)
        for listener in list(self._listeners):
            try:
                listener(snapshot)
            except Exception:
                pass
        return snapshot

    def as_dict(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._values)

    def add_listener(self, callback) -> None:
        self._listeners.append(callback)
