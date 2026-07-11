"""ffmpeg executable resolution.

FrameDeck prefers the system ffmpeg, but can optionally bootstrap the
`imageio-ffmpeg` wheel into the current Python environment and use its bundled
binary. This keeps the host OS untouched while allowing mobile transcode paths
to work on machines that do not have ffmpeg installed globally.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass

logger = logging.getLogger("framedeck")

PIP_INSTALL_TIMEOUT = 180


@dataclass(frozen=True)
class FfmpegResolution:
    path: str | None
    source: str
    error: str | None = None

    @property
    def available(self) -> bool:
        return bool(self.path)


def system_ffmpeg_path() -> str | None:
    return shutil.which("ffmpeg")


def system_ffprobe_path() -> str | None:
    return shutil.which("ffprobe")


def resolve_ffmpeg(auto_download: bool = False) -> FfmpegResolution:
    system = system_ffmpeg_path()
    if system:
        return FfmpegResolution(system, "system")

    bundled = _imageio_ffmpeg_path(auto_download=auto_download)
    if bundled.available:
        return bundled

    if bundled.error:
        return bundled
    return FfmpegResolution(None, "missing", "ffmpeg が見つかりません。")


def _imageio_ffmpeg_path(auto_download: bool) -> FfmpegResolution:
    try:
        import imageio_ffmpeg  # type: ignore
    except ImportError:
        if not auto_download:
            return FfmpegResolution(
                None, "missing",
                "imageio-ffmpeg が未導入のため自動取得できません。",
            )
        installed = _install_imageio_ffmpeg()
        if installed.error:
            return installed
        try:
            import imageio_ffmpeg  # type: ignore
        except ImportError as e:
            return FfmpegResolution(None, "missing", str(e))

    try:
        path = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as e:  # pragma: no cover - platform/package specific
        return FfmpegResolution(None, "imageio-ffmpeg", str(e))
    if path and os.path.exists(path):
        return FfmpegResolution(path, "imageio-ffmpeg")
    return FfmpegResolution(None, "imageio-ffmpeg", "同梱ffmpegを解決できません。")


def _install_imageio_ffmpeg() -> FfmpegResolution:
    cmd = [
        sys.executable, "-m", "pip", "install",
        "--disable-pip-version-check", "imageio-ffmpeg",
    ]
    try:
        subprocess.run(
            cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=PIP_INSTALL_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as e:
        logger.warning("imageio-ffmpeg の自動取得に失敗しました: %s", e)
        return FfmpegResolution(None, "missing", f"imageio-ffmpeg の自動取得に失敗しました: {e}")
    return FfmpegResolution(None, "imageio-ffmpeg")
