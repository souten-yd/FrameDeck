"""Worker pairing, capabilities and scoped read-only media conversion endpoints."""
from __future__ import annotations

import ipaddress
import json
import socket
import threading
from urllib.parse import urlsplit

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from starlette.background import BackgroundTask

from ...worker_sources import library_source, read_ticket
from ...video.stream import parse_range_header, RangeNotSatisfiable, iter_file_range, mime_for_video
from ... import __version__
from ...cluster_pairing import PairingError
from ...cluster_worker import (
    CAPABILITY_PATH,
    PROTOCOL_VERSION,
    ClusterConfigError,
    response_signature,
    worker_secret,
)
from ...core.services import Services
from ...worker_offload import (
    WORKER_FMP4_PATH, WorkerOffloadError,
    worker_root_fingerprints, root_fingerprint, offload_enabled,
)
from ...video.ffmpeg import resolve_ffmpeg
from ...worker_hls import (
    WORKER_HLS_FILE, WORKER_HLS_START, HLS_ALLOWED, valid_hls_file,
)
from ...video.hls_service import HLS_PROFILES
from ...video.transcode import TranscodeError
from ..dependencies import get_services
from .system import _require_same_origin
from ..lan_access import trusted_direct_lan
from .video import _aiter_transcode, _hls_wait_file

router = APIRouter(prefix="/api/cluster", tags=["cluster"])
_WORKER_TRANSCODE_GATE = threading.BoundedSemaphore(2)


def _require_pairing_permission(request: Request, services: Services) -> None:
    _require_same_origin(request)
    # Direct private LAN IP access is explicitly PIN-free. Every pairing
    # operation remains same-origin, short-lived and two-sided approved.
    lan = (services.settings.get("lan_pinless_access", True) is True
           and trusted_direct_lan(request))
    if not lan and not services.settings.get("web_pin", ""):
        raise HTTPException(
            status_code=403,
            detail="LANまたはVPNで開くかWeb PIN認証を有効にしてください",
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
        "parent": services.worker_sources.status(),
        "hls_performance": services.remote_hls.status(),
        "secure_pairing": {
            key: value for key, value in services.pairing.status().items()
            if key not in ("confirmation_code", "candidates",
                           "selected_url", "request_from")
        },
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
    parent_header = request.headers.get("x-framedeck-parent")
    if parent_header:
        try:
            services.worker_sources.accept_announcement(
                parent_header, request.headers.get("x-framedeck-parent-proof", ""),
                request.headers["x-framedeck-nonce"], secret,
                request.client.host if request.client else "",
            )
        except WorkerOffloadError as exc:
            raise HTTPException(status_code=403, detail="invalid parent identity") from exc
    roots = sorted(set(worker_root_fingerprints() + services.worker_sources.roots()))
    payload = json.dumps({
        "protocol_version": PROTOCOL_VERSION,
        "worker": True,
        "name": socket.gethostname(),
        "automatic_sources": True,
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
                source_url: str | None = Query(None, max_length=16384),
                relative_path: str = Query(..., min_length=1, max_length=4096),
                start: float = Query(0.0, ge=0.0, le=1000000.0),
                max_height: int | None = Query(None, ge=144, le=4320),
                max_width: int | None = Query(None, ge=144, le=7680),
                session: str = Query("", max_length=64),
                copy_video: bool = False,
                copy_audio: bool = False,
                tone_map: bool = False,
                smooth_fps: float | None = Query(None, ge=20.0, le=240.0),
                services: Services = Depends(get_services)):
    """Convert a read-only mount or a ticketed video from the paired parent."""
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
        source = services.worker_sources.resolve(source_root, relative_path, source_url, secret)
        if not resolve_ffmpeg(False).available:
            raise WorkerOffloadError("ffmpeg unavailable")
        stream = services.transcode.open_fmp4(
            str(source), start, max_height=max_height, max_width=max_width,
            owner=session or None, copy_video=copy_video,
            copy_audio=copy_audio, smooth_fps=smooth_fps,
            tone_map=tone_map,
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
    source_url: str | None = Query(None, max_length=16384),
    relative_path: str = Query(..., min_length=1, max_length=4096),
    profiles: str = Query(..., min_length=1, max_length=80),
    height: int = Query(0, ge=0, le=4320),
    start: float = Query(0.0, ge=0.0, le=1000000.0),
    session: str = Query("", max_length=64),
    tone_map: bool = False,
    services: Services = Depends(get_services),
) -> Response:
    """Start bounded HLS from a mount or an authenticated parent video."""
    secret = _require_worker_hls(request)
    requested = profiles.split(",")
    if not requested or len(requested) > 4 or any(p not in HLS_ALLOWED for p in requested):
        raise HTTPException(status_code=422, detail="invalid HLS profiles")
    try:
        source = services.worker_sources.resolve(source_root, relative_path, source_url, secret)
        if not resolve_ffmpeg(False).available:
            raise WorkerOffloadError("ffmpeg unavailable")
        manifest = services.hls.ensure_async(
            str(source), profiles=requested, source_height=height,
            start_seconds=start, owner=session or None,
            tone_map=tone_map,
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


@router.post("/pairing/start")
def pairing_start(request: Request,
                  services: Services = Depends(get_services)) -> dict:
    """Admin permits a strictly time-limited, no-secret LAN pairing window."""
    _require_pairing_permission(request, services)
    try:
        return services.pairing.arm()
    except PairingError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/pairing/status")
def pairing_status(request: Request,
                   services: Services = Depends(get_services)) -> dict:
    _require_pairing_permission(request, services)
    return services.pairing.status()


@router.post("/pairing/join")
def pairing_join(request: Request, data: dict = Body(...),
                 services: Services = Depends(get_services)) -> dict:
    """QNAP contacts ONLY an observed LAN candidate chosen in its UI."""
    _require_pairing_permission(request, services)
    if set(data) != {"id"} or not isinstance(data.get("id"), str):
        raise HTTPException(status_code=422, detail="Invalid candidate")
    try:
        return services.pairing.join(data["id"])
    except PairingError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/pairing/revoke")
def pairing_revoke(request: Request,
                   services: Services = Depends(get_services)) -> dict:
    """Admin explicitly resets Linux-only trust for a new bilateral pairing."""
    _require_pairing_permission(request, services)
    try:
        result = services.pairing.reset_linux()
        services.worker_sources.clear()
        return result
    except (PairingError, OSError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/pairing/approve")
def pairing_approve(request: Request,
                    services: Services = Depends(get_services)) -> dict:
    """Linux operator confirms the same short code shown on QNAP."""
    _require_pairing_permission(request, services)
    try:
        return services.pairing.approve_worker()
    except PairingError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/pairing/finish")
def pairing_finish(request: Request,
                   services: Services = Depends(get_services)) -> dict:
    """QNAP admin confirms the code and saves derived HMAC without seeing it."""
    _require_pairing_permission(request, services)
    try:
        result = services.pairing.finish()
        services.settings.set("cluster_offload_enabled", True)
        return result
    except (PairingError, ClusterConfigError, OSError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


async def _small_pairing_payload(request: Request) -> dict:
    if request.headers.get("content-type", "").split(";")[0].strip() != (
        "application/json"
    ):
        raise HTTPException(status_code=415, detail="JSON required")
    try:
        declared = int(request.headers.get("content-length", "0") or "0")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid content length") from exc
    if declared < 0 or declared > 512:
        raise HTTPException(status_code=413, detail="Oversize pairing request")
    raw = await request.body()
    if len(raw) > 512:
        raise HTTPException(status_code=413, detail="Oversize pairing request")
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON") from exc
    if not isinstance(value, dict):
        raise HTTPException(status_code=400, detail="Expected JSON object")
    return value


@router.post("/pairing/hello")
async def pairing_hello(request: Request,
                        services: Services = Depends(get_services)) -> dict:
    """Bounded anonymous handshake ONLY while a Linux admin opened pairing."""
    data = await _small_pairing_payload(request)
    if set(data) != {"id", "nas_pub"}:
        raise HTTPException(status_code=422, detail="Unexpected pairing fields")
    try:
        return services.pairing.hello(
            data["id"], data["nas_pub"],
            request.client.host if request.client else "",
        )
    except (PairingError, KeyError, TypeError) as exc:
        raise HTTPException(status_code=403, detail="Pairing unavailable") from exc


@router.post("/pairing/commit")
async def pairing_commit(request: Request,
                         services: Services = Depends(get_services)) -> dict:
    """HMAC proof, two local approvals, then no-key Linux persistence."""
    data = await _small_pairing_payload(request)
    if set(data) != {"id", "proof"}:
        raise HTTPException(status_code=422, detail="Unexpected pairing fields")
    try:
        return services.pairing.commit(
            data["id"], data["proof"],
            request.client.host if request.client else "",
        )
    except (PairingError, KeyError, TypeError, OSError) as exc:
        raise HTTPException(status_code=403, detail="Pairing unavailable") from exc


@router.api_route("/source", methods=["GET", "HEAD"])
def worker_source(request: Request, ticket: str = Query(..., max_length=12000),
                  services: Services = Depends(get_services)):
    """Serve only a ticketed library video to the currently paired worker."""
    peer = services.cluster.verified_peer()
    try:
        same_peer = bool(peer and request.client and
                         ipaddress.ip_address(request.client.host) ==
                         ipaddress.ip_address(urlsplit(peer["url"]).hostname))
    except ValueError:
        same_peer = False
    if not same_peer or not offload_enabled(services.settings):
        raise HTTPException(status_code=403, detail="paired worker required")
    try:
        data = read_ticket(peer["secret"], ticket)
        path = library_source(services.library.list_roots(), data["root"], data["relative"])
        stat = path.stat()
        if stat.st_size != data["size"] or stat.st_mtime_ns != data["mtime"]:
            raise WorkerOffloadError("source changed")
    except (WorkerOffloadError, OSError, ValueError) as exc:
        raise HTTPException(status_code=403, detail="source unavailable") from exc
    headers = {"Accept-Ranges": "bytes", "Cache-Control": "no-store",
               "X-Content-Type-Options": "nosniff"}
    try:
        span = parse_range_header(request.headers.get("range"), stat.st_size)
    except RangeNotSatisfiable:
        return Response(status_code=416, headers={"Content-Range": f"bytes */{stat.st_size}"})
    start, end = span if span else (0, stat.st_size - 1)
    headers["Content-Length"] = str(max(0, end - start + 1))
    if span:
        headers["Content-Range"] = f"bytes {start}-{end}/{stat.st_size}"
    if request.method == "HEAD":
        return Response(status_code=206 if span else 200, headers=headers)
    return StreamingResponse(iter_file_range(str(path), start, end),
                             status_code=206 if span else 200,
                             media_type=mime_for_video(str(path)), headers=headers)
