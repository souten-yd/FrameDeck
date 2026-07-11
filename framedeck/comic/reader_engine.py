"""ComicReaderEngine - UIから独立した漫画読書エンジン。

- ComicSession / ReadingSequence 管理
- ページ移動・見開き計算(表紙単独・横長単独・RTL/LTR)
- 漫画間移動(シーケンス順のみ。終端動作は設定 stop/wrap/prompt)
- 読書位置の保存・復元
- プリフェッチ指示

シーケンスは遅延実体化(ReadingSequenceLazy)を使い、現在位置は
(top_index, sub_index) のカーソルで管理する。UI(Tkinter / Web)は
このエンジンが返す ComicViewState だけを描画し、独自にページ番号を
計算しない。
"""
from __future__ import annotations

import threading
import uuid

from ..config import Settings
from ..core.storage import Storage
from ..models import ComicEntry, ComicViewState, PageRef, ReaderOptions
from .archive_backend import ArchiveError
from .image_pipeline import ImagePipeline
from .sequence_builder import ReadingSequenceLazy, SequenceBuilder
from .source import ComicSourceResolver

MAX_SESSIONS = 32


class ComicEngineError(Exception):
    pass


class _EngineSession:
    def __init__(self, session_id: str, sequence: ReadingSequenceLazy,
                 top_index: int, sub_index: int, entry: ComicEntry,
                 source, pages: list[PageRef], options: ReaderOptions):
        self.id = session_id
        self.sequence = sequence
        self.top_index = top_index
        self.sub_index = sub_index
        self.entry = entry
        self.source = source
        self.pages = pages
        self.page_index = 0
        self.options = options

    @property
    def page_count(self) -> int:
        return len(self.pages)


class ComicReaderEngine:
    def __init__(self, builder: SequenceBuilder,
                 resolver: ComicSourceResolver,
                 pipeline: ImagePipeline,
                 storage: Storage,
                 settings: Settings):
        self._builder = builder
        self._resolver = resolver
        self._pipeline = pipeline
        self._storage = storage
        self._settings = settings
        self._lock = threading.RLock()
        self._sessions: dict[str, _EngineSession] = {}
        self._session_order: list[str] = []

    # ---------------- セッション管理 ----------------

    def _default_options(self) -> ReaderOptions:
        return ReaderOptions(
            view_mode=self._settings.get("view_mode", "spread"),
            reading_direction=self._settings.get("reading_direction", "rtl"),
            cover_as_single_page=bool(
                self._settings.get("cover_as_single_page", True)),
            landscape_threshold=float(
                self._settings.get("landscape_threshold", 1.25)),
        )

    def entries_for_item(self, item_path: str) -> list[ComicEntry]:
        return self._builder.discover_entries_for_item(item_path)

    def create_session(self, root_folder: str, entry_id: str,
                       restore_progress: bool = True,
                       item_path: str | None = None) -> ComicViewState:
        sequence = self._builder.build_sequence(root_folder)
        location = sequence.locate(entry_id, hint_path=item_path)
        if location is None:
            raise ComicEngineError(
                "読書シーケンス内にエントリーが見つかりません。"
            )
        top_index, sub_index = location
        session_id = uuid.uuid4().hex
        with self._lock:
            session = self._open_entry_session(
                session_id, sequence, top_index, sub_index,
                restore_progress=restore_progress,
            )
            self._sessions[session_id] = session
            self._session_order.append(session_id)
            while len(self._session_order) > MAX_SESSIONS:
                old_id = self._session_order.pop(0)
                self._close_session_locked(old_id)
            return self._state(session)

    def _open_entry_session(self, session_id: str,
                            sequence: ReadingSequenceLazy,
                            top_index: int, sub_index: int,
                            restore_progress: bool,
                            start_page: str = "saved") -> _EngineSession:
        entries = sequence.entries_at(top_index)
        if not (0 <= sub_index < len(entries)):
            raise ComicEngineError("エントリー位置が範囲外です。")
        entry = entries[sub_index]
        source = self._resolver.open(entry)
        try:
            pages = source.list_pages()
        except Exception:
            source.close()
            raise
        if not pages:
            source.close()
            raise ArchiveError(f"画像が見つかりませんでした: {entry.label}")
        session = _EngineSession(
            session_id, sequence, top_index, sub_index, entry, source,
            pages, self._default_options(),
        )
        if start_page == "last":
            session.page_index = self._last_group_start(session)
        elif start_page == "saved" and restore_progress:
            progress = self._storage.get_reading_progress(entry.id)
            if progress and not progress["completed"]:
                session.page_index = max(
                    0, min(progress["page_index"], len(pages) - 1)
                )
        self._save_progress(session)
        self._prefetch(session)
        return session

    def _get(self, session_id: str) -> _EngineSession:
        session = self._sessions.get(session_id)
        if session is None:
            raise ComicEngineError("セッションが存在しません。")
        return session

    def get_state(self, session_id: str) -> ComicViewState:
        with self._lock:
            return self._state(self._get(session_id))

    def close_session(self, session_id: str) -> None:
        with self._lock:
            self._close_session_locked(session_id)
            if session_id in self._session_order:
                self._session_order.remove(session_id)

    def _close_session_locked(self, session_id: str) -> None:
        session = self._sessions.pop(session_id, None)
        if session is not None:
            self._pipeline.release_source(session.source)
            try:
                session.source.close()
            except Exception:
                pass

    def shutdown(self) -> None:
        with self._lock:
            for session_id in list(self._sessions):
                self._close_session_locked(session_id)
            self._session_order.clear()

    # ---------------- 見開き計算 ----------------

    def _is_landscape(self, session: _EngineSession, index: int) -> bool:
        return self._pipeline.is_landscape(
            session.source, session.entry, session.pages[index],
            session.options.landscape_threshold,
        )

    def _group_at(self, session: _EngineSession, index: int) -> tuple[int, ...]:
        opts = session.options
        count = session.page_count
        index = max(0, min(index, count - 1))
        if opts.view_mode == "single":
            return (index,)
        if index == 0 and opts.cover_as_single_page:
            return (0,)
        if self._is_landscape(session, index):
            return (index,)
        nxt = index + 1
        if nxt >= count or self._is_landscape(session, nxt):
            return (index,)
        return (index, nxt)

    def _prev_group(self, session: _EngineSession,
                    index: int) -> tuple[int, ...] | None:
        """index の直前の表示グループ。

        表紙・横長ページで見開きの区切りが変わるため、直近のアンカー
        (先頭または横長ページ)から前方に歩いて index-1 を含むグループを
        求める。これにより前進時のグループ割りと常に一致する。
        """
        if index <= 0:
            return None
        if session.options.view_mode == "single":
            return (index - 1,)
        anchor = index - 1
        while anchor > 0 and not self._is_landscape(session, anchor):
            anchor -= 1
        cursor = anchor
        while cursor < index:
            group = self._group_at(session, cursor)
            if group[-1] >= index - 1:
                return group
            cursor = group[-1] + 1
        return (index - 1,)

    def _last_group_start(self, session: _EngineSession) -> int:
        group = self._prev_group(session, session.page_count)
        return group[0] if group else 0

    # ---------------- 状態・保存 ----------------

    def _state(self, session: _EngineSession,
               at_end: bool = False, at_start: bool = False) -> ComicViewState:
        group = self._group_at(session, session.page_index)
        entries = session.sequence.entries_at(session.top_index)
        has_prev = session.sub_index > 0 or session.top_index > 0
        has_next = (session.sub_index < len(entries) - 1
                    or session.top_index < session.sequence.top_count - 1)
        return ComicViewState(
            session_id=session.id,
            entry_id=session.entry.id,
            entry_index=session.sub_index,
            entry_count=len(entries),
            page_index=session.page_index,
            page_count=session.page_count,
            visible_pages=group,
            has_previous_entry=has_prev,
            has_next_entry=has_next,
            title=session.entry.label,
            reading_direction=session.options.reading_direction,
            view_mode=session.options.view_mode,
            root_item_id=session.entry.root_item_id,
            at_sequence_end=at_end,
            at_sequence_start=at_start,
        )

    def _save_progress(self, session: _EngineSession) -> None:
        group = self._group_at(session, session.page_index)
        completed = group[-1] >= session.page_count - 1
        self._storage.save_reading_progress(
            session.entry.id, session.page_index, session.page_count,
            completed=completed,
            reader_mode=session.options.view_mode,
            reading_direction=session.options.reading_direction,
        )

    def _prefetch(self, session: _EngineSession) -> None:
        self._pipeline.prefetch(
            session.source, session.entry, session.pages,
            center=session.page_index,
            ahead=int(self._settings.get("prefetch_ahead", 6)),
            behind=int(self._settings.get("prefetch_behind", 2)),
        )

    # ---------------- ページ移動 ----------------

    def _move_to(self, session: _EngineSession, index: int) -> ComicViewState:
        session.page_index = max(0, min(index, session.page_count - 1))
        self._save_progress(session)
        self._prefetch(session)
        return self._state(session)

    def next_spread(self, session_id: str) -> ComicViewState:
        with self._lock:
            session = self._get(session_id)
            group = self._group_at(session, session.page_index)
            target = group[-1] + 1
            if target >= session.page_count:
                return self._state(session)
            return self._move_to(session, target)

    def previous_spread(self, session_id: str) -> ComicViewState:
        with self._lock:
            session = self._get(session_id)
            group = self._prev_group(session, session.page_index)
            if group is None:
                return self._state(session)
            return self._move_to(session, group[0])

    def next_page(self, session_id: str) -> ComicViewState:
        with self._lock:
            session = self._get(session_id)
            return self._move_to(session, session.page_index + 1)

    def previous_page(self, session_id: str) -> ComicViewState:
        with self._lock:
            session = self._get(session_id)
            return self._move_to(session, session.page_index - 1)

    def goto_page(self, session_id: str, page_index: int) -> ComicViewState:
        with self._lock:
            session = self._get(session_id)
            return self._move_to(session, page_index)

    def set_view_options(self, session_id: str, *,
                         view_mode: str | None = None,
                         reading_direction: str | None = None,
                         cover_as_single_page: bool | None = None) -> ComicViewState:
        with self._lock:
            session = self._get(session_id)
            if view_mode in ("single", "spread"):
                session.options.view_mode = view_mode
            if reading_direction in ("rtl", "ltr"):
                session.options.reading_direction = reading_direction
            if cover_as_single_page is not None:
                session.options.cover_as_single_page = bool(cover_as_single_page)
            self._save_progress(session)
            return self._state(session)

    # ---------------- 漫画間移動 ----------------

    def next_entry(self, session_id: str) -> ComicViewState:
        return self._move_entry(session_id, +1)

    def previous_entry(self, session_id: str) -> ComicViewState:
        return self._move_entry(session_id, -1)

    def _move_entry(self, session_id: str, delta: int) -> ComicViewState:
        with self._lock:
            session = self._get(session_id)
            sequence = session.sequence
            behavior = self._settings.get("comic_sequence_end_behavior", "stop")

            start_page = "first"
            if delta < 0:
                start_page = {
                    "first": "first",
                    "last": "last",
                    "saved": "saved",
                }[self._settings.get("previous_entry_start", "first")]

            top = session.top_index
            sub = session.sub_index + delta
            tops_visited = 0
            wrapped = False
            while True:
                entries = sequence.entries_at(top)
                if not (0 <= sub < len(entries)):
                    # トップ項目をまたぐ
                    top += delta
                    tops_visited += 1
                    if tops_visited > sequence.top_count:
                        return self._state(session, at_end=delta > 0,
                                           at_start=delta < 0)
                    if not (0 <= top < sequence.top_count):
                        if behavior == "wrap" and not wrapped:
                            wrapped = True
                            top = 0 if delta > 0 else sequence.top_count - 1
                        else:
                            # stop / prompt: 端では移動しない(UI側で通知/確認)
                            return self._state(session, at_end=delta > 0,
                                               at_start=delta < 0)
                    entries = sequence.entries_at(top)
                    if not entries:
                        sub = 0 if delta > 0 else -1
                        continue
                    sub = 0 if delta > 0 else len(entries) - 1
                try:
                    new_session = self._open_entry_session(
                        session.id, sequence, top, sub,
                        restore_progress=(start_page == "saved"),
                        start_page=start_page,
                    )
                except Exception:
                    # 開けないエントリーは進行方向へスキップする
                    sub += delta
                    continue
                self._pipeline.release_source(session.source)
                try:
                    session.source.close()
                except Exception:
                    pass
                self._sessions[session.id] = new_session
                return self._state(new_session)

    def open_entry(self, session_id: str, entry_id: str,
                   restore_progress: bool = True) -> ComicViewState:
        """同一シーケンス内の任意エントリーへ移動する。"""
        with self._lock:
            session = self._get(session_id)
            location = session.sequence.locate(entry_id)
            if location is None:
                raise ComicEngineError("エントリーが見つかりません。")
            new_session = self._open_entry_session(
                session.id, session.sequence, location[0], location[1],
                restore_progress=restore_progress,
            )
            self._pipeline.release_source(session.source)
            try:
                session.source.close()
            except Exception:
                pass
            self._sessions[session.id] = new_session
            return self._state(new_session)

    # ---------------- 画像 ----------------

    def render_page(self, session_id: str, page_index: int,
                    max_width: int | None = None,
                    max_height: int | None = None) -> tuple[bytes, str, str]:
        with self._lock:
            session = self._get(session_id)
            if not (0 <= page_index < session.page_count):
                raise ComicEngineError(f"ページ範囲外です: {page_index}")
            source = session.source
            entry = session.entry
            page = session.pages[page_index]
        return self._pipeline.render_page(source, entry, page,
                                          max_width, max_height)

    def render_thumbnail(self, session_id: str, page_index: int,
                         size: int = 320) -> tuple[bytes, str, str]:
        with self._lock:
            session = self._get(session_id)
            if not (0 <= page_index < session.page_count):
                raise ComicEngineError(f"ページ範囲外です: {page_index}")
            source = session.source
            entry = session.entry
            page = session.pages[page_index]
        return self._pipeline.render_thumbnail(source, entry, page, size)
