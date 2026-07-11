"""ReadingSequence構築のテスト(指示書25.1)。"""
import os

from framedeck.comic.sequence_builder import SequenceBuilder


def expected_labels():
    return [
        "A.zip::A1.cbz",
        "A.zip::A2.cbz",
        os.path.join("B", "B1.cbz"),
        os.path.join("B", "B2.cbz"),
        "C.cbz",
    ]


def test_sequence_order(comic_root):
    builder = SequenceBuilder()
    sequence = builder.build_sequence(str(comic_root))
    labels = [e.label for e in sequence.entries]
    assert labels == expected_labels()


def test_sequence_types(comic_root):
    builder = SequenceBuilder()
    sequence = builder.build_sequence(str(comic_root))
    types = [e.source_type for e in sequence.entries]
    assert types == [
        "nested_archive", "nested_archive", "archive", "archive", "archive",
    ]


def test_sequence_is_cached_until_change(comic_root):
    builder = SequenceBuilder()
    first = builder.build_sequence(str(comic_root))
    second = builder.build_sequence(str(comic_root))
    assert first is second


def test_parent_direct_images_come_first(tmp_path):
    from tests.conftest import make_zip
    root = tmp_path / "mixed"
    root.mkdir()
    make_zip(root / "Parent.zip", pages=2, nested={"Child01.cbz": 3})
    builder = SequenceBuilder()
    sequence = builder.build_sequence(str(root))
    assert [e.source_type for e in sequence.entries] == [
        "archive", "nested_archive",
    ]
    assert sequence.entries[1].label.endswith("Child01.cbz")


def test_exclude_parent_direct_images(tmp_path):
    from tests.conftest import make_zip
    root = tmp_path / "mixed2"
    root.mkdir()
    make_zip(root / "Parent.zip", pages=2, nested={"Child01.cbz": 3})
    builder = SequenceBuilder(include_parent_direct_images=False)
    sequence = builder.build_sequence(str(root))
    assert [e.source_type for e in sequence.entries] == ["nested_archive"]


def test_entry_id_stable_across_rating_rename(comic_root):
    """評価タグのリネームでエントリーIDが変わらないこと。"""
    builder = SequenceBuilder()
    before = builder.build_sequence(str(comic_root))
    ids_before = {e.label: e.id for e in before.entries}

    old = comic_root / "C.cbz"
    new = comic_root / "C{zpi$r=3}.cbz"
    os.rename(old, new)
    builder.invalidate()
    after = builder.build_sequence(str(comic_root))
    c_after = [e for e in after.entries if "C" in e.label][-1]
    assert c_after.id == ids_before["C.cbz"]
