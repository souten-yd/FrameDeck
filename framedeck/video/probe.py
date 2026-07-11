"""ffprobeによる動画メタデータ取得。"""
from __future__ import annotations

import json
import shutil
import subprocess

from ..models import ChapterInfo, TrackInfo, VideoInfo

PROBE_TIMEOUT = 20

# ブラウザ直接再生を期待できる組み合わせ
_BROWSER_CONTAINERS = {"mp4", "webm"}
_BROWSER_VIDEO_CODECS = {"h264", "vp8", "vp9", "av1"}
_BROWSER_AUDIO_CODECS = {"aac", "mp3", "opus", "vorbis", "flac", ""}


def ffprobe_available() -> bool:
    return shutil.which("ffprobe") is not None


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def probe_video(path: str, media_id: str) -> VideoInfo:
    if not ffprobe_available():
        return _fallback_info(path, media_id)
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error", "-print_format", "json",
                "-show_format", "-show_streams", "-show_chapters", path,
            ],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=PROBE_TIMEOUT,
        )
        data = json.loads(result.stdout.decode("utf-8", "replace"))
    except (subprocess.SubprocessError, ValueError, OSError):
        return _fallback_info(path, media_id)

    fmt = data.get("format", {})
    streams = data.get("streams", [])
    # format_nameは "mov,mp4,m4a,3gp,3g2,mj2" のような別名リストになる
    container_names = {
        n.strip() for n in (fmt.get("format_name") or "").split(",") if n
    }
    container = _canonical_container(container_names)
    duration = float(fmt.get("duration") or 0.0)
    bitrate = int(fmt.get("bit_rate") or 0)

    width = height = 0
    video_codec = audio_codec = ""
    frame_rate = 0.0
    tracks: list[TrackInfo] = []
    for stream in streams:
        kind = stream.get("codec_type")
        codec = stream.get("codec_name") or ""
        tags = stream.get("tags") or {}
        disposition = stream.get("disposition") or {}
        if kind == "video" and not video_codec and codec != "mjpeg":
            video_codec = codec
            width = int(stream.get("width") or 0)
            height = int(stream.get("height") or 0)
            rate = stream.get("avg_frame_rate") or "0/1"
            try:
                num, den = rate.split("/")
                frame_rate = float(num) / float(den) if float(den) else 0.0
            except (ValueError, ZeroDivisionError):
                frame_rate = 0.0
        if kind == "audio" and not audio_codec:
            audio_codec = codec
        if kind in ("audio", "subtitle", "video"):
            tracks.append(TrackInfo(
                id=int(stream.get("index", len(tracks))),
                kind=kind,
                codec=codec,
                language=tags.get("language"),
                title=tags.get("title"),
                default=bool(disposition.get("default")),
            ))

    chapters = tuple(
        ChapterInfo(
            index=i,
            title=(c.get("tags") or {}).get("title") or f"Chapter {i + 1}",
            start_seconds=float(c.get("start_time") or 0.0),
        )
        for i, c in enumerate(data.get("chapters", []))
    )

    direct, reason = _direct_play_decision(container_names, video_codec,
                                           audio_codec)
    return VideoInfo(
        media_id=media_id, container=container,
        duration_seconds=duration, width=width, height=height,
        video_codec=video_codec, audio_codec=audio_codec,
        bitrate=bitrate, frame_rate=frame_rate,
        tracks=tuple(tracks), chapters=chapters,
        direct_play=direct, direct_play_reason=reason,
    )


def _canonical_container(names: set[str]) -> str:
    for candidate in ("mp4", "matroska", "webm", "avi", "mov", "mpegts",
                      "flv", "asf", "mpeg"):
        if candidate in names:
            return candidate
    return next(iter(sorted(names)), "")


def _direct_play_decision(container_names: set[str], video_codec: str,
                          audio_codec: str) -> tuple[bool, str]:
    if "matroska" in container_names:
        return False, "MKVコンテナはブラウザ直接再生に非対応です(変換ストリーミングを使用)"
    if not (container_names & _BROWSER_CONTAINERS):
        name = _canonical_container(container_names) or "不明"
        return False, f"コンテナ {name} はブラウザ直接再生に非対応です"
    if video_codec and video_codec not in _BROWSER_VIDEO_CODECS:
        return False, f"動画コーデック {video_codec} はブラウザ非対応です"
    if audio_codec and audio_codec not in _BROWSER_AUDIO_CODECS:
        return False, f"音声コーデック {audio_codec} はブラウザ非対応です"
    return True, "直接再生可能"


def _fallback_info(path: str, media_id: str) -> VideoInfo:
    """ffprobeがない場合は拡張子ベースの推定のみ行う。"""
    import os
    ext = os.path.splitext(path)[1].lower()
    container = ext.lstrip(".")
    direct = ext in (".mp4", ".webm", ".m4v")
    reason = ("ffprobe が見つからないため詳細情報を取得できません。"
              "sudo apt install ffmpeg を推奨します。")
    return VideoInfo(
        media_id=media_id, container=container, duration_seconds=0.0,
        width=0, height=0, video_codec="", audio_codec="", bitrate=0,
        frame_rate=0.0, tracks=(), chapters=(),
        direct_play=direct, direct_play_reason=reason,
    )
