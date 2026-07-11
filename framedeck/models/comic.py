"""漫画の読書単位・シーケンス・セッションモデル。"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Literal

ComicSourceType = Literal["image_folder", "archive", "nested_archive"]


def comic_entry_id(physical_path: str, container_chain: tuple[str, ...],
                   inner_path: str | None) -> str:
    raw = "\x00".join([physical_path, *container_chain, inner_path or ""])
    return hashlib.sha1(raw.encode("utf-8", "surrogatepass")).hexdigest()


@dataclass(frozen=True)
class ComicEntry:
    """1冊として読める単位(画像フォルダ / アーカイブ / 子アーカイブ)。"""
    id: str
    root_item_id: str
    label: str
    source_type: ComicSourceType
    physical_path: str
    container_chain: tuple[str, ...]
    inner_path: str | None
    sort_key: tuple


@dataclass(frozen=True)
class PageRef:
    index: int
    name: str


@dataclass(frozen=True)
class ReadingSequence:
    """確定した読書順。前後移動は entry_index ± 1 のみで行う。"""
    id: str
    root_path: str
    entries: tuple[ComicEntry, ...]

    def index_of(self, entry_id: str) -> int | None:
        for i, entry in enumerate(self.entries):
            if entry.id == entry_id:
                return i
        return None


@dataclass
class ReaderOptions:
    view_mode: str = "spread"            # single | spread
    reading_direction: str = "rtl"       # rtl | ltr
    cover_as_single_page: bool = True
    landscape_threshold: float = 1.25
    # 単ページモードで横長(見開き)画像を左右半分ずつ表示する
    split_spread_in_single_mode: bool = True


@dataclass
class ComicSession:
    session_id: str
    sequence_id: str
    entry_id: str
    entry_index: int
    page_index: int
    page_count: int
    view_mode: str
    reading_direction: str


@dataclass(frozen=True)
class ComicViewState:
    """Reader Engineが返す表示状態。UI側で独自にページ計算をしない。"""
    session_id: str
    entry_id: str
    entry_index: int
    entry_count: int
    page_index: int
    page_count: int
    visible_pages: tuple[int, ...]
    has_previous_entry: bool
    has_next_entry: bool
    title: str
    reading_direction: str
    view_mode: str
    root_item_id: str = ""
    root_folder_id: str | None = None
    at_sequence_end: bool = False
    at_sequence_start: bool = False
    # visible_pages と対の表示面("full" | "left" | "right")。
    # 単ページモードで横長画像を半分ずつ表示するときに使う。
    visible_page_sides: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        sides = self.visible_page_sides or tuple("full" for _ in self.visible_pages)
        return {
            "session_id": self.session_id,
            "entry_id": self.entry_id,
            "root_item_id": self.root_item_id,
            "root_folder_id": self.root_folder_id,
            "entry_index": self.entry_index,
            "entry_count": self.entry_count,
            "page_index": self.page_index,
            "page_count": self.page_count,
            "visible_pages": list(self.visible_pages),
            "visible_page_sides": list(sides),
            "has_previous_entry": self.has_previous_entry,
            "has_next_entry": self.has_next_entry,
            "title": self.title,
            "reading_direction": self.reading_direction,
            "view_mode": self.view_mode,
            "at_sequence_end": self.at_sequence_end,
            "at_sequence_start": self.at_sequence_start,
        }
