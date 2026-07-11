"""漫画リーダーAPI。ComicReaderEngineの薄いHTTPアダプター。"""
from __future__ import annotations

import os

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, Response

from ...comic.archive_backend import ArchiveError
from ...comic.reader_engine import ComicEngineError
from ...core.security import PathValidationError
from ...core.services import Services
from ..dependencies import get_services

router = APIRouter(prefix="/api/comics", tags=["comic"])

PAGE_CACHE_HEADERS = {"Cache-Control": "private, max-age=86400"}


def _entry_dict(entry) -> dict:
    return {
        "id": entry.id,
        "label": entry.label,
        "source_type": entry.source_type,
    }


@router.get("/{item_id}/entries")
def list_entries(item_id: str,
                 services: Services = Depends(get_services)) -> dict:
    item = services.library.get_item(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="項目が見つかりません")
    try:
        services.library.validate_path(item.path)
        entries = services.comic_engine.entries_for_item(item.path)
    except PathValidationError as e:
        raise HTTPException(status_code=403, detail=str(e))
    return {"item_id": item_id,
            "entries": [_entry_dict(e) for e in entries]}


@router.post("/session")
def create_session(payload: dict = Body(...),
                   services: Services = Depends(get_services)) -> dict:
    item_id = payload.get("item_id")
    entry_id = payload.get("entry_id")
    restore = bool(payload.get("restore_progress", True))
    item = services.library.get_item(item_id) if item_id else None
    if item is None:
        raise HTTPException(status_code=404, detail="項目が見つかりません")
    try:
        services.library.validate_path(item.path)
    except PathValidationError as e:
        raise HTTPException(status_code=403, detail=str(e))

    if entry_id is None:
        entries = services.comic_engine.entries_for_item(item.path)
        if not entries:
            raise HTTPException(
                status_code=422,
                detail="開ける画像または圧縮漫画が見つかりませんでした。",
            )
        if len(entries) > 1:
            # 複数候補: クライアント側で選択モーダルを出す
            return {
                "requires_choice": True,
                "entries": [_entry_dict(e) for e in entries],
            }
        entry_id = entries[0].id

    root_folder = os.path.dirname(item.path.rstrip(os.sep))
    try:
        state = services.comic_engine.create_session(
            root_folder, entry_id, restore_progress=restore,
            item_path=item.path,
        )
    except (ComicEngineError, ArchiveError) as e:
        raise HTTPException(status_code=422, detail=str(e))
    services.library.mark_opened(item)
    return state.to_dict()


def _engine_call(services: Services, func, *args) -> dict:
    try:
        return func(*args).to_dict()
    except ComicEngineError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ArchiveError as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.get("/session/{session_id}")
def get_session(session_id: str,
                services: Services = Depends(get_services)) -> dict:
    return _engine_call(services, services.comic_engine.get_state, session_id)


@router.delete("/session/{session_id}")
def close_session(session_id: str,
                  services: Services = Depends(get_services)) -> dict:
    services.comic_engine.close_session(session_id)
    return {"closed": session_id}


@router.post("/session/{session_id}/next-page")
def next_page(session_id: str, payload: dict = Body(default={}),
              services: Services = Depends(get_services)) -> dict:
    engine = services.comic_engine
    if payload.get("unit") == "single":
        return _engine_call(services, engine.next_page, session_id)
    return _engine_call(services, engine.next_spread, session_id)


@router.post("/session/{session_id}/previous-page")
def previous_page(session_id: str, payload: dict = Body(default={}),
                  services: Services = Depends(get_services)) -> dict:
    engine = services.comic_engine
    if payload.get("unit") == "single":
        return _engine_call(services, engine.previous_page, session_id)
    return _engine_call(services, engine.previous_spread, session_id)


@router.post("/session/{session_id}/next-entry")
def next_entry(session_id: str,
               services: Services = Depends(get_services)) -> dict:
    return _engine_call(services, services.comic_engine.next_entry, session_id)


@router.post("/session/{session_id}/previous-entry")
def previous_entry(session_id: str,
                   services: Services = Depends(get_services)) -> dict:
    return _engine_call(services, services.comic_engine.previous_entry,
                        session_id)


@router.post("/session/{session_id}/open-entry")
def open_entry(session_id: str, payload: dict = Body(...),
               services: Services = Depends(get_services)) -> dict:
    entry_id = payload.get("entry_id")
    if not entry_id:
        raise HTTPException(status_code=422, detail="entry_idが必要です")
    return _engine_call(services, services.comic_engine.open_entry,
                        session_id, entry_id)


@router.post("/session/{session_id}/goto")
def goto(session_id: str, payload: dict = Body(...),
         services: Services = Depends(get_services)) -> dict:
    page_index = payload.get("page_index")
    if not isinstance(page_index, int):
        raise HTTPException(status_code=422, detail="page_indexが必要です")
    return _engine_call(services, services.comic_engine.goto_page,
                        session_id, page_index)


@router.patch("/session/{session_id}/options")
def set_options(session_id: str, payload: dict = Body(...),
                services: Services = Depends(get_services)) -> dict:
    try:
        state = services.comic_engine.set_view_options(
            session_id,
            view_mode=payload.get("view_mode"),
            reading_direction=payload.get("reading_direction"),
            cover_as_single_page=payload.get("cover_as_single_page"),
        )
    except ComicEngineError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return state.to_dict()


def _image_response(request: Request, data: bytes, mime: str,
                    etag: str) -> Response:
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304,
                        headers={**PAGE_CACHE_HEADERS, "ETag": etag})
    return Response(
        content=data, media_type=mime,
        headers={**PAGE_CACHE_HEADERS, "ETag": etag},
    )


@router.get("/session/{session_id}/page/{page_index}")
def get_page(session_id: str, page_index: int, request: Request,
             w: int | None = Query(default=None, ge=64, le=8192),
             h: int | None = Query(default=None, ge=64, le=8192),
             services: Services = Depends(get_services)) -> Response:
    try:
        data, mime, etag = services.comic_engine.render_page(
            session_id, page_index, w, h
        )
    except ComicEngineError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except (ArchiveError, OSError) as e:
        raise HTTPException(status_code=422, detail=f"ページ読み込み失敗: {e}")
    return _image_response(request, data, mime, etag)


@router.get("/session/{session_id}/thumbnail/{page_index}")
def get_thumbnail(session_id: str, page_index: int, request: Request,
                  size: int = Query(default=320, ge=64, le=1024),
                  services: Services = Depends(get_services)) -> Response:
    try:
        data, mime, etag = services.comic_engine.render_thumbnail(
            session_id, page_index, size
        )
    except ComicEngineError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except (ArchiveError, OSError) as e:
        raise HTTPException(status_code=422, detail=f"サムネイル生成失敗: {e}")
    return _image_response(request, data, mime, etag)
