"""アーカイブバックエンドのテスト。"""
import io
import zipfile

import pytest

from framedeck.comic.archive_backend import ArchiveError, ArchiveReader
from framedeck.core.security import is_safe_member_name
from tests.conftest import make_image_bytes


def test_zip_images_natural_order(tmp_path):
    path = tmp_path / "order.zip"
    with zipfile.ZipFile(path, "w") as zf:
        for name in ("10.jpg", "2.jpg", "1.jpg"):
            zf.writestr(name, make_image_bytes(100, 100))
    with ArchiveReader(str(path)) as reader:
        assert reader.list_images() == ["1.jpg", "2.jpg", "10.jpg"]


def test_japanese_filenames(tmp_path):
    path = tmp_path / "日本語 アーカイブ.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("ページ 1.jpg", make_image_bytes(100, 100))
        zf.writestr("ページ 2.jpg", make_image_bytes(100, 100))
    with ArchiveReader(str(path)) as reader:
        images = reader.list_images()
        assert images == ["ページ 1.jpg", "ページ 2.jpg"]
        assert len(reader.read(images[0])) > 0


def test_unsafe_member_names_filtered(tmp_path):
    path = tmp_path / "evil.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("../escape.jpg", make_image_bytes(50, 50))
        zf.writestr("/abs.jpg", make_image_bytes(50, 50))
        zf.writestr("ok.jpg", make_image_bytes(50, 50))
    with ArchiveReader(str(path)) as reader:
        assert reader.list_images() == ["ok.jpg"]
        with pytest.raises(ArchiveError):
            reader.read("../escape.jpg")


def test_safe_member_name_rules():
    assert is_safe_member_name("dir/page.jpg")
    assert is_safe_member_name("日本語/ページ.png")
    assert not is_safe_member_name("../up.jpg")
    assert not is_safe_member_name("a/../../up.jpg")
    assert not is_safe_member_name("/absolute.jpg")
    assert not is_safe_member_name("C:evil.jpg")
    assert not is_safe_member_name("bad\x00.jpg")
    assert not is_safe_member_name("")


def test_broken_archive_raises(tmp_path):
    path = tmp_path / "broken.zip"
    path.write_bytes(b"this is not a zip file")
    with pytest.raises(ArchiveError):
        ArchiveReader(str(path)).open()


def test_unsupported_extension(tmp_path):
    path = tmp_path / "file.7z"
    path.write_bytes(b"dummy")
    with pytest.raises(ArchiveError):
        ArchiveReader(str(path)).open()


def test_nested_cache_reuses_extraction(app_env, comic_root):
    services, _, _ = app_env
    cache = services.nested_cache
    parent = str(comic_root / "A.zip")
    first = cache.get_extracted_path(parent, "A1.cbz")
    second = cache.get_extracted_path(parent, "A1.cbz")
    assert first == second
    with ArchiveReader(first) as reader:
        assert len(reader.list_images()) == 4
