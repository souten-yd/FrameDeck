"""Cached HLS generation and file resolution."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

from .ffmpeg import resolve_ffmpeg
from .profile_service import VIDEO_PROFILES, canonical_video_profile, resolve_video_profile, scale_filter_for_box
from .transcode import TranscodeError

HLS_VERSION = "hls-v1"
HLS_PROFILES = ("360p", "480p", "720p", "1080p")


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


def _bitrate_kbps(value: str | None) -> int:
    if not value:
        return 0
    raw = str(value).strip().lower()
    if raw.endswith("k"):
        return int(float(raw[:-1]))
    if raw.endswith("m"):
        return int(float(raw[:-1]) * 1000)
    return int(float(raw) / 1000 if float(raw) > 10000 else float(raw))


class HlsService:
    def __init__(self, cache_root: Path, segment_duration: int = 4,
                 auto_download_ffmpeg: bool = False):
        self.cache_root = Path(cache_root)
        self.segment_duration = int(segment_duration)
        self.auto_download_ffmpeg = bool(auto_download_ffmpeg)
        self._lock = threading.Lock()
        self._active_keys: set[str] = set()

    def configure(self, *, auto_download_ffmpeg: bool) -> None:
        self.auto_download_ffmpeg = bool(auto_download_ffmpeg)

    def available(self) -> bool:
        return resolve_ffmpeg(self.auto_download_ffmpeg).available

    def update_segment_duration(self, value: int) -> None:
        self.segment_duration = int(value)

    def cache_key(self, source_path: str, profiles: list[str], source_height: int = 0) -> str:
        stat = os.stat(source_path)
        payload = {
            "version": HLS_VERSION,
            "path": os.path.abspath(source_path),
            "mtime": stat.st_mtime,
            "size": stat.st_size,
            "profiles": profiles,
            "source_height": source_height,
            "segment_duration": self.segment_duration,
        }
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode()).hexdigest()

    def manifest_for(self, source_path: str, profiles: list[str] | None = None,
                     source_height: int = 0) -> HlsManifest:
        profiles = self._normalize_profiles(profiles)
        key = self.cache_key(source_path, profiles, source_height)
        cache_dir = self.cache_root / key
        variants = tuple(self._variant_for(name, source_height) for name in profiles)
        return HlsManifest(
            cache_dir=cache_dir,
            master=cache_dir / "master.m3u8",
            variants=variants,
            ready=(cache_dir / "master.m3u8").exists(),
        )

    def ensure(self, source_path: str, profiles: list[str] | None = None,
               source_height: int = 0) -> HlsManifest:
        manifest = self.manifest_for(source_path, profiles, source_height)
        if manifest.ready:
            return manifest
        if not self.available():
            raise TranscodeError("ffmpeg が見つかりません。HLSを生成できません。")
        key = manifest.cache_dir.name
        with self._lock:
            if key in self._active_keys:
                raise TranscodeError("HLS生成中です。しばらくしてから再試行してください。")
            self._active_keys.add(key)
        try:
            self._generate(source_path, manifest)
        finally:
            with self._lock:
                self._active_keys.discard(key)
        return self.manifest_for(source_path, [v.name for v in manifest.variants], source_height)


    def ensure_async(self, source_path: str, profiles: list[str] | None = None,
                     source_height: int = 0) -> HlsManifest:
        manifest = self.manifest_for(source_path, profiles, source_height)
        if manifest.ready:
            return manifest
        if not self.available():
            raise TranscodeError("ffmpeg が見つかりません。HLSを生成できません。")
        key = manifest.cache_dir.name
        manifest.cache_dir.mkdir(parents=True, exist_ok=True)
        self._write_master(manifest)
        with self._lock:
            if key in self._active_keys:
                return self.manifest_for(source_path, [v.name for v in manifest.variants], source_height)
            self._active_keys.add(key)
        def worker() -> None:
            try:
                self._generate(source_path, manifest)
            finally:
                with self._lock:
                    self._active_keys.discard(key)
        threading.Thread(target=worker, name=f"framedeck-hls-{key[:8]}", daemon=True).start()
        return self.manifest_for(source_path, [v.name for v in manifest.variants], source_height)

    def resolve_file(self, source_path: str, relative_path: str,
                     profiles: list[str] | None = None,
                     source_height: int = 0) -> Path:
        manifest = self.manifest_for(source_path, profiles, source_height)
        root = manifest.cache_dir.resolve()
        target = (manifest.cache_dir / relative_path).resolve()
        try:
            target.relative_to(root)
        except ValueError as e:
            raise TranscodeError("HLSキャッシュ外のファイルは配信できません。") from e
        if not target.exists() or not target.is_file():
            raise FileNotFoundError(relative_path)
        return target

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

    def _generate(self, source_path: str, manifest: HlsManifest) -> None:
        manifest.cache_dir.mkdir(parents=True, exist_ok=True)
        generated: list[Path] = []
        try:
            for variant in manifest.variants:
                out_dir = manifest.cache_dir / variant.name
                out_dir.mkdir(parents=True, exist_ok=True)
                profile = resolve_video_profile(variant.name, variant.height)
                cmd = self._ffmpeg_cmd(source_path, out_dir,
                                       profile.width, profile.height,
                                       profile.video_bitrate or "1800k",
                                       profile.audio_bitrate or "96k",
                                       profile.fps_limit)
                subprocess.run(cmd, check=True, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, timeout=None)
                generated.append(out_dir)
            self._write_master(manifest)
        except (OSError, subprocess.SubprocessError) as e:
            for path in generated:
                shutil.rmtree(path, ignore_errors=True)
            try:
                manifest.master.unlink()
            except OSError:
                pass
            raise TranscodeError(f"HLS生成に失敗しました: {e}") from e

    def _ffmpeg_cmd(self, source_path: str, out_dir: Path,
                    max_width: int | None, max_height: int | None,
                    video_bitrate: str, audio_bitrate: str,
                    fps_limit: int | None) -> list[str]:
        filters = [scale_filter_for_box(max_width, max_height)]
        if fps_limit:
            filters.append(f"fps=fps='min({fps_limit},source_fps)'")
        return [
            resolve_ffmpeg(self.auto_download_ffmpeg).path or "ffmpeg",
            "-hide_banner", "-loglevel", "error", "-y",
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
