"""Opt-in cluster worker pairing and health endpoints.

Worker capabilities are read-only and require an HMAC-authenticated request.
No job execution, arbitrary URL fetching, media mutation or remote input API
is exposed in this stage.
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Body, Depends, HTTPException, Request, Response

from ... import __version__
from ...cluster_worker import (
    CAPABILITY_PATH,
    PROTOCOL_VERSION,
    ClusterConfigError,
    response_signature,
    worker_secret,
)
from ...core.services import Services
from ...video.ffmpeg import resolve_ffmpeg
from ..dependencies import get_services
from .system import _require_same_origin

router = APIRouter(prefix="/api/cluster", tags=["cluster"])


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
    return {
        "protocol_version": PROTOCOL_VERSION,
        "worker_mode": worker_secret() is not None,
        "auto_discovery_enabled": services.discovery.enabled(),
        "peer": services.cluster.status(),
    }


@router.post("/discovery/enable")
def enable_auto_discovery(request: Request,
                          services: Services = Depends(get_services)) -> dict:
    """Enable trusted LAN discovery and reveal the shared key ONCE to admin."""
    _require_pairing_permission(request, services)
    return {"enabled": True, "secret": services.discovery.enable(),
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
    payload = json.dumps({
        "protocol_version": PROTOCOL_VERSION,
        "worker": True,
        "version": __version__,
        "transcode_available": bool(ffmpeg.available),
    }, separators=(",", ":")).encode("utf-8")
    signature = response_signature(
        secret, request.headers["x-framedeck-nonce"], payload,
    )
    return Response(
        content=payload, media_type="application/json",
        headers={"X-FrameDeck-Response-Signature": signature,
                 "Cache-Control": "no-store"},
    )
