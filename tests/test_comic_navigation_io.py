"""Navigation metadata must not commit to disk on every page turn."""
from types import SimpleNamespace

from framedeck.core.library_service import item_id_for
from framedeck.core.storage import Storage
from framedeck.web.routers.comic import _folder_id_for_item


def test_indexed_parent_navigation_preserves_rating_without_writes(tmp_path, monkeypatch):
    folder = tmp_path / 'series'
    folder.mkdir()
    book = folder / 'book.zip'
    storage = Storage(tmp_path / 'library.db')
    folder_id, book_id = item_id_for(str(folder)), item_id_for(str(book))
    storage.upsert_media_item(folder_id, str(folder), 'folder', 4, 123, None)
    storage.upsert_media_item(book_id, str(book), 'comic', None, 123, None)
    services = SimpleNamespace(storage=storage, library=SimpleNamespace(list_roots=lambda: []))
    queries = []
    storage._conn.set_trace_callback(queries.append)
    def unexpected_stat(*args):
        raise AssertionError('indexed parent must not hit filesystem')
    monkeypatch.setattr('framedeck.web.routers.comic.os.path.isdir', unexpected_stat)
    try:
        for _ in range(100):
            assert _folder_id_for_item(services, book_id) == folder_id
        assert storage.get_media_item(folder_id)['rating'] == 4
        assert not any(q.lstrip().upper().startswith(('INSERT', 'UPDATE', 'COMMIT', 'BEGIN')) for q in queries)
    finally:
        storage.close()


def test_unindexed_parent_is_registered_only_once(tmp_path):
    folder = tmp_path / 'series'
    folder.mkdir()
    book = folder / 'book.zip'
    storage = Storage(tmp_path / 'library.db')
    book_id = item_id_for(str(book))
    storage.upsert_media_item(book_id, str(book), 'comic', None, 123, None)
    services = SimpleNamespace(storage=storage, library=SimpleNamespace(list_roots=lambda: []))
    queries = []
    storage._conn.set_trace_callback(queries.append)
    try:
        for _ in range(3):
            assert _folder_id_for_item(services, book_id) == item_id_for(str(folder))
        assert sum(q.startswith('INSERT INTO media_items') for q in queries) == 1
    finally:
        storage.close()
