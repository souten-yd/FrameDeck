"""ライブラリAPI。クライアントには内部IDのみ公開し、絶対パスは返さない。"""
from __future__ import annotations

import os

from fastapi import APIRouter, Body, Depends, HTTPException, Query

from ...core import rating_service as rs
from ...core.comic_edition import is_color_edition
from ...core.duplicate_finder import (
    DEFAULT_MAX_DISTANCE,
    DuplicateCandidate,
    find_duplicate_groups,
    stale_duplicate_ids,
)
from ...core.security import PathValidationError
from ...core.services import Services
from ...models import MediaItem
from ..dependencies import get_services

router = APIRouter(prefix="/api/library", tags=["library"])

#: 並び順キー。旧クライアント互換のため date / name も受け付ける。
SORT_ALIASES = {"date": "date_desc", "name": "name_asc"}
SORT_KEYS = {
    "date_desc", "date_asc", "name_asc", "name_desc",
    "rating_desc", "rating_asc",
}


def _sorted_items(items: list[MediaItem], sort: str) -> list[MediaItem]:
    """フォルダ・ファイルを同じ規則で並べ替える(フォルダを先頭に置く)。"""
    sort = SORT_ALIASES.get(sort, sort)
    if sort not in SORT_KEYS:
        sort = "date_desc"
    descending = sort.endswith("_desc")
    ordered = sorted(items, key=lambda i: rs.natural_key(i.display_name))
    if sort.startswith("date"):
        ordered.sort(key=lambda i: i.modified_at, reverse=descending)
    elif sort.startswith("rating"):
        ordered.sort(key=lambda i: (i.rating or 0), reverse=descending)
    elif descending:  # name_desc
        ordered.reverse()
    folders = [i for i in ordered if i.media_type == "folder"]
    files = [i for i in ordered if i.media_type != "folder"]
    return folders + files


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
        "color_edition": (
            item.media_type in ("comic", "folder")
            and is_color_edition(item.display_name)
        ),
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


def _visible_items(services: Services, folder: str, mode: str,
                   filter_: str = "all", query: str = "") -> list[MediaItem]:
    try:
        items = services.library.list_folder(folder, mode=mode)
    except PathValidationError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"フォルダを読めません: {e}")

    if filter_ == "rated":
        items = [i for i in items if i.rating]
    elif filter_ == "unrated":
        items = [i for i in items if not i.rating]

    search = query.strip().lower()
    if search:
        items = [i for i in items if search in i.display_name.lower()]
    return items


@router.get("/items")
def list_items(folder_id: str = Query(...),
               mode: str = Query("comic"),
               sort: str = Query("date_desc"),
               filter: str = Query("all"),
               query: str = Query(""),
               services: Services = Depends(get_services)) -> dict:
    folder = _resolve_folder(services, folder_id, mode)
    items = _visible_items(services, folder, mode, filter, query)
    ordered = _sorted_items(items, sort)

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
    _remember_folder(services, mode, folder_id, folder,
                     root["id"] if root else None)
    return {
        "folder": {
            "id": folder_id,
            "display_name": folder_name,
            "relative_path": "" if rel == "." else rel,
            "root_id": root["id"] if root else None,
            "parent_id": parent_id,
        },
        "items": [_item_dict(i, root_path) for i in ordered],
        "total": len(items),
        "rated": sum(1 for i in items if i.rating),
    }


def _last_folder_key(mode: str) -> str:
    return f"last_folder.{mode}"


def _remember_folder(services: Services, mode: str, folder_id: str,
                     folder: str, root_id: str | None) -> None:
    """最後に開いたフォルダを記録する(再接続時の再開用)。"""
    if mode not in ("comic", "video"):
        return
    services.storage.set_state(_last_folder_key(mode), {
        "folder_id": folder_id,
        "path": os.path.abspath(folder),
        "root_id": root_id,
    })


@router.get("/start-folder")
def start_folder(mode: str = Query("comic"),
                 services: Services = Depends(get_services)) -> dict:
    """起動時に開くフォルダ。復元できない場合は folder_id が null。

    設定 `library_start_folder` が "root" のときは常に null を返し、
    クライアントはライブラリのルートを開く。
    """
    if services.settings.get("library_start_folder", "last") != "last":
        return {"folder_id": None, "root_id": None, "reason": "root"}
    state = services.storage.get_state(_last_folder_key(mode)) or {}
    path = state.get("path")
    if not path or not os.path.isdir(path):
        return {"folder_id": None, "root_id": None, "reason": "missing"}
    root = _find_root_for(services, path, mode)
    if root is None:
        return {"folder_id": None, "root_id": None, "reason": "outside-root"}

    from ...core.library_service import item_id_for
    if os.path.abspath(path).rstrip(os.sep) == root["path"].rstrip(os.sep):
        folder_id = root["id"]
    else:
        folder_id = item_id_for(path)
        # 再起動後もIDから辿れるようDBへ登録し直す
        try:
            services.storage.upsert_media_item(
                folder_id, os.path.abspath(path), "folder", None,
                os.path.getmtime(path), None,
            )
        except OSError:
            return {"folder_id": None, "root_id": None, "reason": "missing"}
    rel = os.path.relpath(path, root["path"])
    return {
        "folder_id": folder_id,
        "root_id": root["id"],
        "relative_path": "" if rel == "." else rel,
        "reason": "last",
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


@router.get("/duplicates")
def duplicates(folder_id: str = Query(...),
               mode: str = Query("comic"),
               filter: str = Query("all"),
               query: str = Query(""),
               max_distance: int = Query(DEFAULT_MAX_DISTANCE, ge=0, le=5),
               services: Services = Depends(get_services)) -> dict:
    """表示中フォルダ内の重複候補を返す(削除はしない)。

    拡張子と評価タグを除いた名前の編集距離が `max_distance` 以下で、
    かつ名前に含まれる数値が一致する項目を同一グループとみなし、
    各グループの最新1件以外(=更新日が古い方)を選択候補にする。
    """
    folder = _resolve_folder(services, folder_id, mode)
    items = _visible_items(services, folder, mode, filter, query)
    by_id = {i.id: i for i in items}
    groups = find_duplicate_groups(
        [
            DuplicateCandidate(
                id=i.id, display_name=i.display_name,
                modified_at=i.modified_at, media_type=i.media_type,
                rating=i.rating, size=i.size,
            )
            for i in items
        ],
        max_distance=max_distance,
    )
    return {
        "max_distance": max_distance,
        "select_ids": stale_duplicate_ids(groups),
        "groups": [
            {
                "items": [
                    {**_item_dict(by_id[c.id]), "keep": index == 0}
                    for index, c in enumerate(group)
                ],
            }
            for group in groups
        ],
    }


@router.post("/items/bulk-delete-request")
def bulk_delete_request(payload: dict = Body(...),
                        services: Services = Depends(get_services)) -> dict:
    item_ids = [str(i) for i in (payload.get("item_ids") or [])]
    if not item_ids:
        raise HTTPException(status_code=422, detail="削除対象がありません")
    names: list[str] = []
    valid: list[str] = []
    for item_id in item_ids:
        item = services.library.get_item(item_id)
        if item is None:
            continue
        valid.append(item.id)
        names.append(item.display_name)
    if not valid:
        raise HTTPException(status_code=404, detail="項目が見つかりません")
    token = services.confirm_tokens.issue(f"bulk-delete:{','.join(sorted(valid))}")
    return {
        "token": token,
        "item_ids": valid,
        "display_names": names,
        "count": len(valid),
        "expires_in": 60,
        "to_trash": bool(services.settings.get("delete_to_trash", True)),
    }


@router.post("/items/bulk-delete")
def bulk_delete(token: str = Query(...), payload: dict = Body(...),
                services: Services = Depends(get_services)) -> dict:
    item_ids = [str(i) for i in (payload.get("item_ids") or [])]
    subject = f"bulk-delete:{','.join(sorted(item_ids))}"
    if not item_ids or not services.confirm_tokens.consume(token, subject):
        raise HTTPException(
            status_code=403,
            detail="確認トークンが無効です。削除確認をやり直してください。",
        )
    return services.library.delete_items(item_ids)


@router.get("/recent")
def recent(services: Services = Depends(get_services)) -> list[dict]:
    result = []
    for row in services.library.list_recent():
        item = services.library.get_item(row["media_id"])
        if item is not None:
            result.append(_item_dict(item))
    return result
