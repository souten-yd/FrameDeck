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
