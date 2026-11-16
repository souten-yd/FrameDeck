"""Opt-in cluster worker pairing and health endpoints.

Worker capabilities are read-only and require an HMAC-authenticated request.
No job execution, arbitrary URL fetching, media mutation or remote input API
is exposed in this stage.
"""
from __future__ import annotations

import json
import threading

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from starlette.background import BackgroundTask

from ... import __version__
from ...cluster_worker import (
    CAPABILITY_PATH,
    PROTOCOL_VERSION,
    ClusterConfigError,
    response_signature,
    worker_secret,
)
from ...core.services import Services
from ...worker_offload import (
    WORKER_FMP4_PATH, WorkerOffloadError, resolve_mounted_video,
    worker_root_fingerprints, root_fingerprint,
)
from ...video.ffmpeg import resolve_ffmpeg
from ...worker_hls import (
    WORKER_HLS_FILE, WORKER_HLS_START, HLS_ALLOWED, valid_hls_file,
)
from ...video.hls_service import HLS_PROFILES
from ...video.transcode import TranscodeError
from ..dependencies import get_services
from .system import _require_same_origin
from .video import _aiter_transcode, _hls_wait_file

router = APIRouter(prefix="/api/cluster", tags=["cluster"])
_WORKER_TRANSCODE_GATE = threading.BoundedSemaphore(2)


def _require_pairing_permission(request: Request, services: Services) -> None:
    _require_same_origin(request)
    # Existing web_pin middleware authenticates all remote requests when set.
    # Require that protection before accepting a remotely supplied secret.
    host = request.client.host if request.client else ""
    local = host == "testclient" or (
        host in ("127.0.0.1", "::1", "localhost") and
        request.url.hostname in ("127.0.0.1", "::1", "localhost")
    )
    if not local and not services.settings.get("web_pin", ""):
        raise HTTPException(
            status_code=403,
            detail="LANからワーカーを登録する前にWeb PINを設定してください",
        )


@router.get("/status")
def cluster_status(services: Services = Depends(get_services)) -> dict:
    peer = services.cluster.status()
    capabilities = peer["capabilities"] or {}
    roots = capabilities.get("source_roots", [])
    matching_video_root = any(
        root_fingerprint(root["path"]) in roots
        for root in services.library.list_roots()
        if root.get("kind") in ("video", "any")
    )
    peer["offload_active"] = bool(
        services.cluster.verified_peer() is not None and
        capabilities.get("remote_fmp4") is True and
        capabilities.get("transcode_available") is True and
        matching_video_root and
        (services.settings.get("cluster_offload_enabled", False) is True or
         peer["offload_active"])
    )
    return {
        "protocol_version": PROTOCOL_VERSION,
        "worker_mode": worker_secret() is not None,
        "auto_discovery_enabled": services.discovery.enabled(),
        "web_pin_configured": bool(services.settings.get("web_pin", "")),
        "peer": peer,
        "hls_performance": services.remote_hls.status(),
    }


@router.post("/discovery/enable")
def enable_auto_discovery(request: Request,
                          services: Services = Depends(get_services)) -> dict:
    """Enable trusted LAN discovery and reveal the shared key ONCE to admin."""
    _require_pairing_permission(request, services)
    already_enabled = services.discovery.enabled()
    secret = services.discovery.enable() if not already_enabled else None
    return {"enabled": True, "secret": secret,
            "udp_port": 31964}


@router.delete("/discovery")
def disable_auto_discovery(request: Request,
                           services: Services = Depends(get_services)) -> dict:
    _require_pairing_permission(request, services)
    services.discovery.disable()
    return {"enabled": False}


@router.put("/peer")
def set_cluster_peer(request: Request, payload: dict = Body(...),
                     services: Services = Depends(get_services)) -> dict:
    _require_pairing_permission(request, services)
    try:
        services.cluster.configure(payload.get("url"), payload.get("secret"))
    except ClusterConfigError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return services.cluster.status()


@router.delete("/peer")
def remove_cluster_peer(request: Request,
                        services: Services = Depends(get_services)) -> dict:
    _require_pairing_permission(request, services)
    services.discovery.disable()
    services.cluster.clear()
    return services.cluster.status()


@router.post("/probe")
def probe_worker(request: Request,
                 services: Services = Depends(get_services)) -> dict:
    """Explicit bounded health check from settings UI, not a media request."""
    _require_pairing_permission(request, services)
    services.cluster.probe_once()
    return services.cluster.status()


@router.get("/worker/capabilities")
def worker_capabilities(request: Request,
                        services: Services = Depends(get_services)) -> Response:
    secret = worker_secret()
    if secret is None or not request.app.state.cluster_guard.verify(
        secret, "GET", CAPABILITY_PATH, request.headers,
    ):
        raise HTTPException(status_code=403, detail="worker authentication required")
    # Do not bootstrap ffmpeg during a heartbeat.  Linux may have it in PATH;
    # QNAP's packaged binary is also detected without a background install.
    ffmpeg = resolve_ffmpeg(False)
    roots = worker_root_fingerprints()
    payload = json.dumps({
        "protocol_version": PROTOCOL_VERSION,
        "worker": True,
        "version": __version__,
        "transcode_available": bool(ffmpeg.available),
        "remote_fmp4": bool(ffmpeg.available and roots),
        "remote_hls": bool(ffmpeg.available and roots),
        "source_roots": roots,
    }, separators=(",", ":")).encode("utf-8")
    signature = response_signature(
        secret, request.headers["x-framedeck-nonce"], payload,
    )
    return Response(
        content=payload, media_type="application/json",
        headers={"X-FrameDeck-Response-Signature": signature,
                 "Cache-Control": "no-store"},
    )


@router.get("/worker/fmp4")
def worker_fmp4(request: Request,
                source_root: str = Query(..., min_length=1, max_length=2048),
                relative_path: str = Query(..., min_length=1, max_length=4096),
                start: float = Query(0.0, ge=0.0, le=1000000.0),
                max_height: int | None = Query(None, ge=144, le=4320),
                max_width: int | None = Query(None, ge=144, le=7680),
                session: str = Query("", max_length=64),
                copy_video: bool = False,
                copy_audio: bool = False,
                smooth_fps: float | None = Query(None, ge=20.0, le=240.0),
                services: Services = Depends(get_services)):
    """Authentication-gated conversion of only preconfigured mounted videos.

    There is intentionally no arbitrary URL, command, filepath or raw-file API.
    The Linux mount should be explicitly read-only at the OS level.
    """
    secret = worker_secret()
    signed_resource = request.url.path + (
        ("?" + request.url.query) if request.url.query else ""
    )
    if secret is None or not request.app.state.cluster_guard.verify(
        secret, "GET", signed_resource, request.headers,
    ):
        raise HTTPException(status_code=403, detail="worker authentication required")
    if not _WORKER_TRANSCODE_GATE.acquire(blocking=False):
        raise HTTPException(status_code=503, detail="worker is busy")
    try:
        source = resolve_mounted_video(source_root, relative_path)
        if not resolve_ffmpeg(False).available:
            raise WorkerOffloadError("ffmpeg unavailable")
        stream = services.transcode.open_fmp4(
            str(source), start, max_height=max_height, max_width=max_width,
            owner=session or None, copy_video=copy_video,
            copy_audio=copy_audio, smooth_fps=smooth_fps,
        )
    except (WorkerOffloadError, OSError, ValueError) as exc:
        _WORKER_TRANSCODE_GATE.release()
        raise HTTPException(status_code=503, detail="worker source unavailable") from exc
    except Exception:
        _WORKER_TRANSCODE_GATE.release()
        raise

    # A validates this signed handshake before proxying any bytes.
    # For content-integrity protection over untrusted LAN use real HTTPS.
    signature = response_signature(
        secret, request.headers["x-framedeck-nonce"], b"",
    )

    def release_stream():
        stream.close()
        _WORKER_TRANSCODE_GATE.release()

    return StreamingResponse(
        _aiter_transcode(stream), media_type="video/mp4",
        background=BackgroundTask(release_stream),
        headers={
            "X-FrameDeck-Stream-Proof": signature,
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


def _require_worker_hls(request: Request) -> str:
    """Every worker read is HMAC authenticated, including files and playlists."""
    secret = worker_secret()
    resource = request.url.path + (
        ("?" + request.url.query) if request.url.query else ""
    )
    if secret is None or not request.app.state.cluster_guard.verify(
        secret, "GET", resource, request.headers,
    ):
        raise HTTPException(status_code=403, detail="worker authentication required")
    return secret


@router.get("/worker/hls/start")
def worker_hls_start(
    request: Request,
    source_root: str = Query(..., min_length=1, max_length=2048),
    relative_path: str = Query(..., min_length=1, max_length=4096),
    profiles: str = Query(..., min_length=1, max_length=80),
    height: int = Query(0, ge=0, le=4320),
    start: float = Query(0.0, ge=0.0, le=1000000.0),
    session: str = Query("", max_length=64),
    services: Services = Depends(get_services),
) -> Response:
    """Start bounded existing HlsService on a read-only mapped NAS video."""
    secret = _require_worker_hls(request)
    requested = profiles.split(",")
    if not requested or len(requested) > 4 or any(p not in HLS_ALLOWED for p in requested):
        raise HTTPException(status_code=422, detail="invalid HLS profiles")
    try:
        source = resolve_mounted_video(source_root, relative_path)
        if not resolve_ffmpeg(False).available:
            raise WorkerOffloadError("ffmpeg unavailable")
        manifest = services.hls.ensure_async(
            str(source), profiles=requested, source_height=height,
            start_seconds=start, owner=session or None,
        )
    except (WorkerOffloadError, OSError, ValueError) as exc:
        raise HTTPException(status_code=503, detail="HLS worker unavailable") from exc
    except TranscodeError as exc:
        raise HTTPException(status_code=503, detail="HLS worker busy") from exc
    payload = json.dumps({"key": manifest.key}, separators=(",", ":")).encode("utf-8")
    return Response(payload, media_type="application/json", headers={
        "X-FrameDeck-Response-Signature": response_signature(
            secret, request.headers["x-framedeck-nonce"], payload,
        ),
        "Cache-Control": "no-store",
    })


@router.get("/worker/hls/file")
async def worker_hls_file(
    request: Request,
    key: str = Query(..., min_length=64, max_length=64),
    relative: str = Query(..., min_length=1, max_length=100),
    services: Services = Depends(get_services),
):
    """Serve ONLY a previously generated HLS playlist/init/segment, not raw media."""
    secret = _require_worker_hls(request)
    if not valid_hls_file(relative):
        raise HTTPException(status_code=404, detail="invalid HLS relative file")
    try:
        path = await _hls_wait_file(
            services, key, relative, timeout=7.0,
        )
    except HTTPException:
        raise
    return FileResponse(
        path,
        media_type=("application/vnd.apple.mpegurl"
                    if relative.endswith(".m3u8") else "video/mp4"),
        headers={
            "X-FrameDeck-Stream-Proof": response_signature(
                secret, request.headers["x-framedeck-nonce"], b"",
            ),
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )
