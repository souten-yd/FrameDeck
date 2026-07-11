"""Adaptive video delivery profiles and playback decisions."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..models import VideoInfo

VIDEO_RESOLUTION_PROFILES: dict[str, dict[str, int]] = {
    "2160p": {"max_width": 3840, "max_height": 2160},
    "1440p": {"max_width": 2560, "max_height": 1440},
    "1080p": {"max_width": 1920, "max_height": 1080},
    "720p": {"max_width": 1280, "max_height": 720},
    "480p": {"max_width": 854, "max_height": 480},
    "360p": {"max_width": 640, "max_height": 360},
}

VIDEO_PROFILES: dict[str, dict[str, Any]] = {
    "original": {"transcode": False},
    "2160p": {"resolution": "2160p", "video_bitrate": "16000k", "audio_bitrate": "192k", "fps_limit": None, "codec": "h264"},
    "1440p": {"resolution": "1440p", "video_bitrate": "9000k", "audio_bitrate": "160k", "fps_limit": None, "codec": "h264"},
    "1080p": {"resolution": "1080p", "video_bitrate": "5000k", "audio_bitrate": "160k", "fps_limit": None, "codec": "h264"},
    "720p": {"resolution": "720p", "video_bitrate": "1800k", "audio_bitrate": "96k", "fps_limit": 30, "codec": "h264"},
    "480p": {"resolution": "480p", "video_bitrate": "850k", "audio_bitrate": "64k", "fps_limit": 30, "codec": "h264"},
    "360p": {"resolution": "360p", "video_bitrate": "450k", "audio_bitrate": "48k", "fps_limit": 24, "codec": "h264"},
}

_LEGACY_PROFILE_ALIASES = {
    "wifi_high": "1080p",
    "mobile_balanced": "720p",
    "mobile_low": "480p",
    "data_saver": "360p",
}
_PROFILE_ORDER = ["360p", "480p", "720p", "1080p", "1440p", "2160p"]


@dataclass(frozen=True)
class VideoClientHints:
    effective_type: str | None = None
    downlink_mbps: float | None = None
    save_data: bool = False
    viewport_width: int = 0
    viewport_height: int = 0
    device_pixel_ratio: float = 1.0
    measured_mbps: float | None = None


@dataclass(frozen=True)
class ResolvedVideoProfile:
    name: str
    transcode: bool
    height: int | None = None
    width: int | None = None
    resolution: str | None = None
    video_bitrate: str | None = None
    audio_bitrate: str | None = None
    fps_limit: int | None = None
    codec: str = "h264"
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "transcode": self.transcode,
            "height": self.height,
            "width": self.width,
            "resolution": self.resolution,
            "video_bitrate": self.video_bitrate,
            "audio_bitrate": self.audio_bitrate,
            "fps_limit": self.fps_limit,
            "codec": self.codec,
            "reason": self.reason,
        }


def canonical_video_profile(name: str | None) -> str:
    if not name or name == "auto":
        return "auto"
    return _LEGACY_PROFILE_ALIASES.get(name, name)


def resolution_box(profile: str, source_width: int = 0, source_height: int = 0) -> tuple[int | None, int | None]:
    profile = canonical_video_profile(profile)
    if profile == "original":
        return None, None
    base = VIDEO_RESOLUTION_PROFILES[profile]
    max_width = base["max_width"]
    max_height = base["max_height"]
    if source_width > 0 and source_height > source_width:
        max_width, max_height = max_height, max_width
    if source_width > 0:
        max_width = min(max_width, source_width)
    if source_height > 0:
        max_height = min(max_height, source_height)
    return max_width, max_height


def scale_filter_for_box(max_width: int | None, max_height: int | None) -> str:
    if not max_width or not max_height:
        return "scale=iw:ih"
    return (
        f"scale=w='min(iw,{max_width})':h='min(ih,{max_height})':"
        "force_original_aspect_ratio=decrease:force_divisible_by=2"
    )


def _bitrate_kbps(profile_name: str) -> int:
    profile_name = canonical_video_profile(profile_name)
    value = VIDEO_PROFILES[profile_name].get("video_bitrate", "0k")
    return int(str(value).rstrip("k"))


def _profile_from_bandwidth(mbps: float) -> str:
    budget_kbps = max(0.0, mbps) * 1000 * 0.60
    selected = "360p"
    for name in _PROFILE_ORDER:
        if _bitrate_kbps(name) <= budget_kbps:
            selected = name
    return selected


def _profile_from_viewport(hints: VideoClientHints) -> str:
    long_edge = max(hints.viewport_width, hints.viewport_height)
    dpr = max(1.0, min(hints.device_pixel_ratio or 1.0, 2.0))
    target = long_edge * dpr
    if target <= 480:
        return "480p"
    if target <= 900:
        return "720p"
    if target <= 1600:
        return "1080p"
    return "1440p"


def resolve_video_profile(
    name: str,
    source_height: int | None = None,
    source_width: int | None = None,
) -> ResolvedVideoProfile:
    name = canonical_video_profile(name)
    if name == "auto":
        name = "1080p"
    profile = VIDEO_PROFILES.get(name)
    if not profile:
        raise ValueError(f"Unknown video profile: {name}")
    if profile.get("transcode") is False:
        return ResolvedVideoProfile(name=name, transcode=False, reason="original", resolution="original")
    max_width, max_height = resolution_box(name, source_width or 0, source_height or 0)
    return ResolvedVideoProfile(
        name=name,
        transcode=True,
        height=max_height,
        width=max_width,
        resolution=name,
        video_bitrate=profile["video_bitrate"],
        audio_bitrate=profile["audio_bitrate"],
        fps_limit=profile["fps_limit"],
        codec=profile["codec"],
    )


def select_video_profile(
    settings: dict[str, Any],
    info: VideoInfo,
    hints: VideoClientHints | None = None,
    ui_profile: str = "desktop",
) -> ResolvedVideoProfile:
    hints = hints or VideoClientHints()
    mode = settings.get("video_stream_mode", "auto")
    if mode == "original":
        selected = resolve_video_profile("original", info.height, info.width)
        return ResolvedVideoProfile(**{**selected.to_dict(), "reason": "mode=original"})

    configured = canonical_video_profile(settings.get(
        "video_profile_mobile" if ui_profile == "mobile" else "video_profile_desktop",
        "720p" if ui_profile == "mobile" else "1080p",
    ))
    if configured == "auto":
        configured = canonical_video_profile(settings.get("video_max_resolution", "1080p"))
    if mode == "transcode":
        selected = configured if configured != "original" else "1080p"
        profile = resolve_video_profile(selected, info.height, info.width)
        return ResolvedVideoProfile(**{**profile.to_dict(), "reason": "mode=transcode"})

    if hints.save_data:
        profile = resolve_video_profile("480p", info.height, info.width)
        return ResolvedVideoProfile(**{**profile.to_dict(), "reason": "saveData"})

    # Local desktop use should avoid unnecessary compression when the browser can
    # play the original. Mobile/save-data/manual selections are handled above.
    if ui_profile != "mobile" and info.direct_play and configured in {"auto", "original", "1080p"}:
        selected = resolve_video_profile("original", info.height, info.width)
        return ResolvedVideoProfile(**{**selected.to_dict(), "reason": "desktop-direct-play"})

    measured = hints.measured_mbps or hints.downlink_mbps
    if measured:
        name = _profile_from_bandwidth(measured)
        profile = resolve_video_profile(name, info.height, info.width)
        return ResolvedVideoProfile(**{**profile.to_dict(), "reason": "bandwidth"})

    if hints.effective_type in {"slow-2g", "2g"}:
        name = "360p"
    elif hints.effective_type == "3g":
        name = "480p"
    elif ui_profile == "mobile":
        name = configured if configured != "original" else "720p"
    elif hints.viewport_width and hints.viewport_height:
        name = _profile_from_viewport(hints)
    else:
        name = configured if configured != "original" else "1080p"

    profile = resolve_video_profile(name, info.height, info.width)
    return ResolvedVideoProfile(**{**profile.to_dict(), "reason": "auto"})


def scale_filter_for_height(max_height: int, source_height: int) -> str:
    target = min(max_height, source_height) if source_height > 0 else max_height
    return f"scale=-2:'min({target},ih)'"
