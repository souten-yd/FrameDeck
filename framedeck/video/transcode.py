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
import subprocess
import tempfile
import threading
import time

from .ffmpeg import resolve_ffmpeg
from .profile_service import scale_filter_for_box

TRANSCODE_CHUNK = 1024 * 128
#: 送出が始まらないまま放置されたストリームを回収するまでの猶予(秒)
STREAM_START_TIMEOUT = 30.0
REAPER_INTERVAL = 15.0
logger = logging.getLogger("framedeck")


class TranscodeError(Exception):
    pass


class Fmp4Stream:
    """変換ストリーム1本のハンドル。

    HTTP側は read() で読み、切断時に close() を呼ぶ。close() を確実に
    呼べるようにすることで、シークやファイル切替を連打しても ffmpeg
    プロセスが残り続けない(残るとCPUを食い潰して全体が停止する)。
    """

    def __init__(self, service: "TranscodeService", process: subprocess.Popen,
                 owner: str | None):
        self._service = service
        self._process = process
        self._errlog = None
        self.owner = owner
        self.created_at = time.monotonic()
        self.started = False
        self._closed = False

    def read(self, size: int = TRANSCODE_CHUNK) -> bytes:
        self.started = True
        stdout = self._process.stdout
        if self._closed or stdout is None:
            return b""
        try:
            return stdout.read(size)
        except (OSError, ValueError):
            return b""

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._service._release(self)


class TranscodeService:
    def __init__(self, auto_download_ffmpeg: bool = False):
        self._lock = threading.Lock()
        self._active: set[subprocess.Popen] = set()
        self._streams: dict[str, Fmp4Stream] = {}
        self._open: set[Fmp4Stream] = set()
        self._reaper: threading.Thread | None = None
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

    def open_fmp4(self, path: str, start_seconds: float = 0.0,
                  max_height: int | None = 1080,
                  max_width: int | None = None,
                  owner: str | None = None,
                  copy_video: bool = False,
                  copy_audio: bool = False,
                  smooth_fps: float | None = None) -> Fmp4Stream:
        """変換プロセスを起動してストリームハンドルを返す。

        `owner`(クライアントセッションID)が同じ古いストリームは停止する。
        シークやファイル切替の連打で ffmpeg が多重起動しないようにするため。
        """
        ffmpeg = resolve_ffmpeg(self.auto_download_ffmpeg)
        if not ffmpeg.path:
            raise TranscodeError(ffmpeg.error or "ffmpeg が見つかりません。")
        if owner:
            self.cancel_owner(owner)
        cmd = build_fmp4_transcode_cmd(
            path, start_seconds=start_seconds,
            max_height=max_height, max_width=max_width,
            copy_video=copy_video, copy_audio=copy_audio,
            smooth_fps=smooth_fps, ffmpeg_bin=ffmpeg.path,
        )
        # stderrはパイプにするとログが溜まった時にffmpegが書き込みで
        # ブロックして配信が止まるため、一時ファイルへ逃がす
        errlog = tempfile.TemporaryFile()
        process = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=errlog,
            bufsize=TRANSCODE_CHUNK,
        )
        stream = Fmp4Stream(self, process, owner)
        stream._errlog = errlog
        with self._lock:
            self._active.add(process)
            self._open.add(stream)
            if owner:
                self._streams[owner] = stream
        self._ensure_reaper()
        return stream

    def _ensure_reaper(self) -> None:
        """送出が始まらなかったストリームを回収するスレッドを起動する。

        接続が即座に切れた場合、送出ジェネレータが一度も動かず close() が
        呼ばれないことがある。取り残された ffmpeg を確実に片付ける。
        """
        with self._lock:
            if self._reaper is not None and self._reaper.is_alive():
                return
            self._reaper = threading.Thread(
                target=self._reap_loop, name="framedeck-transcode-reaper",
                daemon=True,
            )
            self._reaper.start()

    def _reap_loop(self) -> None:
        while True:
            time.sleep(REAPER_INTERVAL)
            with self._lock:
                if not self._open:
                    self._reaper = None
                    return
                now = time.monotonic()
                stale = [s for s in self._open
                         if not s.started and now - s.created_at > STREAM_START_TIMEOUT]
            for stream in stale:
                logger.info("開始されない変換ストリームを回収します")
                stream.close()

    def cancel_owner(self, owner: str) -> bool:
        """同じクライアントの進行中ストリームを停止する。"""
        with self._lock:
            stream = self._streams.pop(owner, None)
        if stream is None:
            return False
        stream.close()
        return True

    def _release(self, stream: Fmp4Stream) -> None:
        with self._lock:
            self._open.discard(stream)
            if stream.owner and self._streams.get(stream.owner) is stream:
                self._streams.pop(stream.owner, None)
        process = stream._process
        self._terminate(process)
        for pipe in (process.stdout,):
            try:
                if pipe is not None:
                    pipe.close()
            except OSError:
                pass
        errlog = stream._errlog
        if errlog is not None:
            try:
                if process.returncode not in (0, None, -15, -9):
                    errlog.seek(0)
                    detail = errlog.read().decode("utf-8", "replace").strip()
                    if detail:
                        logger.warning("ffmpeg 変換ストリームが異常終了しました: %s",
                                       detail[:1200])
            except OSError:
                pass
            finally:
                try:
                    errlog.close()
                except OSError:
                    pass

    def _terminate(self, process: subprocess.Popen) -> None:
        with self._lock:
            self._active.discard(process)
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()

    def shutdown(self) -> None:
        with self._lock:
            streams = list(self._open)
            active = list(self._active)
        for stream in streams:
            stream.close()
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
    max_height: int | None = 1080,
    max_width: int | None = None,
    copy_video: bool = False,
    copy_audio: bool = False,
    smooth_fps: float | None = None,
    ffmpeg_bin: str = "ffmpeg",
) -> list[str]:
    """Build a mobile-compatible progressive fragmented MP4 command.

    max_width / max_height が None の場合は原寸のまま(コーデック変換のみ)。
    copy_video=True は再エンコードせずコンテナだけ入れ替える(remux)。
    端末が映像コーデックを再生できる場合(MKVのH.264など)はこちらを使う。
    実時間の再エンコードは高解像度・高フレームレートで破綻するため、
    可能な限りコピーで済ませることが再生の安定に直結する。

    smooth_fps はディスプレイのリフレッシュレートに合わせた中間フレーム生成。
    `framerate` フィルタは前後フレームをブレンドするため、単純複製と違い
    3:2プルダウン特有の不均等な見え方が出ない(その代わり動きは柔らかくなる)。
    """
    cmd = [ffmpeg_bin, "-hide_banner", "-loglevel", "error"]
    if start_seconds > 0:
        cmd += ["-ss", f"{start_seconds:.3f}"]
    cmd += ["-i", path, "-map", "0:v:0", "-map", "0:a:0?"]
    if copy_video:
        cmd += ["-c:v", "copy"]
    else:
        filters = [scale_filter_for_box(max_width, max_height)]
        if smooth_fps and smooth_fps > 0:
            filters.append(f"framerate=fps={smooth_fps:.3f}")
        cmd += [
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
            "-vf", ",".join(filters),
        ]
    if copy_audio:
        cmd += ["-c:a", "copy"]
    else:
        cmd += ["-c:a", "aac", "-b:a", "160k", "-ac", "2"]
    cmd += [
        "-movflags", "+frag_keyframe+empty_moov+default_base_moof",
        "-f", "mp4", "pipe:1",
    ]
    return cmd
