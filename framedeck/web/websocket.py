"""WebSocketイベント配信(/ws/events)。

サービス側スレッドからは publish_threadsafe() で通知できる。
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect


class EventBus:
    def __init__(self):
        self._clients: set[WebSocket] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    def attach_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self._clients.add(websocket)

    def disconnect(self, websocket: WebSocket) -> None:
        self._clients.discard(websocket)

    async def publish(self, event: str, data: Any = None) -> None:
        message = json.dumps({"event": event, "data": data},
                             ensure_ascii=False)
        dead = []
        for client in list(self._clients):
            try:
                await client.send_text(message)
            except Exception:
                dead.append(client)
        for client in dead:
            self._clients.discard(client)

    def publish_threadsafe(self, event: str, data: Any = None) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        asyncio.run_coroutine_threadsafe(self.publish(event, data), loop)


async def websocket_endpoint(websocket: WebSocket, bus: EventBus) -> None:
    await bus.connect(websocket)
    try:
        while True:
            # クライアントからの入力は現状ping用途のみ
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        bus.disconnect(websocket)
