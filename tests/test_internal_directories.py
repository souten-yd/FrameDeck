"""外部ツールの内部管理ディレクトリを表示・読書対象から除外するテスト。"""

from framedeck.comic.sequence_builder import SequenceBuilder
from framedeck.core.path_filters import is_internal_directory_name
from tests.conftest import make_zip


def test_doc_extractor_prefix_is_generic():
    assert is_internal_directory_name(".docExtractor-name-1234567")
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
        "B/B1.cbz",
        "B/B2.cbz",
        "C.cbz",
    ]
