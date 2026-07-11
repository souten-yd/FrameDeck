"""動画再生の共通サービス。WebとデスクトップUIの両方が使う。"""
from __future__ import annotations

import threading

from ..core.storage import Storage
from ..models import VideoInfo
from .probe import probe_video


class VideoPlaybackService:
    def __init__(self, storage: Storage):
        self._storage = storage
        self._probe_cache: dict[str, tuple[float, VideoInfo]] = {}
        self._lock = threading.Lock()

    def get_info(self, path: str, media_id: str) -> VideoInfo:
        import os
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            mtime = 0.0
        with self._lock:
            cached = self._probe_cache.get(media_id)
            if cached and cached[0] == mtime:
                return cached[1]
        info = probe_video(path, media_id)
        with self._lock:
            self._probe_cache[media_id] = (mtime, info)
        return info

    def save_progress(self, media_id: str, position: float, duration: float,
                      speed: float = 1.0,
                      audio_track: int | None = None,
                      subtitle_track: int | None = None) -> None:
        completed = duration > 0 and position >= duration * 0.97
        self._storage.save_video_progress(
            media_id, position, duration, completed=completed,
            speed=speed, audio_track=audio_track,
            subtitle_track=subtitle_track,
        )

    def get_progress(self, media_id: str) -> dict | None:
        return self._storage.get_video_progress(media_id)

    def resume_position(self, media_id: str) -> float:
        """続きから再生する開始位置(完了済み・冒頭なら0)。"""
        progress = self.get_progress(media_id)
        if not progress or progress["completed"]:
            return 0.0
        position = float(progress["position_seconds"])
        return position if position > 5.0 else 0.0
