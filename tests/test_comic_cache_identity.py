"""Old prefetch sources must not populate a reopened book's page cache."""
import io
import zipfile

from PIL import Image

from framedeck.comic.archive_memory import ArchiveMemoryBudget
from framedeck.comic.image_pipeline import ImagePipeline
from framedeck.comic.source import ArchiveComicSource
from framedeck.models import ComicEntry, PageRef


def test_reopened_zip_and_late_old_prefetch_have_separate_caches(tmp_path):
    path = tmp_path / 'book.zip'
    def write(size, color):
        buf = io.BytesIO()
        Image.new('RGB', size, color).save(buf, 'PNG')
        with zipfile.ZipFile(path, 'w') as z:
            z.writestr('page.png', buf.getvalue())
        return buf.getvalue()
    old_data = write((40, 60), 'red')
    pool = ArchiveMemoryBudget(1024 * 1024)
    old = ArchiveComicSource(str(path), pool)
    new_data = write((80, 120), 'blue')
    new = ArchiveComicSource(str(path), pool)
    entry = ComicEntry('same-book', 'root', 'book', 'archive', str(path), (), None, ())
    page = PageRef(0, 'page.png')
    pipeline = ImagePipeline(tmp_path/'pages', tmp_path/'thumbs')
    try:
        # An old memory snapshot finishes prefetching after a new snapshot opens.
        assert pipeline.get_raw(old, entry, page) == old_data
        assert pipeline.get_page_size(old, entry, page) == (40, 60)
        assert pipeline.get_raw(new, entry, page) == new_data
        assert pipeline.get_page_size(new, entry, page) == (80, 120)
        assert pipeline.get_raw(old, entry, page) == old_data
        assert pipeline.get_raw(new, entry, page) == new_data
    finally:
        pipeline.executor.shutdown(wait=True)
        old.close()
        new.close()


def test_cache_keys_distinguish_member_names_at_same_index(tmp_path):
    pipeline = ImagePipeline(tmp_path/'pages', tmp_path/'thumbs')
    entry = ComicEntry('same', 'root', 'book', 'archive', '/missing.zip', (), None, ())
    a, b = PageRef(0, 'old.png'), PageRef(0, 'new.png')
    try:
        assert pipeline._raw_key(entry, a) != pipeline._raw_key(entry, b)
        assert pipeline._disk_key(entry, a, None, None, 'page') != pipeline._disk_key(entry, b, None, None, 'page')
    finally:
        pipeline.shutdown()


def test_content_identity_changes_on_reopen_and_stale_request_is_rejected(app_env, comic_root):
    from framedeck.comic.reader_engine import ComicEngineError
    import pytest
    services, *_ = app_env
    path = str(comic_root/'C.cbz')
    entry = services.comic_engine.entries_for_item(path)[0]
    old = services.comic_engine.create_session(str(comic_root), entry.id, item_path=path)
    new = services.comic_engine.open_entry(old.session_id, entry.id)
    assert old.content_id and old.content_id != new.content_id
    with pytest.raises(ComicEngineError):
        services.comic_engine.render_page(old.session_id, 0, expected_content=old.content_id)
    with pytest.raises(ComicEngineError):
        services.comic_engine.render_variant_page(old.session_id, 0, expected_content=old.content_id)
    assert services.comic_engine.render_page(new.session_id, 0, expected_content=new.content_id)[0]


def test_browser_page_urls_change_with_content_generation():
    import shutil
    import subprocess
    from pathlib import Path
    import pytest
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js required')
    js = (Path(__file__).resolve().parents[1]/'framedeck/web/static/js/app.js').read_text()
    code = js[js.index('function comicPageUrl('):js.index('function comicVisiblePageSpecs(')]
    harness = '''
const assert=require('node:assert/strict');
const S={settings:{comic_delivery_mode:'original'},comic:{state:{session_id:'s',entry_id:'e',content_id:'old'}},uiProfile:'mobile'};
const $=()=>({getBoundingClientRect:()=>({width:400,height:700})});
const window={devicePixelRatio:1};
const comicDeliveryProfile=()=> 'mobile';
'''
    checks = '''
for(const mode of ['original','compressed']) {
  S.settings.comic_delivery_mode=mode;
  S.comic.state.content_id='old'; const old=comicPageUrl(5);
  S.comic.state.content_id='new'; const current=comicPageUrl(5);
  assert.notEqual(old,current);
  assert.equal(new URL(current,'http://localhost').searchParams.get('content'),'new');
}
'''
    subprocess.run([node, '-e', harness+code+checks],check=True,timeout=10)
