"""ブラウザ非対応形式の変換ストリーミング。

初期実装はffmpegによるプログレッシブfMP4(フラグメントMP4)配信。
`start` 秒からの再開に対応するため、シークはクライアント側で
`?start=` を付けた再読み込みとして行う。

設計メモ: 指示書はHLS生成を第一候補としているが、hls.jsの同梱を避けて
ビルド不要構成を守るため、初期実装はfMP4パイプ配信とした
(IMPLEMENTATION_NOTES.md参照)。キャッシュ済み完全変換・HLSは将来拡張。
"""
from __future__ import annotations

import logging
import shutil
import subprocess
import threading
from typing import Iterator

from .ffmpeg import resolve_ffmpeg
from .profile_service import scale_filter_for_box

TRANSCODE_CHUNK = 1024 * 128
logger = logging.getLogger("framedeck")


class TranscodeError(Exception):
    pass


class TranscodeService:
    def __init__(self, auto_download_ffmpeg: bool = False):
        self._lock = threading.Lock()
        self._active: set[subprocess.Popen] = set()
        self.auto_download_ffmpeg = bool(auto_download_ffmpeg)

    def configure(self, *, auto_download_ffmpeg: bool) -> None:
        self.auto_download_ffmpeg = bool(auto_download_ffmpeg)

    def available(self) -> bool:
        return resolve_ffmpeg(self.auto_download_ffmpeg).available

    def ffmpeg_status(self) -> dict:
        resolved = resolve_ffmpeg(self.auto_download_ffmpeg)
        return {
            "available": resolved.available,
            "source": resolved.source,
            "path": resolved.path,
            "error": resolved.error,
        }

    def stream_fmp4(self, path: str, start_seconds: float = 0.0,
                    max_height: int = 1080,
                    max_width: int | None = None) -> Iterator[bytes]:
        """ffmpegでH.264/AACのフラグメントMP4へ変換しながら配信する。"""
        ffmpeg = resolve_ffmpeg(self.auto_download_ffmpeg)
        if not ffmpeg.path:
            raise TranscodeError(ffmpeg.error or "ffmpeg が見つかりません。")
        cmd = build_fmp4_transcode_cmd(
            path, start_seconds=start_seconds,
            max_height=max_height, max_width=max_width,
            ffmpeg_bin=ffmpeg.path,
        )
        process = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            bufsize=TRANSCODE_CHUNK,
        )
        with self._lock:
            self._active.add(process)

        def generate() -> Iterator[bytes]:
            try:
                assert process.stdout is not None
                while True:
                    chunk = process.stdout.read(TRANSCODE_CHUNK)
                    if not chunk:
                        break
                    yield chunk
            finally:
                stderr = b""
                if process.stderr is not None:
                    try:
                        stderr = process.stderr.read()
                    except OSError:
                        stderr = b""
                self._terminate(process)
                if process.returncode not in (0, None) and stderr:
                    logger.warning(
                        "ffmpeg 変換ストリームが終了しました: %s",
                        stderr.decode("utf-8", "replace").strip()[:1200],
                    )

        return generate()

    def _terminate(self, process: subprocess.Popen) -> None:
        with self._lock:
            self._active.discard(process)
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()

    def shutdown(self) -> None:
        with self._lock:
            active = list(self._active)
        for process in active:
            self._terminate(process)


def video_thumbnail(path: str, at_seconds: float = 10.0,
                    width: int = 480) -> bytes:
    """ffmpegで動画サムネイル(JPEG)を生成する。"""
    ffmpeg = resolve_ffmpeg(auto_download=False)
    if not ffmpeg.path:
        raise TranscodeError("ffmpeg が見つかりません。")
    try:
        result = subprocess.run(
            [
                ffmpeg.path, "-hide_banner", "-loglevel", "error",
                "-ss", f"{at_seconds:.2f}", "-i", path,
                "-frames:v", "1", "-vf", f"scale={width}:-2",
                "-f", "image2", "-c:v", "mjpeg", "pipe:1",
            ],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=30,
        )
    except subprocess.SubprocessError as e:
        raise TranscodeError(f"サムネイル生成に失敗しました: {e}") from e
    if not result.stdout:
        raise TranscodeError("サムネイル生成に失敗しました(出力なし)。")
    return result.stdout


def build_fmp4_transcode_cmd(
    path: str,
    *,
    start_seconds: float = 0.0,
    max_height: int = 1080,
    max_width: int | None = None,
    ffmpeg_bin: str = "ffmpeg",
) -> list[str]:
    """Build a mobile-compatible progressive fragmented MP4 command."""
    cmd = [ffmpeg_bin, "-hide_banner", "-loglevel", "error"]
    if start_seconds > 0:
        cmd += ["-ss", f"{start_seconds:.3f}"]
    cmd += [
        "-i", path,
        "-map", "0:v:0", "-map", "0:a:0?",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-tune", "zerolatency",
        "-profile:v", "baseline",
        "-level", "4.0",
        "-pix_fmt", "yuv420p",
        "-tag:v", "avc1",
        "-bf", "0",
        "-g", "60",
        "-keyint_min", "60",
        "-sc_threshold", "0",
        "-crf", "23",
        "-vf", scale_filter_for_box(max_width, max_height),
        "-c:a", "aac", "-b:a", "160k", "-ac", "2",
        "-movflags", "+frag_keyframe+empty_moov+default_base_moof",
        "-f", "mp4", "pipe:1",
    ]
    return cmd
