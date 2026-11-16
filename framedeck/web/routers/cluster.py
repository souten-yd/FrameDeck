"""Opt-in cluster worker pairing and health endpoints.

Worker capabilities are read-only and require an HMAC-authenticated request.
No job execution, arbitrary URL fetching, media mutation or remote input API
is exposed in this stage.
"""
from __future__ import annotations

import json
import threading

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
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
    worker_root_fingerprints,
)
from ...video.ffmpeg import resolve_ffmpeg
from ..dependencies import get_services
from .system import _require_same_origin
from .video import _aiter_transcode

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
    peer["offload_active"] = bool(
        peer["online"] and peer["capabilities"] and
        peer["capabilities"].get("remote_fmp4") is True and
        (services.settings.get("cluster_offload_enabled", False) is True or
         peer["offload_active"])
    )
    return {
        "protocol_version": PROTOCOL_VERSION,
        "worker_mode": worker_secret() is not None,
        "peer": peer,
    }


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
