"""漫画フォルダの巻表示メタデータAPI。"""
from __future__ import annotations

import os

from fastapi import APIRouter, Depends, Query

from ...core.services import Services
from ...core.library_service import canonical_path
from ...core.volume_view import parse_volume_descriptor, recommend_display_mode
from ...models import comic_entry_id
from ..dependencies import get_services
from .library import _resolve_folder, _visible_items

router = APIRouter(prefix="/api/library", tags=["library"])


def _reading_summary(progress_rows: list[dict], entry_count: int) -> dict:
    if not progress_rows:
        return {
            "status": "unread",
            "completed": False,
            "progress_percent": 0,
            "page_count": None,
            "page_count_complete": False,
            "updated_at": None,
        }

    known_all_entries = entry_count > 0 and len(progress_rows) == entry_count
    page_count = sum(max(0, int(row.get("page_count") or 0)) for row in progress_rows)
    viewed_pages = sum(
        max(0, int(row.get("page_count") or 0))
        if row.get("completed")
        else min(
            max(0, int(row.get("page_count") or 0)),
            max(0, int(row.get("page_index") or 0)) + 1,
        )
        for row in progress_rows
    )
    completed = known_all_entries and all(bool(row.get("completed")) for row in progress_rows)
    progress_percent = None
    if known_all_entries and page_count > 0:
        progress_percent = 100 if completed else min(100, round(viewed_pages * 100 / page_count))
    return {
        "status": "completed" if completed else "reading",
        "completed": completed,
        "progress_percent": progress_percent,
        "page_count": page_count or None,
        "page_count_complete": known_all_entries,
        "updated_at": max(float(row.get("updated_at") or 0) for row in progress_rows),
    }


@router.get("/volume-view")
def volume_view(folder_id: str = Query(...),
                services: Services = Depends(get_services)) -> dict:
    folder = _resolve_folder(services, folder_id, "comic")
    items = _visible_items(services, folder, "comic")
    files = [item for item in items if item.media_type != "folder"]
    has_folders = any(item.media_type == "folder" for item in items)

    # A normal archive's entry id is derived without opening the archive. Only
    # recently opened physical items need discovery to account for nested
    # archives; scanning every volume here made a cold folder load take seconds.
    recent_item_ids = {
        row["media_id"] for row in services.storage.list_recent(100)
        if row.get("media_type") == "comic"
    }
    direct_id_by_item = {
        item.id: comic_entry_id(
            canonical_path(os.path.abspath(item.path)), (), None,
        )
        for item in files
    }
    progress_by_id = services.storage.get_reading_progress_many(
        list(direct_id_by_item.values())
    )
    entry_ids_by_item: dict[str, list[str]] = {}
    discovered_entry_ids: list[str] = []
    for item in files:
        direct_id = direct_id_by_item[item.id]
        if direct_id in progress_by_id or item.id not in recent_item_ids:
            entry_ids = [direct_id]
        else:
            entry_ids = [
                entry.id for entry in services.comic_engine.entries_for_item(item.path)
            ]
            discovered_entry_ids.extend(entry_ids)
        entry_ids_by_item[item.id] = entry_ids
    progress_by_id.update(
        services.storage.get_reading_progress_many(discovered_entry_ids)
    )

    parsed = []
    for item in files:
        descriptor = parse_volume_descriptor(item.display_name, path=item.path)
        parsed.append((item, descriptor))

    recommended, ratio = recommend_display_mode(
        [descriptor for _, descriptor in parsed], has_folders=has_folders,
    )
    entries = [
        {
            "item_id": item.id,
            "label": descriptor.label,
            "kind": descriptor.kind,
            "recognized": descriptor.recognized,
            "start": descriptor.start,
            "end": descriptor.end,
            "special": descriptor.special,
            "confidence": descriptor.confidence,
            "sort_key": list(descriptor.sort_key),
            "reading": _reading_summary(
                [
                    progress_by_id[entry_id]
                    for entry_id in entry_ids_by_item[item.id]
                    if entry_id in progress_by_id
                ],
                len(entry_ids_by_item[item.id]),
            ),
        }
        for item, descriptor in parsed
    ]
    # 巻表示ではユーザーの選択した通常ソートを無視し、論理巻順に固定する。
    entries.sort(key=lambda entry: tuple(entry["sort_key"]))
    return {
        "recommended_mode": recommended,
        "recognized_ratio": ratio,
        "has_folders": has_folders,
        "entries": entries,
    }
