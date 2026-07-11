"""ComicSource抽象化。UIやエンジンはこの共通インターフェースだけを使う。"""
from __future__ import annotations

import os
from typing import Protocol

from ..config import IMAGE_EXTENSIONS
from ..core.rating_service import natural_key
from ..models import ComicEntry, PageRef
from .archive_backend import ArchiveError, ArchiveReader
from .nested_cache import NestedArchiveCache


class ComicSource(Protocol):
    def list_pages(self) -> list[PageRef]: ...
    def read_page(self, page: PageRef) -> bytes: ...
    def close(self) -> None: ...


class FolderComicSource:
    def __init__(self, path: str):
        self._path = path
        if not os.path.isdir(path):
            raise ArchiveError(f"画像フォルダが見つかりません: {path}")

    def list_pages(self) -> list[PageRef]:
        names = []
        try:
            for name in os.listdir(self._path):
                full = os.path.join(self._path, name)
                if not os.path.isfile(full):
                    continue
                if os.path.splitext(name)[1].lower() in IMAGE_EXTENSIONS:
                    names.append(name)
        except OSError as e:
            raise ArchiveError(f"フォルダを読めませんでした: {e}") from e
        names.sort(key=natural_key)
        return [PageRef(i, n) for i, n in enumerate(names)]

    def read_page(self, page: PageRef) -> bytes:
        with open(os.path.join(self._path, page.name), "rb") as f:
            return f.read()

    def close(self) -> None:
        pass


class ArchiveComicSource:
    """アーカイブ直下(および内部)の画像をページとして扱う。"""

    def __init__(self, path: str):
        self._reader = ArchiveReader(path).open()

    def list_pages(self) -> list[PageRef]:
        return [PageRef(i, n) for i, n in enumerate(self._reader.list_images())]

    def read_page(self, page: PageRef) -> bytes:
        return self._reader.read(page.name)

    def close(self) -> None:
        self._reader.close()


class ComicSourceResolver:
    """ComicEntry から適切な ComicSource を生成する唯一の入口。"""

    def __init__(self, nested_cache: NestedArchiveCache):
        self._nested_cache = nested_cache

    def open(self, entry: ComicEntry) -> ComicSource:
        if entry.source_type == "image_folder":
            return FolderComicSource(entry.physical_path)
        if entry.source_type == "archive":
            return ArchiveComicSource(entry.physical_path)
        if entry.source_type == "nested_archive":
            if not entry.container_chain or not entry.inner_path:
                raise ArchiveError("ネストアーカイブ情報が不完全です。")
            extracted = self._nested_cache.get_extracted_path(
                entry.container_chain[-1], entry.inner_path
            )
            return ArchiveComicSource(extracted)
        raise ArchiveError(f"未知のソース種別です: {entry.source_type}")
