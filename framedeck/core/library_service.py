"""ライブラリ(フォルダ・メディア項目)管理サービス。

Web APIにはファイルパスを直接公開せず、内部ID(canonical pathのハッシュ)
だけを公開する。IDは評価タグ `{zpi$r=N}` を除いた正規化パスから計算する
ため、評価の変更(リネーム)ではIDが変化しない。
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import time
from pathlib import Path

from ..config import COMIC_EXTENSIONS, VIDEO_EXTENSIONS, Settings
from ..models import MediaItem, media_id_for_path
from . import rating_service as rs
from .security import PathValidationError, resolve_within_roots
from .storage import Storage

try:
    from send2trash import send2trash
except ImportError:  # pragma: no cover
    send2trash = None


def canonical_path(path: str) -> str:
    """評価タグを除いた正規化パス(ID計算用)。"""
    folder = os.path.dirname(path.rstrip(os.sep))
    return os.path.join(folder, rs.display_name_of_path(path))


def item_id_for(path: str) -> str:
    return media_id_for_path(canonical_path(os.path.abspath(path)))


def root_id_for(path: str, kind: str) -> str:
    raw = f"{kind}\0{os.path.abspath(path)}"
    return hashlib.sha1(raw.encode("utf-8", "surrogatepass")).hexdigest()


class LibraryService:
    def __init__(self, storage: Storage, settings: Settings,
                 rating: rs.RatingService):
        self._storage = storage
        self._settings = settings
        self._rating = rating
        self._ensure_default_roots()

    # ---------------- roots ----------------

    def _ensure_default_roots(self) -> None:
        existing = {(r["path"], r["kind"]) for r in self._storage.list_roots()}
        for key, kind in (("default_folder_comic", "comic"),
                          ("default_folder_video", "video")):
            path = self._settings.get(key)
            if not path or not os.path.isdir(path):
                continue
            resolved = str(Path(path).resolve())
            if (resolved, kind) not in existing:
                self.add_root(resolved, kind)

    def list_roots(self) -> list[dict]:
        return self._storage.list_roots()

    def root_paths(self) -> list[str]:
        return [r["path"] for r in self._storage.list_roots()]

    def add_root(self, path: str, kind: str = "any", display_name: str | None = None) -> dict:
        resolved = str(Path(path).resolve())
        if not os.path.isdir(resolved):
            raise FileNotFoundError(f"フォルダが見つかりません: {path}")
        root_id = root_id_for(resolved, kind)
        try:
            self._storage.add_root(root_id, resolved, kind, display_name)
        except sqlite3.IntegrityError as exc:
            raise FileExistsError(
                f"同じ{kind}ライブラリルートは既に登録されています: {path}"
            ) from exc
        return {"id": root_id, "path": resolved, "kind": kind, "display_name": display_name}

    def update_root(self, root_id: str, display_name: str | None = None) -> None:
        self._storage.update_root(root_id, display_name)

    def remove_root(self, root_id: str) -> None:
        self._storage.remove_root(root_id)

    def validate_path(self, path: str) -> Path:
        """登録済みルート配下であることを検証する(Web経由アクセス用)。"""
        return resolve_within_roots(path, self.root_paths())

    # ---------------- listing ----------------

    def _make_item(self, path: str) -> MediaItem | None:
        try:
            stat = os.stat(path)
        except OSError:
            return None
        is_dir = os.path.isdir(path)
        ext = os.path.splitext(path)[1].lower()
        if is_dir:
            media_type = "folder"
        elif ext in VIDEO_EXTENSIONS:
            media_type = "video"
        elif ext in COMIC_EXTENSIONS:
            media_type = "comic"
        else:
            return None
        item = MediaItem(
            id=item_id_for(path),
            path=os.path.abspath(path),
            display_name=rs.display_name_of_path(path),
            media_type=media_type,
            source_kind="directory" if is_dir else "file",
            rating=rs.rating_of_path(path),
            modified_at=stat.st_mtime,
            size=None if is_dir else stat.st_size,
        )
        self._storage.upsert_media_item(
            item.id, item.path, item.media_type, item.rating,
            item.modified_at, item.size,
        )
        return item

    def list_folder(self, folder: str, mode: str = "comic",
                    enforce_roots: bool = True) -> list[MediaItem]:
        """フォルダ直下の項目一覧。mode=comicはフォルダ+漫画、videoはフォルダ+動画。"""
        if enforce_roots:
            folder = str(self.validate_path(folder))
        items: list[MediaItem] = []
        for name in sorted(os.listdir(folder), key=rs.natural_key):
            full = os.path.join(folder, name)
            ext = os.path.splitext(name)[1].lower()
            is_dir = os.path.isdir(full)
            if mode == "video" and not (is_dir or ext in VIDEO_EXTENSIONS):
                continue
            if mode == "comic" and not (is_dir or ext in COMIC_EXTENSIONS):
                continue
            item = self._make_item(full)
            if item:
                items.append(item)
        return items

    def get_item(self, item_id: str) -> MediaItem | None:
        row = self._storage.get_media_item(item_id)
        if not row:
            return None
        path = row["path"]
        if not os.path.exists(path):
            return None
        return self._make_item(path)

    def require_item(self, item_id: str) -> MediaItem:
        item = self.get_item(item_id)
        if item is None:
            raise KeyError(f"メディア項目が見つかりません: {item_id}")
        return item

    # ---------------- rating / delete / recent ----------------

    def set_rating(self, item_id: str, rating: int | None) -> MediaItem:
        item = self.require_item(item_id)
        new_path = self._rating.set_rating(item.path, rating)
        updated = self._make_item(new_path)
        assert updated is not None
        return updated

    def delete_item(self, item_id: str, use_trash: bool | None = None) -> dict:
        item = self.require_item(item_id)
        if use_trash is None:
            use_trash = bool(self._settings.get("delete_to_trash", True))
        method = "trash"
        if use_trash and send2trash is not None:
            send2trash(item.path)
        else:
            method = "unlink"
            if os.path.isdir(item.path):
                import shutil
                shutil.rmtree(item.path)
            else:
                os.remove(item.path)
        self._storage.delete_media_item(item_id)
        return {"deleted": item.display_name, "method": method}

    def mark_opened(self, item: MediaItem) -> None:
        self._storage.add_recent(item.id, item.path, item.media_type)

    def list_recent(self, limit: int = 30) -> list[dict]:
        return self._storage.list_recent(limit)
