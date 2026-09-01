"""外部ツールの内部管理ディレクトリを表示・読書対象から除外するテスト。"""
import os

from framedeck.comic.sequence_builder import SequenceBuilder
from framedeck.core.path_filters import is_internal_directory_name
from tests.conftest import make_zip


def test_doc_extractor_prefix_is_generic():
    assert is_internal_directory_name(".docExtractor-name-1234567")
    assert is_internal_directory_name(".docextractor-name-3855633329")
    assert is_internal_directory_name(".DOCEXTRACTOR-state")
    assert is_internal_directory_name(".docExtractor-duplicates")
    assert is_internal_directory_name(".docExtractor-state")
    assert is_internal_directory_name(".docExtractor-future-cache")
    assert not is_internal_directory_name(".docExtractor")
    assert not is_internal_directory_name(".docExtractorish-state")


def test_library_hides_all_doc_extractor_directories(app_env, comic_root):
    services, _paths, _tmp_path = app_env
    for name in (
        ".docExtractor-name-1234567",
        ".docExtractor-duplicates",
        ".docExtractor-state",
        ".docExtractor-future-cache",
    ):
        (comic_root / name).mkdir()
    (comic_root / ".docExtractorish-state").mkdir()

    services.library.add_root(str(comic_root), "comic")
    names = {
        item.display_name
        for item in services.library.list_folder(str(comic_root), mode="comic")
    }

    assert ".docExtractorish-state" in names
    assert not any(name.startswith(".docExtractor-") for name in names)


def test_video_library_hides_doc_extractor_directories_recursively(app_env, tmp_path):
    """動画モードでも各階層の内部フォルダを隠し、通常名は維持する。"""
    services, _paths, _tmp_path = app_env
    video_root = tmp_path / "video-library"
    visible_folder = video_root / "Series"
    hidden_top = video_root / ".docextractor-name-3855633329"
    hidden_nested = visible_folder / ".docextractor-name-233096521"
    similar_name = video_root / ".docExtractorish-state"
    for directory in (visible_folder, hidden_top, hidden_nested, similar_name):
        directory.mkdir(parents=True, exist_ok=True)
    (video_root / "Visible.mp4").write_bytes(b"video")
    (hidden_top / "HiddenTop.mp4").write_bytes(b"video")
    (hidden_nested / "HiddenNested.mp4").write_bytes(b"video")

    services.library.add_root(str(video_root), "video")
    root_names = {
        item.display_name
        for item in services.library.list_folder(str(video_root), mode="video")
    }
    nested_names = {
        item.display_name
        for item in services.library.list_folder(str(visible_folder), mode="video")
    }

    assert root_names == {".docExtractorish-state", "Series", "Visible.mp4"}
    assert ".docextractor-name-233096521" not in nested_names


def test_sequence_prunes_doc_extractor_directories_recursively(comic_root):
    make_zip(comic_root / ".docExtractor-state" / "HiddenTop.cbz", pages=1)
    make_zip(
        comic_root / "B" / ".docExtractor-duplicates" / "HiddenNested.cbz",
        pages=1,
    )

    sequence = SequenceBuilder().build_sequence(str(comic_root))
    labels = [entry.label for entry in sequence.entries]

    assert labels == [
        "A.zip::A1.cbz",
        "A.zip::A2.cbz",
        os.path.join("B", "B1.cbz"),
        os.path.join("B", "B2.cbz"),
        "C.cbz",
    ]
