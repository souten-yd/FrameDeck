"""I/O regression checks use actual archives and SQLite transaction traces."""
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from framedeck.comic.archive_memory import ArchiveMemoryBudget
from framedeck.comic.archive_backend import ArchiveReader, ArchiveError
from framedeck.core.storage import Storage


class LowIOTest(unittest.TestCase):
    def test_zip_page_reads_use_memory_and_budget_is_shared(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'book.cbz'
            with zipfile.ZipFile(path, 'w') as z:
                for i in range(20):
                    z.writestr(f'{i}.jpg', bytes([i]) * 1024)
            pool = ArchiveMemoryBudget(path.stat().st_size)
            first = ArchiveReader(str(path), pool).open()
            second = ArchiveReader(str(path), pool).open()
            self.assertIsNotNone(first._memory_buffer)
            self.assertIsNone(second._memory_buffer)
            second.close()
            # No physical source may be reopened for 20 reads or backtracking.
            with patch('builtins.open', side_effect=AssertionError('disk open')):
                for i in list(range(20)) + list(reversed(range(20))):
                    self.assertEqual(first.read(f'{i}.jpg'), bytes([i]) * 1024)
            first.close()
            first.close()
            self.assertEqual(pool.used_bytes, 0)
            with ArchiveReader(str(path), pool) as reopened:
                self.assertIsNotNone(reopened._memory_buffer)
            self.assertEqual(pool.used_bytes, 0)

    def test_bad_archive_releases_budget_and_zero_disables(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'bad.zip'
            p.write_bytes(b'not a zip')
            pool = ArchiveMemoryBudget(100)
            with self.assertRaises(ArchiveError):
                ArchiveReader(str(p), pool).open()
            self.assertEqual(pool.used_bytes, 0)
            self.assertIsNone(ArchiveMemoryBudget(0).load(str(p)))

    def test_progress_coalesces_and_latest_state_is_immediately_visible(self):
        db = Storage(':memory:')
        db.reading_flush_seconds = 30
        statements = []
        db._conn.set_trace_callback(statements.append)
        try:
            for i in range(20):
                db.save_reading_progress('book', i, 100)
            self.assertEqual(db.get_reading_progress('book')['page_index'], 19)
            self.assertEqual(db.get_reading_progress_many(['book'])['book']['page_index'], 19)
            self.assertEqual(sum(x == 'COMMIT' for x in statements), 0)
            db.flush_reading_progress()
            self.assertEqual(sum(x == 'COMMIT' for x in statements), 1)
            self.assertEqual(db.get_reading_progress('book')['page_index'], 19)
            db.save_reading_progress('book', 99, 100, completed=True)
            self.assertFalse(db._pending_reading)
            self.assertEqual(sum(x == 'COMMIT' for x in statements), 2)
        finally:
            db.close()

    def test_timer_close_and_rename_flush_pending_progress(self):
        import threading
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'state.db'
            db = Storage(path)
            db.reading_flush_seconds = 0.01
            committed = threading.Event()
            db._conn.set_trace_callback(lambda sql: committed.set() if sql == 'COMMIT' else None)
            db.save_reading_progress('old', 7, 20)
            self.assertTrue(committed.wait(2))
            db.reading_flush_seconds = 30
            db.save_reading_progress('old', 8, 20)
            db.remap_rated_folder('/nonexistent', '/new', {'old': 'new'})
            self.assertEqual(db.get_reading_progress('new')['page_index'], 8)
            db.save_reading_progress('new', 9, 20)
            db.close()
            reopened = Storage(path)
            self.assertEqual(reopened.get_reading_progress('new')['page_index'], 9)
            reopened.close()

if __name__ == '__main__':
    unittest.main()
