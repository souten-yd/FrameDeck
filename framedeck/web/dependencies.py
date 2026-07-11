"""FastAPI依存性。app.state経由でサービスコンテナへアクセスする。"""
from __future__ import annotations

from fastapi import Request

from ..core.services import Services


def get_services(request: Request) -> Services:
    return request.app.state.services


def get_event_bus(request: Request):
    return request.app.state.event_bus
