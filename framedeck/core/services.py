"""アプリ全体で共有するサービスコンテナ。

WebサーバとデスクトップUIは同じServicesインスタンスを使用し、
読書履歴・再生位置・設定を共有する。
"""
from __future__ import annotations

import logging

from ..comic.image_pipeline import ImagePipeline
from ..comic.nested_cache import NestedArchiveCache
from ..comic.reader_engine import ComicReaderEngine
from ..comic.sequence_builder import SequenceBuilder
from ..comic.source import ComicSourceResolver
from ..config import AppPaths, Settings
from ..video.playback_service import VideoPlaybackService
from ..video.transcode import TranscodeService
from .library_service import LibraryService
from .rating_service import RatingService
from .security import ConfirmTokenBox
from .storage import Storage

logger = logging.getLogger("framedeck")


class Services:
    def __init__(self, settings: Settings, storage: Storage, paths: AppPaths):
        self.settings = settings
        self.storage = storage
        self.paths = paths

        self.rating = RatingService(storage)
        self.library = LibraryService(storage, settings, self.rating)

        self.nested_cache = NestedArchiveCache(
            paths.nested_archive_cache, storage,
            max_bytes=int(settings.get("nested_cache_max_gb", 10)) * 1024**3,
            max_age_days=float(settings.get("nested_cache_max_age_days", 30)),
        )
        self.sequence_builder = SequenceBuilder(
            include_parent_direct_images=bool(
                settings.get("include_parent_direct_images", True))
        )
        self.pipeline = ImagePipeline(
            paths.comic_page_cache, paths.thumbnail_cache,
            memory_limit_bytes=int(settings.get("memory_cache_mb", 512)) * 1024**2,
            resize_filter=settings.get("resize_filter", "lanczos"),
        )
        self.resolver = ComicSourceResolver(self.nested_cache)
        self.comic_engine = ComicReaderEngine(
            self.sequence_builder, self.resolver, self.pipeline,
            storage, settings,
        )

        self.video_playback = VideoPlaybackService(storage)
        self.transcode = TranscodeService()
        self.confirm_tokens = ConfirmTokenBox()

        settings.add_listener(self._on_settings_changed)

    def _on_settings_changed(self, values: dict) -> None:
        self.sequence_builder.include_parent_direct_images = bool(
            values.get("include_parent_direct_images", True)
        )
        self.pipeline.resize_filter = values.get("resize_filter", "lanczos")

    def startup_maintenance(self) -> None:
        try:
            self.nested_cache.prune()
        except Exception:
            logger.exception("キャッシュ整理に失敗しました")

    def shutdown(self) -> None:
        try:
            self.comic_engine.shutdown()
        except Exception:
            pass
        try:
            self.transcode.shutdown()
        except Exception:
            pass
        try:
            self.pipeline.shutdown()
        except Exception:
            pass
        try:
            self.nested_cache.prune()
        except Exception:
            pass
        try:
            self.storage.close()
        except Exception:
            pass


def build_services(settings: Settings, storage: Storage,
                   paths: AppPaths) -> Services:
    return Services(settings, storage, paths)
