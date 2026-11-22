"""Optional authenticated Linux fMP4 worker offload (no media copying).

The Linux worker reads a **read-only NFS/SMB mount**, not arbitrary remote URLs.
A remains the browser origin and proxies only the compressed output.  If the
worker is unavailable, A uses its established local conversion path.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import socket
from pathlib import Path
from urllib.parse import urlencode
from urllib.error import HTTPError, URLError
from urllib.request import Request

from .cluster_worker import (
    ClusterConfigError,
    _secure_opener,
    response_signature,
    signed_headers,
    validate_worker_url,
)
from .config import VIDEO_EXTENSIONS

logger = logging.getLogger("framedeck")
WORKER_FMP4_PATH = "/api/cluster/worker/fmp4"
_MAP_ENV = "FRAMEDECK_CLUSTER_WORKER_MOUNTS_JSON"
_ENABLED_ENV = "FRAMEDECK_CLUSTER_ENABLE_OFFLOAD"


class WorkerOffloadError(Exception):
    """Unavailable or unsafe worker; do not expose detailed network errors."""


def offload_enabled(settings=None) -> bool:
    if settings is not None and settings.get("cluster_offload_enabled", False) is True:
        return True
    return os.environ.get(_ENABLED_ENV, "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def _canonical_root(path: str) -> str:
    if not isinstance(path, str) or not path.startswith("/") or "\x00" in path:
        raise ClusterConfigError("invalid absolute root")
    return os.path.normpath(path)


def root_fingerprint(path: str) -> str:
    return hashlib.sha256(_canonical_root(path).encode("utf-8")).hexdigest()


def _is_read_only_mount(folder: Path) -> bool:
    """Enforce an OS-level read-only mount, not just read-only code paths."""
    return bool(os.statvfs(folder).f_flag & os.ST_RDONLY)


def worker_source_map() -> dict[str, Path]:
    """Read explicitly preconfigured NAS-root to local READ-ONLY mount mappings.

    Fail closed on invalid mapping.  This never scans the NAS and never
    discovers/mounts shares or downloads missing files on the worker.
    """
    raw = os.environ.get(_MAP_ENV, "")
    if not raw:
        return {}
    if len(raw) > 16384:
        raise ClusterConfigError("worker root map too large")
    try:
        mapping = json.loads(raw)
    except ValueError as exc:
        raise ClusterConfigError("worker root map is invalid JSON") from exc
    if not isinstance(mapping, dict) or len(mapping) > 64:
        raise ClusterConfigError("invalid worker root map")
    result: dict[str, Path] = {}
    for source, target in mapping.items():
        root = _canonical_root(source)
        if (not isinstance(target, str) or
                not target.startswith("/") or "\x00" in target or
                len(target) > 2048):
            raise ClusterConfigError("worker mount must be an absolute directory")
        # Do not allow a worker to use arbitrary system directories as a mount.
        folder = Path(target).resolve()
        if not folder.is_dir():
            raise ClusterConfigError("worker mount does not exist")
        if not _is_read_only_mount(folder):
            raise ClusterConfigError("worker source mount must be read-only")
        result[root] = folder
    return result


def worker_root_fingerprints() -> list[str]:
    try:
        return sorted(root_fingerprint(root) for root in worker_source_map())
    except (ClusterConfigError, OSError):
        logger.warning("Worker source mapping unavailable")
        return []


def resolve_mounted_video(source_root: str, relative_path: str) -> Path:
    """Resolve only explicitly mapped file, excluding traversal and symlink escape."""
    if not isinstance(relative_path, str) or not (0 < len(relative_path) <= 4096):
        raise WorkerOffloadError("invalid relative path")
    if ("\x00" in relative_path or "\\" in relative_path or
            relative_path.startswith("/") or
            any(part in ("", ".", "..") for part in relative_path.split("/"))):
        raise WorkerOffloadError("invalid relative path")
    if any(ord(char) < 32 or ord(char) == 127 for char in relative_path):
        raise WorkerOffloadError("invalid relative path")
    try:
        mapping = worker_source_map()
        root = mapping.get(_canonical_root(source_root))
        if root is None:
            raise WorkerOffloadError("source not configured")
        file = (root / relative_path).resolve(strict=True)
        file.relative_to(root)
        if not file.is_file() or file.suffix.lower() not in VIDEO_EXTENSIONS:
            raise WorkerOffloadError("source is not a video")
        return file
    except (ClusterConfigError, OSError, ValueError, RuntimeError) as exc:
        raise WorkerOffloadError("source unavailable") from exc


def owner_source_ref(video_path: str, library_roots: list[dict]) -> tuple[str, str]:
    """Match a validated A-side media path to its longest configured library root."""
    try:
        video = Path(video_path).resolve(strict=True)
    except OSError as exc:
        raise WorkerOffloadError("source unavailable") from exc
    best: tuple[str, str] | None = None
    longest_resolved_root = -1
    for entry in library_roots:
        try:
            # Use realpaths only for confinement checks.  The worker mapping
            # key must retain the *registered* path: QNAP /share/... roots
            # may be symlinks to CACHEDEV1_DATA and Linux cannot resolve the
            # QNAP-only symlink while comparing its configured mapping key.
            configured = _canonical_root(entry["path"])
            root = Path(configured).resolve(strict=True)
            relative = video.relative_to(root)
            if not relative.parts:
                continue
            candidate = (configured, relative.as_posix())
            if len(str(root)) > longest_resolved_root:
                best = candidate
                longest_resolved_root = len(str(root))
        except (KeyError, TypeError, ValueError, OSError):
            continue
    if best is None:
        raise WorkerOffloadError("source not in library")
    return best


def remote_eligible(controller, source_root: str, settings=None) -> bool:
    if not offload_enabled(settings):
        return False
    caps = controller.status().get("capabilities") or {}
    return bool(
        controller.verified_peer() is not None
        and caps.get("remote_fmp4") is True
        and caps.get("transcode_available") is True
        and root_fingerprint(source_root) in caps.get("source_roots", [])
    )


def open_remote_fmp4(controller, source_root: str, relative_path: str,
                     *, settings=None, start: float = 0.0,
                     max_height: int | None = None,
                     max_width: int | None = None,
                     session: str = "",
                     copy_video: bool = False,
                     copy_audio: bool = False,
                     smooth_fps: float | None = None,
                     tone_map: bool = False):
    """Authenticate request and response handshake; return a live HTTP stream.

    Call from a bounded thread since connecting to B can take a few seconds.
    Data streaming itself is chunked and never copied wholesale into RAM.
    """
    peer = controller.verified_peer()
    if not peer or not remote_eligible(controller, source_root, settings):
        raise WorkerOffloadError("worker is offline or unsupported")
    params = {
        "source_root": source_root,
        "relative_path": relative_path,
        "start": str(float(start)),
        "session": session[:64],
        "copy_video": "true" if copy_video else "false",
        "copy_audio": "true" if copy_audio else "false",
        "tone_map": "true" if tone_map else "false",
    }
    if max_height is not None:
        params["max_height"] = str(int(max_height))
    if max_width is not None:
        params["max_width"] = str(int(max_width))
    if smooth_fps is not None:
        params["smooth_fps"] = str(float(smooth_fps))
    provider = getattr(controller, "source_provider", None)
    if controller.status().get("capabilities", {}).get("automatic_sources") and provider:
        params.update(provider(source_root, relative_path))
    resource = WORKER_FMP4_PATH + "?" + urlencode(params)
    try:
        base = validate_worker_url(peer["url"])
        headers = signed_headers(peer["secret"], "GET", resource)
        response = _secure_opener().open(
            Request(base + resource, headers=headers, method="GET"), timeout=10,
        )
        try:
            if response.status != 200:
                raise WorkerOffloadError("unexpected worker response")
            proof = response.headers.get("X-FrameDeck-Stream-Proof", "")
            expected = response_signature(
                peer["secret"], headers["X-FrameDeck-Nonce"], b"",
            )
            if not hmac.compare_digest(proof, expected):
                raise WorkerOffloadError("invalid worker response proof")
            return response
        except Exception:
            response.close()
            raise
    except (HTTPError, URLError, OSError, ValueError, WorkerOffloadError,
            socket.timeout) as exc:
        controller.mark_unhealthy()
        logger.info("Linux fMP4 worker was unavailable (%s); using local route",
                    type(exc).__name__)
        raise WorkerOffloadError("worker unavailable") from exc
