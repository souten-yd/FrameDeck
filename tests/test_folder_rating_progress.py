"""Folder ratings must carry saved state across physical path changes."""
import tempfile
import unittest
import zipfile
from pathlib import Path
from framedeck.core.storage import Storage
from framedeck.core.library_service import LibraryService
from framedeck.core.rating_service import RatingService
from framedeck.comic.sequence_builder import SequenceBuilder


class FolderRatingProgressTest(unittest.TestCase):
    def test_rating_changes_and_removal_preserve_descendant_progress(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = root / 'Series_%'
            folder.mkdir()
            (folder / 'images').mkdir()
            (folder / 'images' / '01.jpg').write_bytes(b'page')
            with zipfile.ZipFile(folder / '01.cbz', 'w') as archive:
                archive.writestr('01.jpg', b'page')
                archive.writestr('nested.cbz', b'placeholder')
            (folder / 'clip.mp4').write_bytes(b'video')
            db = Storage(':memory:')
            library = LibraryService(db, {}, RatingService(db))
            item = library._make_item(str(folder))
            children = library.list_folder(str(folder), 'video', enforce_roots=False)
            video = next(x for x in children if x.media_type == 'video')
            db.save_video_progress(video.id, 42, 100)
            db.add_recent(video.id, video.path, 'video')
            entries = SequenceBuilder().discover_entries_for_item(str(folder))
            self.assertEqual(len(entries), 3)
            for i, entry in enumerate(entries):
                db.save_reading_progress(entry.id, i + 2, 12, i == 0, 'single', 'rtl')
            for rating in [5, 2, None, 5]:
                item = library.set_rating(item.id, rating)
                current = SequenceBuilder().discover_entries_for_item(item.path)
                for i, entry in enumerate(current):
                    progress = db.get_reading_progress(entry.id)
                    self.assertEqual(progress['page_index'], i + 2)
                    self.assertEqual(progress['completed'], i == 0)
                    self.assertEqual(progress['reader_mode'], 'single')
                video = next(x for x in library.list_folder(item.path, 'video', enforce_roots=False) if x.media_type == 'video')
                self.assertEqual(db.get_video_progress(video.id)['position_seconds'], 42)
                self.assertEqual(db.list_recent()[0]['path'], video.path)
            db.close()

    def test_db_failure_rolls_back_folder_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / 'Series'
            folder.mkdir()
            db = Storage(':memory:')
            library = LibraryService(db, {}, RatingService(db))
            item = library._make_item(str(folder))
            def fail(*args):
                raise RuntimeError('database unavailable')
            db.remap_rated_folder = fail
            with self.assertRaises(RuntimeError):
                library.set_rating(item.id, 5)
            self.assertTrue(folder.exists())
            self.assertFalse(Path(str(folder) + '{zpi$r=5}').exists())
            db.close()

if __name__ == '__main__':
    unittest.main()
