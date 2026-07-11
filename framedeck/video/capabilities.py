"""ffmpeg encoder capability detection."""
from __future__ import annotations

import subprocess
from dataclasses import dataclass

from .ffmpeg import resolve_ffmpeg


@dataclass(frozen=True)
class EncoderCapability:
    name: str
    codec: str
    hardware: bool


_ENCODERS = {
    "h264_vaapi": ("h264", True),
    "hevc_vaapi": ("hevc", True),
    "h264_amf": ("h264", True),
    "hevc_amf": ("hevc", True),
    "h264_nvenc": ("h264", True),
    "hevc_nvenc": ("hevc", True),
    "av1_nvenc": ("av1", True),
    "libx264": ("h264", False),
}


class EncoderCapabilityService:
    def detect(self, auto_download_ffmpeg: bool = False) -> list[EncoderCapability]:
        ffmpeg = resolve_ffmpeg(auto_download_ffmpeg)
        if not ffmpeg.path:
            return []
        try:
            result = subprocess.run(
                [ffmpeg.path, "-hide_banner", "-encoders"],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            return []
        text = result.stdout.decode("utf-8", "replace")
        found = []
        for name, (codec, hardware) in _ENCODERS.items():
            if name in text:
                found.append(EncoderCapability(name, codec, hardware))
        return found
