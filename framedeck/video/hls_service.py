"""Cached HLS generation and file resolution.

生成は key(ソース+プロファイル+開始秒+設定のハッシュ)単位のディレクトリへ
バックグラウンドで行う。生成完了時に `complete` マーカーを書き、マーカーの
無いディレクトリは再利用しない(クラッシュ残骸は削除して作り直す)。

シークは `start` 秒からの再生成として扱い、同一ソースに対する古い生成
ジョブは停止して未完成キャッシュを削除する。完成済みキャッシュは
`max_cache_bytes` を上限に古い順で prune する。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .ffmpeg import resolve_ffmpeg
from .profile_service import VIDEO_PROFILES, canonical_video_profile, resolve_video_profile, scale_filter_for_box
from .transcode import TranscodeError

HLS_VERSION = "hls-v2"
HLS_PROFILES = ("360p", "480p", "720p", "1080p")
COMPLETE_MARKER = "complete"
_KEY_RE = re.compile(r"^[0-9a-f]{64}$")

logger = logging.getLogger("framedeck")


@dataclass(frozen=True)
class HlsVariant:
    name: str
    width: int
    height: int
    bandwidth: int
    playlist: str


@dataclass(frozen=True)
class HlsManifest:
    cache_dir: Path
    master: Path
    variants: tuple[HlsVariant, ...]
    ready: bool

    @property
    def key(self) -> str:
        return self.cache_dir.name


class _HlsCancelled(Exception):
    pass


@dataclass
class _HlsJob:
    key: str
    source_path: str
    process: subprocess.Popen | None = None
    cancelled: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)

    def cancel(self) -> None:
        with self.lock:
            self.cancelled = True
            process = self.process
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()


def _bitrate_kbps(value: str | None) -> int:
    if not value:
        return 0
    raw = str(value).strip().lower()
    if raw.endswith("k"):
        return int(float(raw[:-1]))
    if raw.endswith("m"):
        return int(float(raw[:-1]) * 1000)
    return int(float(raw) / 1000 if float(raw) > 10000 else float(raw))


def _dir_size(path: Path) -> int:
    total = 0
    for child in path.rglob("*"):
        try:
            if child.is_file():
                total += child.stat().st_size
        except OSError:
            continue
    return total


class HlsService:
    def __init__(self, cache_root: Path, segment_duration: int = 4,
                 auto_download_ffmpeg: bool = False,
                 max_cache_bytes: int = 0):
        self.cache_root = Path(cache_root)
        self.segment_duration = int(segment_duration)
        self.auto_download_ffmpeg = bool(auto_download_ffmpeg)
        self.max_cache_bytes = int(max_cache_bytes)
        self._lock = threading.Lock()
        self._jobs: dict[str, _HlsJob] = {}

    def configure(self, *, auto_download_ffmpeg: bool | None = None,
                  max_cache_bytes: int | None = None) -> None:
        if auto_download_ffmpeg is not None:
            self.auto_download_ffmpeg = bool(auto_download_ffmpeg)
        if max_cache_bytes is not None:
            self.max_cache_bytes = int(max_cache_bytes)

    def available(self) -> bool:
        return resolve_ffmpeg(self.auto_download_ffmpeg).available

    def update_segment_duration(self, value: int) -> None:
        self.segment_duration = int(value)

    def cache_key(self, source_path: str, profiles: list[str],
                  source_height: int = 0, start_seconds: float = 0.0) -> str:
        stat = os.stat(source_path)
        payload = {
            "version": HLS_VERSION,
            "path": os.path.abspath(source_path),
            "mtime": stat.st_mtime,
            "size": stat.st_size,
            "profiles": profiles,
            "source_height": source_height,
            "segment_duration": self.segment_duration,
            "start": round(float(start_seconds), 2),
        }
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode()).hexdigest()

    def manifest_for(self, source_path: str, profiles: list[str] | None = None,
                     source_height: int = 0,
                     start_seconds: float = 0.0) -> HlsManifest:
        profiles = self._normalize_profiles(profiles)
        key = self.cache_key(source_path, profiles, source_height, start_seconds)
        cache_dir = self.cache_root / key
        variants = tuple(self._variant_for(name, source_height) for name in profiles)
        return HlsManifest(
            cache_dir=cache_dir,
            master=cache_dir / "master.m3u8",
            variants=variants,
            ready=self._is_complete(cache_dir),
        )

    def _is_complete(self, cache_dir: Path) -> bool:
        return (cache_dir / "master.m3u8").exists() and \
            (cache_dir / COMPLETE_MARKER).exists()

    def is_generating(self, key: str) -> bool:
        with self._lock:
            return key in self._jobs

    def dir_for_key(self, key: str) -> Path:
        if not _KEY_RE.match(key or ""):
            raise TranscodeError("不正なHLSキャッシュキーです。")
        return self.cache_root / key

    def ensure(self, source_path: str, profiles: list[str] | None = None,
               source_height: int = 0,
               start_seconds: float = 0.0) -> HlsManifest:
        """同期生成(テスト・デスクトップ用)。"""
        manifest = self.manifest_for(source_path, profiles, source_height, start_seconds)
        if manifest.ready:
            return manifest
        if not self.available():
            raise TranscodeError("ffmpeg が見つかりません。HLSを生成できません。")
        job = self._register_job(manifest, source_path)
        if job is None:
            raise TranscodeError("HLS生成中です。しばらくしてから再試行してください。")
        try:
            self._generate(source_path, manifest, job, start_seconds)
        finally:
            self._unregister_job(job)
        return self.manifest_for(
            source_path, [v.name for v in manifest.variants],
            source_height, start_seconds,
        )

    def ensure_async(self, source_path: str, profiles: list[str] | None = None,
                     source_height: int = 0,
                     start_seconds: float = 0.0) -> HlsManifest:
        """バックグラウンド生成を開始してマスタープレイリストを即返す。

        同一ソースの他ジョブ(別start/プロファイル)は停止し、未完成
        キャッシュを削除する。
        """
        manifest = self.manifest_for(source_path, profiles, source_height, start_seconds)
        if manifest.ready:
            return manifest
        if not self.available():
            raise TranscodeError("ffmpeg が見つかりません。HLSを生成できません。")
        self.cancel_source(source_path, except_key=manifest.key)
        job = self._register_job(manifest, source_path)
        if job is None:
            # 同一keyの生成が進行中
            return manifest
        manifest.cache_dir.mkdir(parents=True, exist_ok=True)
        self._write_master(manifest)

        def worker() -> None:
            try:
                self._generate(source_path, manifest, job, start_seconds)
            except _HlsCancelled:
                shutil.rmtree(manifest.cache_dir, ignore_errors=True)
            except TranscodeError as e:
                logger.warning("HLS生成に失敗しました: %s", e)
            finally:
                self._unregister_job(job)
                try:
                    self.prune()
                except Exception:
                    logger.exception("HLSキャッシュのpruneに失敗しました")

        threading.Thread(target=worker, name=f"framedeck-hls-{manifest.key[:8]}",
                         daemon=True).start()
        return manifest

    def _register_job(self, manifest: HlsManifest, source_path: str) -> _HlsJob | None:
        key = manifest.key
        with self._lock:
            if key in self._jobs:
                return None
            job = _HlsJob(key=key, source_path=os.path.abspath(source_path))
            self._jobs[key] = job
        # 未完成の残骸(クラッシュ等)は作り直す
        if manifest.cache_dir.exists() and not self._is_complete(manifest.cache_dir):
            shutil.rmtree(manifest.cache_dir, ignore_errors=True)
        return job

    def _unregister_job(self, job: _HlsJob) -> None:
        with self._lock:
            self._jobs.pop(job.key, None)

    def cancel_source(self, source_path: str, except_key: str | None = None) -> int:
        """指定ソースの生成ジョブを停止する。停止したジョブ数を返す。

        停止されたジョブの未完成キャッシュはworker側で削除される。
        """
        abspath = os.path.abspath(source_path)
        with self._lock:
            jobs = [job for job in self._jobs.values()
                    if job.source_path == abspath and job.key != except_key]
        for job in jobs:
            job.cancel()
        return len(jobs)

    def shutdown(self) -> None:
        with self._lock:
            jobs = list(self._jobs.values())
        for job in jobs:
            job.cancel()

    def wait_for_file(self, key: str, relative_path: str,
                      timeout: float = 10.0) -> Path:
        """生成中のファイル出現を待って返す。

        生成ジョブが無く未完成なら即 FileNotFoundError、タイムアウトは
        TimeoutError を送出する。
        """
        cache_dir = self.dir_for_key(key)
        root = cache_dir.resolve()
        target = (cache_dir / relative_path).resolve()
        try:
            target.relative_to(root)
        except ValueError as e:
            raise TranscodeError("HLSキャッシュ外のファイルは配信できません。") from e
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            if target.is_file() and target.stat().st_size > 0:
                return target
            if not self.is_generating(key) and not self._is_complete(cache_dir):
                raise FileNotFoundError(relative_path)
            if time.monotonic() >= deadline:
                raise TimeoutError(relative_path)
            time.sleep(0.2)

    def resolve_file(self, source_path: str, relative_path: str,
                     profiles: list[str] | None = None,
                     source_height: int = 0,
                     start_seconds: float = 0.0) -> Path:
        manifest = self.manifest_for(source_path, profiles, source_height, start_seconds)
        root = manifest.cache_dir.resolve()
        target = (manifest.cache_dir / relative_path).resolve()
        try:
            target.relative_to(root)
        except ValueError as e:
            raise TranscodeError("HLSキャッシュ外のファイルは配信できません。") from e
        if not target.exists() or not target.is_file():
            raise FileNotFoundError(relative_path)
        return target

    def prune(self, max_bytes: int | None = None) -> None:
        """HLSキャッシュを整理する。

        - 生成中でない未完成ディレクトリは削除(中断・クラッシュ残骸)。
        - 完成済みは合計サイズが上限を超えたら古い順に削除。
        """
        if max_bytes is None:
            max_bytes = self.max_cache_bytes
        if not self.cache_root.exists():
            return
        with self._lock:
            active = set(self._jobs)
        completed: list[tuple[float, int, Path]] = []
        for child in self.cache_root.iterdir():
            if not child.is_dir() or child.name in active:
                continue
            if not self._is_complete(child):
                shutil.rmtree(child, ignore_errors=True)
                continue
            try:
                mtime = (child / COMPLETE_MARKER).stat().st_mtime
            except OSError:
                mtime = 0.0
            completed.append((mtime, _dir_size(child), child))
        if not max_bytes or max_bytes <= 0:
            return
        completed.sort(reverse=True)  # 新しい順
        total = 0
        for mtime, size, path in completed:
            total += size
            if total > max_bytes:
                shutil.rmtree(path, ignore_errors=True)

    def _normalize_profiles(self, profiles: list[str] | None) -> list[str]:
        if not profiles:
            return ["720p"]
        result = []
        for name in profiles:
            canonical = canonical_video_profile(name)
            if canonical in VIDEO_PROFILES and canonical != "original" and canonical not in result:
                result.append(canonical)
        return result or ["720p"]

    def _variant_for(self, name: str, source_height: int) -> HlsVariant:
        canonical = canonical_video_profile(name)
        profile = resolve_video_profile(canonical, source_height or None)
        width = int(profile.width or 0)
        height = int(profile.height or 0)
        video_kbps = _bitrate_kbps(profile.video_bitrate)
        audio_kbps = _bitrate_kbps(profile.audio_bitrate)
        bandwidth = max(1, (video_kbps + audio_kbps) * 1000)
        return HlsVariant(name=canonical, width=width, height=height, bandwidth=bandwidth,
                          playlist=f"{canonical}/playlist.m3u8")

    def _write_master(self, manifest: HlsManifest) -> None:
        lines = ["#EXTM3U", "#EXT-X-VERSION:7"]
        for variant in manifest.variants:
            lines.append(
                f"#EXT-X-STREAM-INF:BANDWIDTH={variant.bandwidth},RESOLUTION={variant.width}x{variant.height}"
            )
            lines.append(variant.playlist)
        manifest.master.write_text("\n".join(lines) + "\n", "utf-8")

    def _generate(self, source_path: str, manifest: HlsManifest,
                  job: _HlsJob, start_seconds: float = 0.0) -> None:
        manifest.cache_dir.mkdir(parents=True, exist_ok=True)
        self._write_master(manifest)
        try:
            for variant in manifest.variants:
                if job.cancelled:
                    raise _HlsCancelled()
                out_dir = manifest.cache_dir / variant.name
                out_dir.mkdir(parents=True, exist_ok=True)
                profile = resolve_video_profile(variant.name, variant.height)
                cmd = self._ffmpeg_cmd(source_path, out_dir,
                                       profile.width, profile.height,
                                       profile.video_bitrate or "1800k",
                                       profile.audio_bitrate or "96k",
                                       profile.fps_limit,
                                       start_seconds)
                process = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                           stderr=subprocess.PIPE)
                with job.lock:
                    if job.cancelled:
                        process.terminate()
                    job.process = process
                _, stderr = process.communicate()
                with job.lock:
                    job.process = None
                if job.cancelled:
                    raise _HlsCancelled()
                if process.returncode != 0:
                    detail = stderr.decode("utf-8", "replace").strip()[:1200]
                    raise TranscodeError(f"HLS生成に失敗しました: {detail}")
            (manifest.cache_dir / COMPLETE_MARKER).write_text(
                json.dumps({"source": os.path.abspath(source_path),
                            "start": round(float(start_seconds), 2)}),
                "utf-8",
            )
        except _HlsCancelled:
            raise
        except (OSError, subprocess.SubprocessError, TranscodeError) as e:
            shutil.rmtree(manifest.cache_dir, ignore_errors=True)
            if isinstance(e, TranscodeError):
                raise
            raise TranscodeError(f"HLS生成に失敗しました: {e}") from e

    def _ffmpeg_cmd(self, source_path: str, out_dir: Path,
                    max_width: int | None, max_height: int | None,
                    video_bitrate: str, audio_bitrate: str,
                    fps_limit: int | None,
                    start_seconds: float = 0.0) -> list[str]:
        filters = [scale_filter_for_box(max_width, max_height)]
        if fps_limit:
            filters.append(f"fps=fps='min({fps_limit},source_fps)'")
        cmd = [
            resolve_ffmpeg(self.auto_download_ffmpeg).path or "ffmpeg",
            "-hide_banner", "-loglevel", "error", "-y",
        ]
        if start_seconds > 0:
            cmd += ["-ss", f"{start_seconds:.3f}"]
        cmd += [
            "-i", source_path,
            "-map", "0:v:0", "-map", "0:a:0?",
            "-c:v", "libx264", "-preset", "veryfast",
            "-profile:v", "baseline", "-level", "4.0",
            "-pix_fmt", "yuv420p", "-tag:v", "avc1", "-bf", "0",
            "-b:v", video_bitrate,
            "-vf", ",".join(filters),
            "-c:a", "aac", "-b:a", audio_bitrate, "-ac", "2",
            "-f", "hls",
            "-hls_time", str(self.segment_duration),
            "-hls_list_size", "0",
            "-hls_flags", "independent_segments",
            "-hls_segment_type", "fmp4",
            "-hls_fmp4_init_filename", "init.mp4",
            "-hls_segment_filename", str(out_dir / "segment_%05d.m4s"),
            str(out_dir / "playlist.m3u8"),
        ]
        return cmd
