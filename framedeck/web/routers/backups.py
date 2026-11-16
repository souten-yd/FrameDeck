"""Metadata-only backup status, verified manual snapshot, and check endpoints.

No remote restore, delete, or arbitrary backup paths are accepted.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from ...core.backup_service import (
    BackupError, DAILY_GENERATIONS, WEEKLY_GENERATIONS,
)
from ...core.services import Services
from ..dependencies import get_services
from .system import _require_same_origin

router = APIRouter(prefix="/api/backups", tags=["backups"])


def _require_backup_admin(request: Request, services: Services) -> None:
    _require_same_origin(request)
    client = request.client.host if request.client else ""
    hostname = request.url.hostname
    local = client == "testclient" or (
        client in ("127.0.0.1", "::1", "localhost") and
        hostname in ("127.0.0.1", "::1", "localhost")
    )
    if not local and not services.settings.get("web_pin", ""):
        raise HTTPException(
            status_code=403,
            detail="LANからバックアップ管理するにはWeb PINを設定してください",
        )


@router.get("/status")
def backup_status(services: Services = Depends(get_services)) -> dict:
    return {
        "enabled": services.settings.get("backup_enabled", False) is True,
        "daily_generations": DAILY_GENERATIONS,
        "weekly_generations": WEEKLY_GENERATIONS,
        "scope": "sqlite_and_configuration_only",
        "snapshots": services.backups.list_snapshots(),
    }


@router.post("/create")
def create_backup(request: Request,
                  services: Services = Depends(get_services)) -> dict:
    _require_backup_admin(request, services)
    try:
        return services.backups.create_snapshot()
    except BackupError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/{name}/verify")
def verify_backup(name: str, request: Request,
                  services: Services = Depends(get_services)) -> dict:
    _require_backup_admin(request, services)
    try:
        return services.backups.verify(name)
    except BackupError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
