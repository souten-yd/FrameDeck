"""漫画間ナビゲーション(ReadingSequence準拠)のテスト(指示書25.1)。"""
import pytest


def _create_session(services, comic_root, label_part="A1"):
    sequence = services.sequence_builder.build_sequence(str(comic_root))
    entry = next(e for e in sequence.entries if label_part in e.label)
    return services.comic_engine.create_session(str(comic_root), entry.id)


def test_next_entry_walks_sequence(app_env, comic_root):
    services, _, _ = app_env
    state = _create_session(services, comic_root, "A1")
    engine = services.comic_engine

    state = engine.next_entry(state.session_id)
    assert "A2" in state.title
    state = engine.next_entry(state.session_id)
    assert "B1" in state.title
    state = engine.previous_entry(state.session_id)
    assert "A2" in state.title


def test_b2_next_is_c(app_env, comic_root):
    services, _, _ = app_env
    state = _create_session(services, comic_root, "B2")
    state = services.comic_engine.next_entry(state.session_id)
    assert "C" in state.title


def test_stop_at_sequence_end(app_env, comic_root):
    services, _, _ = app_env
    assert services.settings.get("comic_sequence_end_behavior") == "stop"
    state = _create_session(services, comic_root, "C")
    engine = services.comic_engine

    moved = engine.next_entry(state.session_id)
    assert "C" in moved.title            # 移動しない
    assert moved.at_sequence_end is True

    first = _create_session(services, comic_root, "A1")
    moved = engine.previous_entry(first.session_id)
    assert "A1" in moved.title
    assert moved.at_sequence_start is True


def test_wrap_at_sequence_end(app_env, comic_root):
    services, _, _ = app_env
    services.settings.set("comic_sequence_end_behavior", "wrap")
    state = _create_session(services, comic_root, "C")
    engine = services.comic_engine

    moved = engine.next_entry(state.session_id)
    assert "A1" in moved.title           # 先頭へループ

    moved = engine.previous_entry(moved.session_id)
    assert "C" in moved.title            # 末尾へループ


def test_navigation_independent_of_ui_order(app_env, comic_root):
    """フィルター・並び替え(UI表示順)は読書順に影響しない。

    エンジンはUI一覧を参照しないため、ライブラリ側をどう並び替えても
    next_entryの結果は同じ。
    """
    services, _, _ = app_env
    services.library.add_root(str(comic_root))

    state = _create_session(services, comic_root, "A2")
    # UI相当の操作: 評価順で一覧を取得(読書順に影響しないことを確認)
    services.library.list_folder(str(comic_root), mode="comic")
    moved = services.comic_engine.next_entry(state.session_id)
    assert "B1" in moved.title


def test_session_navigation_without_ui_selection(app_env, comic_root):
    """UI選択が存在しなくてもセッションだけで移動できる。"""
    services, _, _ = app_env
    state = _create_session(services, comic_root, "B1")
    moved = services.comic_engine.next_entry(state.session_id)
    assert "B2" in moved.title


def test_previous_entry_opens_first_page_by_default(app_env, comic_root):
    services, _, _ = app_env
    state = _create_session(services, comic_root, "B2")
    moved = services.comic_engine.previous_entry(state.session_id)
    assert "B1" in moved.title
    assert moved.page_index == 0


def test_previous_entry_last_spread_setting(app_env, comic_root):
    services, _, _ = app_env
    services.settings.set("previous_entry_start", "last")
    state = _create_session(services, comic_root, "B2")
    moved = services.comic_engine.previous_entry(state.session_id)
    assert "B1" in moved.title
    # B1は4ページ・表紙単独 → 表示グループは [0], [1,2], [3]
    assert moved.page_index == 3


def test_progress_saved_and_restored(app_env, comic_root):
    services, _, _ = app_env
    engine = services.comic_engine
    state = _create_session(services, comic_root, "C")
    state = engine.next_spread(state.session_id)
    assert state.page_index > 0
    saved_index = state.page_index
    engine.close_session(state.session_id)

    restored = _create_session(services, comic_root, "C")
    assert restored.page_index == saved_index


def test_spread_grouping_with_cover(app_env, comic_root):
    """表紙単独 + 見開き: 4ページなら [0], [1,2], [3]。"""
    services, _, _ = app_env
    engine = services.comic_engine
    state = _create_session(services, comic_root, "B1")
    assert state.visible_pages == (0,)
    state = engine.next_spread(state.session_id)
    assert state.visible_pages == (1, 2)
    state = engine.next_spread(state.session_id)
    assert state.visible_pages == (3,)
    # 末尾で停止
    state = engine.next_spread(state.session_id)
    assert state.visible_pages == (3,)
    # 逆方向
    state = engine.previous_spread(state.session_id)
    assert state.visible_pages == (1, 2)
    state = engine.previous_spread(state.session_id)
    assert state.visible_pages == (0,)
    state = engine.previous_spread(state.session_id)
    assert state.visible_pages == (0,)


def test_landscape_page_shown_alone(app_env, tmp_path):
    from tests.conftest import make_zip
    root = tmp_path / "landscape_root"
    root.mkdir()
    # 5ページ、index2が横長
    make_zip(root / "L.cbz", pages=5, landscape_pages=(2,))
    services, _, _ = app_env
    engine = services.comic_engine
    sequence = services.sequence_builder.build_sequence(str(root))
    state = engine.create_session(str(root), sequence.entries[0].id)
    assert state.visible_pages == (0,)
    state = engine.next_spread(state.session_id)
    assert state.visible_pages == (1,)   # 次(2)が横長なので単独
    state = engine.next_spread(state.session_id)
    assert state.visible_pages == (2,)   # 横長単独
    state = engine.next_spread(state.session_id)
    assert state.visible_pages == (3, 4)


def test_single_page_mode(app_env, comic_root):
    services, _, _ = app_env
    engine = services.comic_engine
    state = _create_session(services, comic_root, "B1")
    state = engine.set_view_options(state.session_id, view_mode="single")
    assert state.visible_pages == (state.page_index,)
    state = engine.next_page(state.session_id)
    assert state.visible_pages == (1,)


def test_lazy_sequence_does_not_scan_other_items(app_env, comic_root):
    """item_pathヒント付きセッション作成では、他のトップ項目を展開しない
    (大規模ライブラリでの起動時間退行の防止)。"""
    services, _, _ = app_env
    engine = services.comic_engine
    entries = engine.entries_for_item(str(comic_root / "B"))
    b1 = next(e for e in entries if "B1" in e.label)
    state = engine.create_session(
        str(comic_root), b1.id, item_path=str(comic_root / "B"))
    assert "B1" in state.title
    sequence = services.sequence_builder.build_sequence(str(comic_root))
    # tops: [A.zip, B, C.cbz] — 展開済みはB(index 1)のみのはず
    assert set(sequence._cache.keys()) == {1}


def test_empty_archive_rejected(app_env, tmp_path):
    import zipfile
    from framedeck.comic.archive_backend import ArchiveError
    root = tmp_path / "empty_root"
    root.mkdir()
    with zipfile.ZipFile(root / "empty.cbz", "w") as zf:
        zf.writestr("readme.txt", "no images")
    services, _, _ = app_env
    sequence = services.sequence_builder.build_sequence(str(root))
    assert len(sequence.entries) == 0

def test_next_page_shifts_spread_by_one(app_env, comic_root):
    services, _, _ = app_env
    engine = services.comic_engine
    state = _create_session(services, comic_root, "B1")
    assert state.visible_pages == (0,)

    state = engine.next_spread(state.session_id)
    assert state.page_index == 1
    assert state.visible_pages == (1, 2)

    state = engine.next_page(state.session_id)
    assert state.page_index == 2
    assert state.visible_pages == (2, 3)

    state = engine.previous_page(state.session_id)
    assert state.page_index == 1
    assert state.visible_pages == (1, 2)


def test_next_spread_moves_by_group_after_shift(app_env, comic_root):
    services, _, _ = app_env
    engine = services.comic_engine
    state = _create_session(services, comic_root, "C")
    state = engine.next_page(state.session_id)
    assert state.visible_pages == (1, 2)
    state = engine.next_spread(state.session_id)
    assert state.page_index == 3
    assert state.visible_pages == (3, 4)

