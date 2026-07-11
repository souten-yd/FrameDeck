"""設定・パス管理。

永続データはすべて `<スクリプト名>_venv/` 配下へ保存する:
    config/  settings.json
    data/    framedeck.db
    cache/   comic_pages / nested_archives / thumbnails / transcodes
             video_variants / video_segments / comic_variants
             comic_analysis / device_models
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
    # 漫画適応配信・画像解析
    "comic_delivery_mode": "auto",           # original | auto | compressed
    "comic_output_format": "auto",           # auto | jpeg | webp | avif | png | original
    "comic_auto_crop": True,
    "comic_crop_white": True,
    "comic_crop_gray": True,
    "comic_crop_black": True,
    "comic_crop_tolerance": 18,
    "comic_crop_safety_margin": 4,
    "comic_variant_sharpen": True,           # 縮小配信時に輪郭を復元してから圧縮

    "comic_spread_detection": True,
    "comic_split_spread_in_single_mode": True,
    "comic_spread_display_behavior": "auto", # keep_original | split | auto
    "comic_client_enhancement": "auto",      # off | auto | sharpen | contrast | super_resolution
    "comic_desktop_view_mode": "spread",     # single | spread
    "comic_desktop_delivery_profile": "high",# original | high | balanced | mobile | data_saver | custom
    "comic_desktop_page_fit": "height",      # width | height | contain
    "comic_desktop_split_spread": False,
    "comic_desktop_client_enhancement": "off",
    "comic_mobile_view_mode": "single",
    "comic_mobile_delivery_profile": "mobile",
    "comic_mobile_page_fit": "width",
    "comic_mobile_split_spread": True,
    "comic_mobile_client_enhancement": "auto",
    # 動画
    "video_wheel_action": "seek",            # seek | volume
    "resume_playback": True,
    "default_volume": 0,
    # 動画適応配信
    "video_stream_mode": "auto",             # original | auto | transcode
    "video_profile_desktop": "1080p",       # auto | original | 2160p | 1440p | 1080p | 720p | 480p | 360p
    "video_profile_mobile": "720p",
    "video_codec": "h264",                   # h264 | hevc | av1 | vp9 | copy
    "video_container": "hls_fmp4",           # hls_fmp4
    "video_max_resolution": "1080p",
    "video_bitrate_kbps": 1800,
    "video_audio_bitrate_kbps": 96,
    "video_fps_limit": 30,
    "video_segment_duration": 4,
    "video_hardware_encoder": "auto",
    "video_ffmpeg_auto_download": True,
    "video_variant_cache_gb": 30,
    "video_variant_expire_days": 30,
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
    "comic_delivery_mode": {"original", "auto", "compressed"},
    "comic_output_format": {"auto", "jpeg", "webp", "avif", "png", "original"},
    "comic_spread_display_behavior": {"keep_original", "split", "auto"},
    "comic_client_enhancement": {
        "off", "auto", "sharpen", "contrast", "super_resolution",
    },
    "comic_desktop_view_mode": {"single", "spread"},
    "comic_mobile_view_mode": {"single", "spread"},
    "comic_desktop_delivery_profile": {
        "original", "high", "balanced", "mobile", "data_saver", "custom",
    },
    "comic_mobile_delivery_profile": {
        "original", "high", "balanced", "mobile", "data_saver", "custom",
    },
    "comic_desktop_page_fit": {"width", "height", "contain"},
    "comic_mobile_page_fit": {"width", "height", "contain"},
    "comic_desktop_client_enhancement": {
        "off", "auto", "sharpen", "contrast", "super_resolution",
    },
    "comic_mobile_client_enhancement": {
        "off", "auto", "sharpen", "contrast", "super_resolution",
    },
    "video_stream_mode": {"original", "auto", "transcode"},
    "video_profile_desktop": {
        "auto", "original", "2160p", "1440p", "1080p", "720p", "480p", "360p",
        "wifi_high", "mobile_balanced", "mobile_low", "data_saver", "custom",
    },
    "video_profile_mobile": {
        "auto", "original", "2160p", "1440p", "1080p", "720p", "480p", "360p",
        "wifi_high", "mobile_balanced", "mobile_low", "data_saver", "custom",
    },
    "video_max_resolution": {
        "auto", "original", "2160p", "1440p", "1080p", "720p", "480p", "360p",
    },
    "video_codec": {"h264", "hevc", "av1", "vp9", "copy"},
    "video_container": {"hls_fmp4"},
    "video_hardware_encoder": {
        "auto", "software", "h264_vaapi", "hevc_vaapi", "h264_amf",
        "hevc_amf", "h264_nvenc", "hevc_nvenc", "av1_nvenc",
    },
}


_NUMERIC_LIMITS: dict[str, tuple[float, float]] = {
    "comic_crop_tolerance": (0, 255),
    "comic_crop_safety_margin": (0, 128),
    "video_bitrate_kbps": (0, 100000),
    "video_audio_bitrate_kbps": (0, 2000),
    "video_fps_limit": (0, 240),
    "video_segment_duration": (1, 30),
    "video_variant_cache_gb": (0, 10000),
    "video_variant_expire_days": (0, 3650),
}


def resolve_comic_reader_settings(
    values: dict[str, Any],
    ui_profile: str,
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """PC/モバイル別漫画設定を共通設定へ重ねて解決する。"""
    profile = "mobile" if ui_profile == "mobile" else "desktop"
    resolved = {
        "reading_direction": values.get("reading_direction", "rtl"),
        "cover_as_single_page": values.get("cover_as_single_page", True),
        "auto_crop": values.get("comic_auto_crop", True),
        "delivery_mode": values.get("comic_delivery_mode", "auto"),
        "output_format": values.get("comic_output_format", "auto"),
        "crop_white": values.get("comic_crop_white", True),
        "crop_gray": values.get("comic_crop_gray", True),
        "crop_black": values.get("comic_crop_black", True),
        "crop_tolerance": values.get("comic_crop_tolerance", 18),
        "crop_safety_margin": values.get("comic_crop_safety_margin", 4),
        "spread_detection": values.get("comic_spread_detection", True),
        "split_spread_in_single_mode": values.get(
            "comic_split_spread_in_single_mode", True
        ),
        "spread_display_behavior": values.get(
            "comic_spread_display_behavior", "auto"
        ),
        "client_enhancement": values.get("comic_client_enhancement", "auto"),
        "view_mode": values.get(f"comic_{profile}_view_mode"),
        "delivery_profile": values.get(f"comic_{profile}_delivery_profile"),
        "page_fit": values.get(f"comic_{profile}_page_fit"),
        "split_spread": values.get(f"comic_{profile}_split_spread"),
    }
    profile_enhancement = values.get(f"comic_{profile}_client_enhancement")
    if profile_enhancement is not None:
        resolved["client_enhancement"] = profile_enhancement
    if overrides:
        resolved.update(overrides)
    return resolved


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
    def video_variants_cache(self) -> Path:
        return self.cache_dir / "video_variants"

    @property
    def video_segments_cache(self) -> Path:
        return self.cache_dir / "video_segments"

    @property
    def comic_variants_cache(self) -> Path:
        return self.cache_dir / "comic_variants"

    @property
    def comic_analysis_cache(self) -> Path:
        return self.cache_dir / "comic_analysis"

    @property
    def device_models_cache(self) -> Path:
        return self.cache_dir / "device_models"

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
        paths.video_variants_cache,
        paths.video_segments_cache,
        paths.comic_variants_cache,
        paths.comic_analysis_cache,
        paths.device_models_cache,
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
        for key, (low, high) in _NUMERIC_LIMITS.items():
            try:
                value = float(self._values.get(key))
            except (TypeError, ValueError):
                self._values[key] = DEFAULT_SETTINGS[key]
                continue
            if not low <= value <= high:
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
                if key in _NUMERIC_LIMITS:
                    low, high = _NUMERIC_LIMITS[key]
                    if not low <= float(value) <= high:
                        raise ValueError(f"Invalid value for {key}: {value!r}")
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
