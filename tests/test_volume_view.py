from pathlib import Path
import zipfile

from framedeck.core.volume_view import parse_volume_descriptor, recommend_display_mode


def test_single_volume_ignores_suffix_noise():
    item = parse_volume_descriptor("[橋本悠] 2.5次元の誘惑 第21巻 セミカラー版w.zip")
    assert item.kind == "volume"
    assert item.label == "21"
    assert item.start == 21


def test_volume_range_is_one_physical_archive():
    item = parse_volume_descriptor("BLACK_LAGOON_01-03巻.zip")
    assert item.kind == "volume_range"
    assert item.label == "01–03"
    assert item.start == 1
    assert item.end == 3


def test_compact_range_with_suffixes_is_recognized():
    item = parse_volume_descriptor("BLACK_LAGOON_01b-03b.zip")
    assert item.kind == "volume_range"
    assert item.label == "01–03"
    assert item.start == 1
    assert item.end == 3


def test_volume_range_can_be_confirmed_from_archive_directories(tmp_path: Path):
    archive = tmp_path / "作品 第01-03巻.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        for volume in range(1, 4):
            zf.writestr(f"第{volume:02d}巻/001.jpg", b"x")
    item = parse_volume_descriptor(archive.name, path=str(archive))
    assert item.label == "01–03"
    assert item.confidence == 1.0


def test_special_series_keeps_minimum_context():
    item = parse_volume_descriptor("転生したらスライムだった件 異聞 ～魔国暮らしのトリニティ～ 第11巻.zip")
    assert item.kind == "volume"
    assert item.label == "異聞 11"
    assert item.special == "異聞"


def test_chapter_is_recognized():
    assert parse_volume_descriptor("scan Ch.12.5.zip").label == "Ch. 12.5"
    assert parse_volume_descriptor("作品 第37話.zip").label == "Ch. 37"


def test_random_names_fall_back_to_conventional_display():
    values = [parse_volume_descriptor(name) for name in ("a8f3.zip", "bd72.zip", "c991.cbz")]
    mode, ratio = recommend_display_mode(values)
    assert mode == "files"
    assert ratio == 0.0


def test_recognized_folder_uses_volume_mode_independent_of_input_order():
    values = [
        parse_volume_descriptor("作品 第08巻.zip"),
        parse_volume_descriptor("作品 第01巻.zip"),
        parse_volume_descriptor("作品 第03巻r.zip"),
        parse_volume_descriptor("作品 第02巻w.zip"),
    ]
    mode, ratio = recommend_display_mode(values)
    assert mode == "volume"
    assert ratio == 1.0
    labels = [value.label for value in sorted(values, key=lambda value: value.sort_key)]
    assert labels == ["01", "02", "03", "08"]


def test_subfolders_prefer_conventional_display():
    values = [parse_volume_descriptor("作品 第01巻.zip")]
    mode, ratio = recommend_display_mode(values, has_folders=True)
    assert mode == "files"
    assert ratio == 0.0


def test_index_loads_volume_layer_after_main_app():
    html = Path("framedeck/web/templates/index.html").read_text("utf-8")
    assert html.index('/static/js/app.js') < html.index('/static/js/volume_view.js')
