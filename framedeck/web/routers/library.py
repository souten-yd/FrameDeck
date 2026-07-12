"""ライブラリAPI。クライアントには内部IDのみ公開し、絶対パスは返さない。"""
from __future__ import annotations

import os

from fastapi import APIRouter, Body, Depends, HTTPException, Query

from ...core.security import PathValidationError
from ...core.services import Services
from ...models import MediaItem
from ..dependencies import get_services

router = APIRouter(prefix="/api/library", tags=["library"])


def _item_dict(item: MediaItem, root_path: str | None = None) -> dict:
    rel = None
    if root_path:
        try:
            rel = os.path.relpath(item.path, root_path)
        except ValueError:
            rel = None
    return {
        "id": item.id,
        "display_name": item.display_name,
        "media_type": item.media_type,
        "rating": item.rating,
        "stars": item.stars,
        "modified_at": item.modified_at,
        "size": item.size,
        "relative_path": rel,
    }


def _resolve_folder(services: Services, folder_id: str, mode: str | None = None) -> str:
    for root in services.library.list_roots():
        if mode and root["kind"] not in (mode, "any"):
            continue
        if root["id"] == folder_id:
            return root["path"]
    row = services.storage.get_media_item(folder_id)
    if row and os.path.isdir(row["path"]):
        root = _find_root_for(services, row["path"], mode)
        if root is None:
            raise HTTPException(status_code=404, detail="フォルダが見つかりません")
        return str(services.library.validate_path(row["path"]))
    raise HTTPException(status_code=404, detail="フォルダが見つかりません")


def _find_root_for(services: Services, path: str, kind: str | None = None) -> dict | None:
    best = None
    for root in services.library.list_roots():
        if kind and root["kind"] not in (kind, "any"):
            continue
        try:
            if os.path.commonpath([path, root["path"]]) == root["path"]:
                if (best is None or len(root["path"]) > len(best["path"])
                        or (kind and len(root["path"]) == len(best["path"])
                            and root["kind"] == kind and best["kind"] != kind)):
                    best = root
        except ValueError:
            continue
    return best


def _root_display_name(root: dict) -> str:
    return root.get("display_name") or os.path.basename(
        root["path"].rstrip(os.sep)
    ) or root["path"]


@router.get("/roots")
def list_roots(services: Services = Depends(get_services)) -> list[dict]:
    return [
        {"id": r["id"], "kind": r["kind"],
         "display_name": _root_display_name(r)}
        for r in services.library.list_roots()
    ]


@router.post("/roots")
def add_root(payload: dict = Body(...),
             services: Services = Depends(get_services)) -> dict:
    path = payload.get("path", "")
    kind = payload.get("kind", "any")
    if kind not in ("comic", "video", "any"):
        raise HTTPException(status_code=422, detail="kindが不正です")
    try:
        root = services.library.add_root(path, kind, payload.get("display_name"))
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except FileExistsError as e:
        raise HTTPException(status_code=409, detail=str(e))
    display_name = payload.get("display_name") or os.path.basename(
        root["path"].rstrip(os.sep)
    ) or root["path"]
    return {"id": root["id"], "kind": root["kind"],
            "display_name": display_name}


@router.patch("/roots/{root_id}")
def update_root(root_id: str, payload: dict = Body(...),
                services: Services = Depends(get_services)) -> dict:
    display_name = payload.get("display_name")
    services.library.update_root(root_id, display_name)
    roots = [root for root in services.library.list_roots() if root["id"] == root_id]
    if not roots:
        raise HTTPException(status_code=404, detail="ライブラリが見つかりません")
    root = roots[0]
    return {"id": root["id"], "kind": root["kind"],
            "display_name": _root_display_name(root)}


@router.delete("/roots/{root_id}")
def remove_root(root_id: str,
                services: Services = Depends(get_services)) -> dict:
    services.library.remove_root(root_id)
    return {"removed": root_id}


@router.get("/items")
def list_items(folder_id: str = Query(...),
               mode: str = Query("comic"),
               sort: str = Query("date"),
               filter: str = Query("all"),
               query: str = Query(""),
               services: Services = Depends(get_services)) -> dict:
    folder = _resolve_folder(services, folder_id, mode)
    try:
        items = services.library.list_folder(folder, mode=mode)
    except PathValidationError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"フォルダを読めません: {e}")

    if filter == "rated":
        items = [i for i in items if i.rating]
    elif filter == "unrated":
        items = [i for i in items if not i.rating]

    search = query.strip().lower()
    if search:
        items = [i for i in items if search in i.display_name.lower()]

    folders = [i for i in items if i.media_type == "folder"]
    files = [i for i in items if i.media_type != "folder"]
    if sort == "date":
        files.sort(key=lambda i: (-i.modified_at, i.display_name.lower()))
    elif sort == "rating_desc":
        files.sort(key=lambda i: (-(i.rating or 0), i.display_name.lower()))
    elif sort == "rating_asc":
        files.sort(key=lambda i: ((i.rating or 0), i.display_name.lower()))
    # name順はlist_folderの自然順のまま

    root = _find_root_for(services, folder, mode)
    root_path = root["path"] if root else None

    parent_id = None
    parent = os.path.dirname(folder.rstrip(os.sep))
    if root_path and folder.rstrip(os.sep) != root_path.rstrip(os.sep):
        try:
            services.library.validate_path(parent)
            parent_items = services.library.list_folder(
                os.path.dirname(parent) or parent, mode=mode,
                enforce_roots=False,
            )
            from ...core.library_service import item_id_for
            parent_id = item_id_for(parent)
            # 親フォルダをIDで辿れるようDBへ登録しておく
            services.storage.upsert_media_item(
                parent_id, os.path.abspath(parent), "folder", None,
                os.path.getmtime(parent), None,
            )
            del parent_items
        except (PathValidationError, OSError):
            parent_id = None

    folder_name = os.path.basename(folder.rstrip(os.sep)) or folder
    rel = os.path.relpath(folder, root_path) if root_path else folder_name
    return {
        "folder": {
            "id": folder_id,
            "display_name": folder_name,
            "relative_path": "" if rel == "." else rel,
            "root_id": root["id"] if root else None,
            "parent_id": parent_id,
        },
        "items": [_item_dict(i, root_path) for i in folders + files],
        "total": len(items),
        "rated": sum(1 for i in items if i.rating),
    }


@router.get("/items/{item_id}")
def get_item(item_id: str,
             services: Services = Depends(get_services)) -> dict:
    item = services.library.get_item(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="項目が見つかりません")
    return _item_dict(item)


@router.post("/items/{item_id}/rating")
def set_rating(item_id: str, payload: dict = Body(...),
               services: Services = Depends(get_services)) -> dict:
    rating = payload.get("rating")
    if rating is not None and rating not in (1, 2, 3, 4, 5):
        raise HTTPException(status_code=422, detail="評価は1〜5またはnullです")
    try:
        item = services.library.set_rating(item_id, rating)
    except KeyError:
        raise HTTPException(status_code=404, detail="項目が見つかりません")
    except FileExistsError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return _item_dict(item)


@router.post("/items/{item_id}/delete-request")
def delete_request(item_id: str,
                   services: Services = Depends(get_services)) -> dict:
    item = services.library.get_item(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="項目が見つかりません")
    token = services.confirm_tokens.issue(f"delete:{item_id}")
    return {
        "token": token,
        "display_name": item.display_name,
        "media_type": item.media_type,
        "expires_in": 60,
        "to_trash": bool(services.settings.get("delete_to_trash", True)),
    }


@router.delete("/items/{item_id}")
def delete_item(item_id: str, token: str = Query(...),
                services: Services = Depends(get_services)) -> dict:
    if not services.confirm_tokens.consume(token, f"delete:{item_id}"):
        raise HTTPException(
            status_code=403,
            detail="確認トークンが無効です。削除確認をやり直してください。",
        )
    try:
        return services.library.delete_item(item_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="項目が見つかりません")
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"削除に失敗しました: {e}")


@router.get("/recent")
def recent(services: Services = Depends(get_services)) -> list[dict]:
    result = []
    for row in services.library.list_recent():
        item = services.library.get_item(row["media_id"])
        if item is not None:
            result.append(_item_dict(item))
    return result
