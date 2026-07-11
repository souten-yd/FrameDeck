"""共通メディアモデル。"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Literal

MediaType = Literal["video", "comic", "folder"]


def media_id_for_path(path: str) -> str:
    """パスから決定論的な内部IDを生成する(WebへはこのIDのみ公開する)。"""
    return hashlib.sha1(path.encode("utf-8", "surrogatepass")).hexdigest()


@dataclass(frozen=True)
class MediaItem:
    id: str
    path: str
    display_name: str
    media_type: MediaType
    source_kind: str          # "file" | "directory"
    rating: int | None
    modified_at: float
    size: int | None

    @property
    def stars(self) -> str:
        if not self.rating:
            return "—"
        return "★" * self.rating + "☆" * (5 - self.rating)
