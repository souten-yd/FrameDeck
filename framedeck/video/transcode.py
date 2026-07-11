"""ブラウザ非対応形式の変換ストリーミング。

初期実装はffmpegによるプログレッシブfMP4(フラグメントMP4)配信。
`start` 秒からの再開に対応するため、シークはクライアント側で
`?start=` を付けた再読み込みとして行う。

設計メモ: 指示書はHLS生成を第一候補としているが、hls.jsの同梱を避けて
ビルド不要構成を守るため、初期実装はfMP4パイプ配信とした
(IMPLEMENTATION_NOTES.md参照)。キャッシュ済み完全変換・HLSは将来拡張。
"""
from __future__ import annotations

import shutil
import subprocess
import threading
from typing import Iterator

TRANSCODE_CHUNK = 1024 * 128


class TranscodeError(Exception):
    pass


class TranscodeService:
    def __init__(self):
        self._lock = threading.Lock()
        self._active: set[subprocess.Popen] = set()

    def available(self) -> bool:
        return shutil.which("ffmpeg") is not None

    def stream_fmp4(self, path: str, start_seconds: float = 0.0,
                    max_height: int = 720) -> Iterator[bytes]:
        """ffmpegでH.264/AACのフラグメントMP4へ変換しながら配信する。"""
        if not self.available():
            raise TranscodeError(
                "ffmpeg が見つかりません。sudo apt install ffmpeg でインストールしてください。"
            )
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error"]
        if start_seconds > 0:
            cmd += ["-ss", f"{start_seconds:.3f}"]
        cmd += [
            "-i", path,
            "-map", "0:v:0", "-map", "0:a:0?",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-vf", f"scale=-2:'min({max_height},ih)'",
            "-c:a", "aac", "-b:a", "160k", "-ac", "2",
            "-movflags", "frag_keyframe+empty_moov+default_base_moof",
            "-f", "mp4", "pipe:1",
        ]
        process = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
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
                self._terminate(process)

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
    if not shutil.which("ffmpeg"):
        raise TranscodeError("ffmpeg が見つかりません。")
    try:
        result = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error",
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
