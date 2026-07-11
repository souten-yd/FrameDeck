"""ReadingSequence(確定した読書順)の構築。

前後移動はUI一覧・フィルター・並び替えから独立し、このシーケンスに
従って行う。並び順は自然順:
  1. ディレクトリ階層
  2. ファイル名の自然順
  3. アーカイブ内パスの自然順

大規模ライブラリ対策として、シーケンスは遅延実体化する:
ルート直下のトップ項目(フォルダ・アーカイブ)の順序だけを先に確定し、
各トップ項目内の読書単位(ComicEntry)は必要になった時点で展開して
キャッシュする。読書順の定義自体は完全実体化した場合と同一。
"""
from __future__ import annotations

import hashlib
import os
import threading

from ..config import COMIC_EXTENSIONS, IMAGE_EXTENSIONS, MAX_FOLDER_NEST_DEPTH
from ..core.library_service import canonical_path, item_id_for
from ..core.rating_service import natural_key
from ..models import ComicEntry, comic_entry_id
from .archive_backend import ArchiveError, ArchiveReader


def _entry(root_item_id: str, label: str, source_type: str,
           physical_path: str, container_chain: tuple[str, ...] = (),
           inner_path: str | None = None) -> ComicEntry:
    canonical = canonical_path(os.path.abspath(physical_path))
    canonical_chain = tuple(
        canonical_path(os.path.abspath(p)) for p in container_chain
    )
    return ComicEntry(
        id=comic_entry_id(canonical, canonical_chain, inner_path),
        root_item_id=root_item_id,
        label=label,
        source_type=source_type,  # type: ignore[arg-type]
        physical_path=os.path.abspath(physical_path),
        container_chain=tuple(os.path.abspath(p) for p in container_chain),
        inner_path=inner_path,
        sort_key=tuple(natural_key(part) for part in label.split(os.sep)),
    )


class ReadingSequenceLazy:
    """遅延実体化されたReadingSequence。

    tops: ルート直下のトップ項目パス(自然順で確定済み)。
    entries_at(i): トップ項目iの読書単位(オンデマンド展開・キャッシュ)。
    """

    def __init__(self, builder: "SequenceBuilder", root_path: str,
                 tops: list[str]):
        self._builder = builder
        self.root_path = root_path
        self.id = hashlib.sha1(
            root_path.encode("utf-8", "surrogatepass")).hexdigest()
        self.tops = tops
        self._lock = threading.Lock()
        self._cache: dict[int, tuple[ComicEntry, ...]] = {}

    @property
    def top_count(self) -> int:
        return len(self.tops)

    def entries_at(self, top_index: int) -> tuple[ComicEntry, ...]:
        if not (0 <= top_index < len(self.tops)):
            return ()
        with self._lock:
            cached = self._cache.get(top_index)
        if cached is not None:
            return cached
        entries = tuple(self._builder.discover_entries_for_item(
            self.tops[top_index]))
        with self._lock:
            self._cache[top_index] = entries
        return entries

    def locate(self, entry_id: str,
               hint_path: str | None = None) -> tuple[int, int] | None:
        """entry_id の (top_index, sub_index) を返す。

        hint_path(エントリーが属するライブラリ項目のパス)があれば
        その項目だけを展開する。なければキャッシュ済み→全トップの順で探す。
        """
        if hint_path:
            hint_abs = os.path.abspath(hint_path)
            for i, top in enumerate(self.tops):
                if os.path.abspath(top) == hint_abs:
                    for j, entry in enumerate(self.entries_at(i)):
                        if entry.id == entry_id:
                            return (i, j)
                    break
        with self._lock:
            cached_indices = list(self._cache.keys())
        for i in cached_indices:
            for j, entry in enumerate(self.entries_at(i)):
                if entry.id == entry_id:
                    return (i, j)
        for i in range(len(self.tops)):
            for j, entry in enumerate(self.entries_at(i)):
                if entry.id == entry_id:
                    return (i, j)
        return None

    # ---- テスト・小規模用途向けの完全実体化ビュー ----

    @property
    def entries(self) -> tuple[ComicEntry, ...]:
        result: list[ComicEntry] = []
        for i in range(len(self.tops)):
            result.extend(self.entries_at(i))
        return tuple(result)

    def index_of(self, entry_id: str) -> int | None:
        for i, entry in enumerate(self.entries):
            if entry.id == entry_id:
                return i
        return None


class SequenceBuilder:
    def __init__(self, include_parent_direct_images: bool = True):
        self.include_parent_direct_images = include_parent_direct_images
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[tuple, ReadingSequenceLazy]] = {}

    # ---------------- 1項目内のエントリー列挙 ----------------

    def discover_entries_for_item(self, path: str) -> list[ComicEntry]:
        """ライブラリ項目(フォルダまたはアーカイブ)1つ分の読書単位を列挙する。"""
        path = os.path.abspath(path)
        root_item_id = item_id_for(path)
        if os.path.isdir(path):
            entries = self._discover_folder(path, root_item_id)
            # ラベルを項目名からの相対で統一する
            name = os.path.basename(path.rstrip(os.sep))
            result = []
            for entry in entries:
                label = entry.label
                if label != name and not label.startswith(name + os.sep):
                    label = os.path.join(name, label)
                result.append(ComicEntry(
                    id=entry.id, root_item_id=entry.root_item_id,
                    label=label, source_type=entry.source_type,
                    physical_path=entry.physical_path,
                    container_chain=entry.container_chain,
                    inner_path=entry.inner_path, sort_key=entry.sort_key,
                ))
            return result
        return self._discover_archive(
            path, root_item_id, label_prefix=os.path.basename(path))

    def _discover_folder(self, folder: str,
                         root_item_id: str) -> list[ComicEntry]:
        entries: list[ComicEntry] = []
        root_depth = folder.rstrip(os.sep).count(os.sep)
        for current, dirs, files in os.walk(folder):
            depth = current.rstrip(os.sep).count(os.sep) - root_depth
            if depth >= MAX_FOLDER_NEST_DEPTH:
                dirs[:] = []
            if depth > MAX_FOLDER_NEST_DEPTH:
                continue
            dirs.sort(key=natural_key)
            files_sorted = sorted(files, key=natural_key)

            has_images = any(
                os.path.splitext(name)[1].lower() in IMAGE_EXTENSIONS
                for name in files_sorted
            )
            if has_images:
                label = os.path.relpath(current, folder)
                if label == ".":
                    label = os.path.basename(folder)
                entries.append(_entry(
                    root_item_id, label, "image_folder", current
                ))

            for name in files_sorted:
                if os.path.splitext(name)[1].lower() in COMIC_EXTENSIONS:
                    full = os.path.join(current, name)
                    prefix = os.path.relpath(full, folder)
                    entries.extend(self._discover_archive(
                        full, root_item_id, label_prefix=prefix
                    ))
        return entries

    def _discover_archive(self, archive_path: str, root_item_id: str,
                          label_prefix: str) -> list[ComicEntry]:
        """アーカイブの直接画像 + 子アーカイブをエントリー化する。

        読書順: 親の直接画像 → 子アーカイブ(自然順)。
        """
        base_label = label_prefix or os.path.basename(archive_path)
        try:
            with ArchiveReader(archive_path) as reader:
                names = reader.list_names()
        except ArchiveError:
            return []

        direct_images = False
        nested: list[str] = []
        for name in names:
            base = os.path.basename(name.replace("\\", "/"))
            if not base:
                continue
            ext = os.path.splitext(base)[1].lower()
            if ext in IMAGE_EXTENSIONS:
                direct_images = True
            elif ext in COMIC_EXTENSIONS:
                nested.append(name)
        nested.sort(key=natural_key)

        entries: list[ComicEntry] = []
        include_direct = direct_images and (
            self.include_parent_direct_images or not nested
        )
        if include_direct:
            entries.append(_entry(
                root_item_id, base_label, "archive", archive_path
            ))
        for inner in nested:
            entries.append(_entry(
                root_item_id, f"{base_label}::{inner}", "nested_archive",
                archive_path, container_chain=(archive_path,),
                inner_path=inner,
            ))
        return entries

    # ---------------- ルート全体のシーケンス ----------------

    def _list_tops(self, root_folder: str) -> list[str]:
        tops: list[str] = []
        try:
            for name in sorted(os.listdir(root_folder), key=natural_key):
                full = os.path.join(root_folder, name)
                ext = os.path.splitext(name)[1].lower()
                if os.path.isdir(full) or ext in COMIC_EXTENSIONS:
                    tops.append(full)
        except OSError:
            pass
        return tops

    def _signature(self, root_folder: str) -> tuple:
        sig = []
        for full in self._list_tops(root_folder):
            try:
                stat = os.stat(full)
                sig.append((os.path.basename(full), stat.st_mtime,
                            stat.st_size))
            except OSError:
                continue
        return tuple(sig)

    def build_sequence(self, root_folder: str) -> ReadingSequenceLazy:
        """ルートフォルダ直下を基準にした読書シーケンス(遅延実体化)。"""
        root_folder = os.path.abspath(root_folder)
        signature = self._signature(root_folder)
        with self._lock:
            cached = self._cache.get(root_folder)
            if cached and cached[0] == signature:
                return cached[1]
        sequence = ReadingSequenceLazy(
            self, root_folder, self._list_tops(root_folder))
        with self._lock:
            self._cache[root_folder] = (signature, sequence)
        return sequence

    def invalidate(self, root_folder: str | None = None) -> None:
        with self._lock:
            if root_folder is None:
                self._cache.clear()
            else:
                self._cache.pop(os.path.abspath(root_folder), None)
