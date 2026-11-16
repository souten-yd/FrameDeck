"""システム情報・設定API。"""
from __future__ import annotations

import shutil
import hmac
from fastapi.responses import JSONResponse
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request, status

from ... import __version__
from ...comic.archive_backend import rar_backend_available
from ...core.services import Services
from ...core.update_service import UpdateError, get_update_manager
from ...runtime_profile import qnap_lite
from ...video.ffmpeg import resolve_ffmpeg, system_ffprobe_path
from ..dependencies import get_services

router = APIRouter(prefix="/api", tags=["system"])


def _require_same_origin(request: Request) -> None:
    """Reject browser cross-site requests for restart/install operations."""
    if request.headers.get("sec-fetch-site", "").lower() == "cross-site":
        raise HTTPException(status_code=403, detail="クロスサイトからの更新要求を拒否しました")
    origin = request.headers.get("origin")
    host = request.headers.get("host")
    if origin and host:
        try:
            origin_host = urlsplit(origin).netloc
        except ValueError:
            origin_host = ""
        if not origin_host or origin_host.lower() != host.lower():
            raise HTTPException(status_code=403, detail="更新要求のOriginが一致しません")


@router.post("/auth/pin")
def authenticate_web_pin(payload: dict[str, Any], request: Request,
                         services: Services = Depends(get_services)):
    """Authenticate a browser without placing the PIN in URL, JS storage or logs.

    The existing FrameDeck middleware uses an HttpOnly cookie containing the
    configured Web PIN. The login endpoint alone must bypass that middleware
    and is protected by same-origin checks and a bounded memory-only limiter.
    """
    _require_same_origin(request)
    limiter = request.app.state.pin_login_limiter
    client = request.client.host if request.client else "unknown"
    if not limiter.allow(client):
        raise HTTPException(status_code=429, detail="PIN試行回数が多すぎます")
    provided = payload.get("pin")
    saved = services.settings.get("web_pin", "")
    if not isinstance(saved, str) or not saved:
        raise HTTPException(status_code=403, detail="Web PINが設定されていません")
    if (not isinstance(provided, str) or not 1 <= len(provided) <= 256 or
            not hmac.compare_digest(provided.encode("utf-8"), saved.encode("utf-8"))):
        limiter.failed(client)
        raise HTTPException(status_code=401, detail="Web PINが違います")
    limiter.success(client)
    result = JSONResponse({"authenticated": True})
    result.set_cookie(
        "framedeck_pin", saved, httponly=True, samesite="strict",
        secure=request.url.scheme == "https", max_age=7 * 24 * 3600,
    )
    return result


@router.get("/health")
def health() -> dict:
    return {"status": "ok"}


@router.get("/system/info")
def system_info(services: Services = Depends(get_services)) -> dict:
    ffmpeg = resolve_ffmpeg(bool(services.settings.get("video_ffmpeg_auto_download", True)))
    return {
        "name": "FrameDeck",
        "version": __version__,
        "tools": {
            "mpv": shutil.which("mpv") is not None,
            "ffmpeg": ffmpeg.available,
            "ffmpeg_source": ffmpeg.source,
            "ffmpeg_error": ffmpeg.error,
            "ffprobe": system_ffprobe_path() is not None,
            "rar_backend": rar_backend_available(),
        },
        "transcode_available": services.transcode.available(),
    }


@router.get("/settings")
def get_settings(services: Services = Depends(get_services)) -> dict:
    values = services.settings.as_dict()
    values.pop("web_pin", None)  # PINは公開しない
    values["runtime_profile"] = "qnap-lite" if qnap_lite() else "standard"
    return values


@router.put("/settings")
def put_settings(values: dict[str, Any], request: Request,
                 services: Services = Depends(get_services)) -> dict:
    if "backup_enabled" in values:
        # Turning on scheduled disk writes requires a protected local/admin
        # request. Existing ordinary settings keep their current semantics.
        _require_same_origin(request)
        client = request.client.host if request.client else ""
        local = client == "testclient" or (
            client in ("127.0.0.1", "::1", "localhost") and
            request.url.hostname in ("127.0.0.1", "::1", "localhost")
        )
        if not local and not services.settings.get("web_pin", ""):
            raise HTTPException(
                status_code=403,
                detail="バックアップを設定するにはWeb PINを有効にしてください",
            )
    try:
        updated = services.settings.update(values)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    updated.pop("web_pin", None)
    updated["runtime_profile"] = "qnap-lite" if qnap_lite() else "standard"
    return updated


@router.get("/update/status")
def update_status(services: Services = Depends(get_services)) -> dict:
    """Return local updater/job state without contacting GitHub."""
    return get_update_manager(services.paths).job_status()


@router.get("/update/check")
def update_check(services: Services = Depends(get_services)) -> dict:
    """Check the latest stable GitHub Release and resolve this platform's asset."""
    try:
        return get_update_manager(services.paths).check()
    except UpdateError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/update/apply", status_code=status.HTTP_202_ACCEPTED)
def update_apply(request: Request,
                 services: Services = Depends(get_services)) -> dict:
    """Download, validate and install the latest compatible stable Release."""
    _require_same_origin(request)
    try:
        return get_update_manager(services.paths).start_update()
    except UpdateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
