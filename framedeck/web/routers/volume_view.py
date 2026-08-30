"""漫画フォルダの巻表示メタデータAPI。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from ...core.services import Services
from ...core.volume_view import parse_volume_descriptor, recommend_display_mode
from ..dependencies import get_services
from .library import _resolve_folder, _visible_items

router = APIRouter(prefix="/api/library", tags=["library"])


@router.get("/volume-view")
def volume_view(folder_id: str = Query(...),
                services: Services = Depends(get_services)) -> dict:
    folder = _resolve_folder(services, folder_id, "comic")
    items = _visible_items(services, folder, "comic")
    files = [item for item in items if item.media_type != "folder"]
    has_folders = any(item.media_type == "folder" for item in items)

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
