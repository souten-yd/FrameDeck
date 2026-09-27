"""Small NAS policy shared by the web UI and service layer.

The QPKG launcher selects this profile; the Linux launcher retains the
existing settings and behavior. No application code is copied into the QPKG.
"""
from __future__ import annotations

import os

QNAP_TRANSCODE_WIDTH = 854
QNAP_TRANSCODE_HEIGHT = 480


def qnap_lite() -> bool:
    return os.environ.get("FRAMEDECK_PROFILE", "").lower() == "qnap-lite"


QNAP_DEFAULTS = {
    "prefetch_ahead": 1,
    "prefetch_behind": 0,
    "memory_cache_mb": 64,
    "nested_cache_max_gb": 1,
    "resize_filter": "bilinear",
    "comic_delivery_mode": "original",
    "comic_auto_crop": False,
    "comic_spread_detection": False,
    "comic_split_spread_in_single_mode": False,
    "comic_desktop_split_spread": False,
    "comic_mobile_split_spread": False,
    "comic_variant_sharpen": False,
    "comic_client_enhancement": "off",
    "comic_mobile_client_enhancement": "off",
    "comic_desktop_client_enhancement": "off",
    "comic_cache_max_mb": 32,
    "video_stream_mode": "auto",
    "video_profile_desktop": "auto",
    "video_profile_mobile": "auto",
    "video_cellular_max_resolution": "480p",
    "video_hls_max_concurrent": 1,
    "video_ffmpeg_auto_download": False,
    "video_variant_cache_mb": 128,
    "video_smooth_motion": "off",
}
