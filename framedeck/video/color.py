"""Explicit video colour metadata and bounded HDR-to-SDR conversion.

Do not color-grade ordinary SDR or remux/direct video. A source tagged as PQ or
HLG can be mapped to BT.709 only when the FFmpeg zscale and tonemap filters exist.
"""
from __future__ import annotations

import functools
import subprocess


HDR_TRANSFERS = frozenset({"smpte2084", "arib-std-b67"})


def detect_hdr_format(stream: dict) -> str:
    side_data = stream.get("side_data_list") or []
    if any("dovi" in str(side.get("side_data_type", "")).lower()
           for side in side_data if isinstance(side, dict)):
        return "dolby_vision"
    transfer = str(stream.get("color_transfer") or "").lower()
    if transfer == "smpte2084":
        return "hdr10"
    if transfer == "arib-std-b67":
        return "hlg"
    if transfer in {"bt709", "iec61966-2-1", "bt470bg", "smpte170m"}:
        return "sdr"
    if str(stream.get("color_primaries") or "").lower() == "bt709":
        return "sdr"
    return "unknown"


def hdr_to_sdr_filters() -> list[str]:
    """Run in linear light before SDR BT.709 encoding; preserve audio untouched."""
    return [
        "zscale=t=linear:npl=100",
        "format=gbrpf32le",
        "tonemap=tonemap=hable:desat=0",
        "zscale=p=bt709:t=bt709:m=bt709:r=tv",
        "format=yuv420p",
    ]


@functools.lru_cache(maxsize=8)
def check_tonemap_filters(ffmpeg_binary: str) -> None:
    """Fail explicitly rather than silently outputting wrongly tagged HDR."""
    from .transcode import TranscodeError

    try:
        result = subprocess.run([ffmpeg_binary, "-hide_banner", "-filters"],
                                capture_output=True, text=True, timeout=6, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise TranscodeError("HDR変換用FFmpegフィルターを確認できません") from exc
    output = result.stdout
    if result.returncode != 0 or not all(
        any(line.split() and line.split()[1:2] == [name]
            for line in output.splitlines())
        for name in ("zscale", "tonemap")
    ):
        raise TranscodeError("このFFmpegにはHDR→SDRに必要なzscale/tonemapがありません")
