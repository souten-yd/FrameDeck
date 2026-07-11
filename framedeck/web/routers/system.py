"""システム情報・設定API。"""
from __future__ import annotations

import shutil
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from ... import __version__
from ...comic.archive_backend import rar_backend_available
from ...core.services import Services
from ..dependencies import get_services

router = APIRouter(prefix="/api", tags=["system"])


@router.get("/health")
def health() -> dict:
    return {"status": "ok"}


@router.get("/system/info")
def system_info(services: Services = Depends(get_services)) -> dict:
    return {
        "name": "FrameDeck",
        "version": __version__,
        "tools": {
            "mpv": shutil.which("mpv") is not None,
            "ffmpeg": shutil.which("ffmpeg") is not None,
            "ffprobe": shutil.which("ffprobe") is not None,
            "rar_backend": rar_backend_available(),
        },
        "transcode_available": services.transcode.available(),
    }


@router.get("/settings")
def get_settings(services: Services = Depends(get_services)) -> dict:
    values = services.settings.as_dict()
    values.pop("web_pin", None)  # PINは公開しない
    return values


@router.put("/settings")
def put_settings(values: dict[str, Any],
                 services: Services = Depends(get_services)) -> dict:
    try:
        updated = services.settings.update(values)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    updated.pop("web_pin", None)
    return updated
