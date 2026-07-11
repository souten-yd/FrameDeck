"""FastAPIアプリケーションファクトリ。"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Response, WebSocket
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from ..core.services import Services
from .routers import comic, library, system, video
from .websocket import EventBus, websocket_endpoint

STATIC_DIR = Path(__file__).parent / "static"
TEMPLATE_DIR = Path(__file__).parent / "templates"

_LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost", "testclient"}


def create_app(services: Services) -> FastAPI:
    event_bus = EventBus()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        event_bus.attach_loop(asyncio.get_running_loop())
        yield
        services.shutdown()

    app = FastAPI(title="FrameDeck", lifespan=lifespan)
    app.state.services = services
    app.state.event_bus = event_bus

    @app.middleware("http")
    async def pin_guard(request: Request, call_next):
        """LANアクセス時の簡易PIN(設定時のみ)。localhostは常に許可。"""
        pin = services.settings.get("web_pin", "")
        if pin:
            client_host = request.client.host if request.client else ""
            if client_host not in _LOCAL_HOSTS:
                supplied = (request.headers.get("x-framedeck-pin")
                            or request.query_params.get("pin")
                            or request.cookies.get("framedeck_pin"))
                if supplied != pin:
                    return JSONResponse(
                        status_code=401,
                        content={"detail": "PINが必要です"},
                    )
        response = await call_next(request)
        if pin and request.query_params.get("pin") == pin:
            response.set_cookie("framedeck_pin", pin, httponly=True)
        return response

    app.include_router(system.router)
    app.include_router(library.router)
    app.include_router(comic.router)
    app.include_router(video.router)

    @app.websocket("/ws/events")
    async def ws_events(websocket: WebSocket):
        await websocket_endpoint(websocket, event_bus)

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)),
              name="static")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(TEMPLATE_DIR / "index.html",
                            media_type="text/html")

    @app.get("/manifest.webmanifest", include_in_schema=False)
    def manifest() -> FileResponse:
        return FileResponse(STATIC_DIR / "manifest.webmanifest",
                            media_type="application/manifest+json")

    @app.get("/sw.js", include_in_schema=False)
    def service_worker() -> FileResponse:
        return FileResponse(STATIC_DIR / "js" / "sw.js",
                            media_type="application/javascript")

    return app
