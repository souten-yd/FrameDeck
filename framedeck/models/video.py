"""動画セッション・トラック情報モデル。"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class TrackInfo:
    id: int
    kind: str                 # audio | subtitle | video
    codec: str
    language: str | None
    title: str | None
    default: bool = False

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "codec": self.codec,
            "language": self.language,
            "title": self.title,
            "default": self.default,
        }


@dataclass(frozen=True)
class ChapterInfo:
    index: int
    title: str
    start_seconds: float

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "title": self.title,
            "start_seconds": self.start_seconds,
        }


@dataclass(frozen=True)
class VideoInfo:
    media_id: str
    container: str
    duration_seconds: float
    width: int
    height: int
    video_codec: str
    audio_codec: str
    bitrate: int
    frame_rate: float
    tracks: tuple[TrackInfo, ...]
    chapters: tuple[ChapterInfo, ...]
    direct_play: bool
    direct_play_reason: str

    def to_dict(self) -> dict:
        return {
            "media_id": self.media_id,
            "container": self.container,
            "duration_seconds": self.duration_seconds,
            "width": self.width,
            "height": self.height,
            "video_codec": self.video_codec,
            "audio_codec": self.audio_codec,
            "bitrate": self.bitrate,
            "frame_rate": self.frame_rate,
            "tracks": [t.to_dict() for t in self.tracks],
            "chapters": [c.to_dict() for c in self.chapters],
            "direct_play": self.direct_play,
            "direct_play_reason": self.direct_play_reason,
        }


@dataclass
class VideoSession:
    media_id: str
    position_seconds: float = 0.0
    duration_seconds: float = 0.0
    paused: bool = True
    volume: int = 0
    muted: bool = False
    speed: float = 1.0
    audio_track_id: int | None = None
    subtitle_track_id: int | None = None
