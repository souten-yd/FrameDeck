import io
import threading

from PIL import Image

from framedeck.comic.image_pipeline import ImagePipeline
from framedeck.models import ComicEntry, PageRef


class _MemorySource:
    def __init__(self, data: bytes):
        self.data = data

    def read_page(self, page: PageRef) -> bytes:
        return self.data


def _jpeg_bytes() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (160, 240), "white").save(output, "JPEG")
    return output.getvalue()


def test_repeated_prefetch_deduplicates_inflight_pages(tmp_path, monkeypatch):
    pipeline = ImagePipeline(
        tmp_path / "pages", tmp_path / "thumbs", max_workers=4)
    source = _MemorySource(_jpeg_bytes())
    entry = ComicEntry(
        "entry", "root", "book", "archive", "/book.zip", (), None, ())
    pages = [PageRef(index, f"{index}.jpg") for index in range(10)]
    release = threading.Event()
    started = threading.Event()
    analyzed: list[int] = []
    analyzed_lock = threading.Lock()

    def analyze(_source, _entry, page):
        with analyzed_lock:
            analyzed.append(page.index)
            if len(analyzed) == 4:
                started.set()
        release.wait(timeout=2)

    monkeypatch.setattr(pipeline, "analyze_page", analyze)
    try:
        pipeline.prefetch(source, entry, pages, 0, ahead=6, behind=0)
        assert started.wait(timeout=2)
        for _ in range(10):
            pipeline.prefetch(source, entry, pages, 0, ahead=6, behind=0)
        release.set()
        pipeline.executor.shutdown(wait=True)
    finally:
        release.set()

    assert sorted(analyzed) == [1, 2, 3, 4, 5, 6]


def test_fast_seeking_bounds_prefetch_and_skips_obsolete_work(tmp_path, monkeypatch):
    pipeline = ImagePipeline(tmp_path / 'pages', tmp_path / 'thumbs')
    pipeline.executor.shutdown(wait=True)
    queued = []
    class Queue:
        def submit(self, fn, *args):
            queued.append((fn, args))
    pipeline.executor = Queue()
    source = _MemorySource(_jpeg_bytes())
    reads = []
    monkeypatch.setattr(source, 'read_page', lambda page: reads.append(page.index) or source.data)
    monkeypatch.setattr(pipeline, 'analyze_page', lambda *args: None)
    entry = ComicEntry('entry', 'root', 'book', 'archive', '/book.zip', (), None, ())
    pages = [PageRef(i, f'{i}.jpg') for i in range(200)]
    for center in range(100):
        pipeline.prefetch(source, entry, pages, center, ahead=8, behind=2)
    assert len(queued) <= 16
    for fn, args in queued:
        fn(*args)
    assert reads == []  # Every queued page is now far behind the viewport.
    queued.clear()
    pipeline.prefetch(source, entry, pages, 100, ahead=2, behind=0)
    for fn, args in queued:
        fn(*args)
    assert reads == [101, 102]
    assert not pipeline._prefetch_inflight


def test_page_dimensions_do_not_decode_pixels(tmp_path, monkeypatch):
    pipeline = ImagePipeline(tmp_path / 'pages', tmp_path / 'thumbs')
    source = _MemorySource(_jpeg_bytes())
    entry = ComicEntry('entry', 'root', 'book', 'archive', '/book.zip', (), None, ())
    decoded = []
    def forbidden(*args, **kwargs):
        decoded.append(True)
        raise AssertionError('dimension lookup decoded the image')
    monkeypatch.setattr(Image.Image, 'load', forbidden)
    try:
        assert pipeline.get_page_size(source, entry, PageRef(0, '0.jpg')) == (160, 240)
        assert decoded == []
    finally:
        pipeline.shutdown()


def test_oversized_page_does_not_evict_cached_neighbors():
    from framedeck.comic.image_pipeline import MemoryLRU
    cache = MemoryLRU(10)
    cache.put('nearby', b'12345')
    cache.put('huge', b'x' * 11)
    assert cache.get('nearby') == b'12345'
    assert cache.get('huge') is None
