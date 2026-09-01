from pathlib import Path
import zipfile

from framedeck.core.volume_view import parse_volume_descriptor, recommend_display_mode
from framedeck.comic.reader_engine import display_label
from framedeck.web.routers.volume_view import _reading_summary


def test_single_volume_ignores_suffix_noise():
    item = parse_volume_descriptor("[橋本悠] 2.5次元の誘惑 第21巻 セミカラー版w.zip")
    assert item.kind == "volume"
    assert item.label == "🌈 21"
    assert item.start == 21
    assert item.color_edition is True


def test_normal_and_color_versions_have_distinct_labels_and_adjacent_order():
    normal = parse_volume_descriptor("青の祓魔師 第07巻.zip")
    color = parse_volume_descriptor("青の祓魔師 カラー版 第07巻.zip")

    assert normal.label == "07"
    assert normal.color_edition is False
    assert color.label == "🌈 07"
    assert color.color_edition is True
    assert sorted([color, normal], key=lambda item: item.sort_key) == [normal, color]


def test_color_edition_detection_supports_actual_and_english_names():
    assert parse_volume_descriptor("作品 第01巻 フルカラー版.zip").label == "🌈 01"
    assert parse_volume_descriptor("Work Vol.2 Full Color.cbz").label == "🌈 02"
    assert parse_volume_descriptor("Color Study Vol.2.cbz").label == "02"
    assert parse_volume_descriptor("カラーマン 第02巻.cbz").label == "02"


def test_reader_title_marks_color_edition_once_and_hides_rating_tag():
    assert display_label("作品 カラー版{zpi$r=4}.zip") == "🌈 作品 カラー版.zip"
    assert display_label("🌈 作品 カラー版.zip") == "🌈 作品 カラー版.zip"


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


def test_volume_progress_ui_keeps_last_read_date_on_mobile():
    js = Path("framedeck/web/static/js/volume_view.js").read_text("utf-8")
    css = Path("framedeck/web/static/css/app.css").read_text("utf-8")

    assert 'className = "volume-progress"' in js
    assert 'className = "volume-last-read"' in js
    assert 'body.ui-mobile .volume-last-read { display: none; }' not in css
    assert '.volume-last-read { display: none; }' not in css
    assert '@media (max-width: 760px)' in css


def test_reading_summary_reports_exact_single_volume_progress():
    summary = _reading_summary([{
        "page_index": 24,
        "page_count": 100,
        "completed": 0,
        "updated_at": 1234.5,
    }], entry_count=1)

    assert summary == {
        "status": "reading",
        "completed": False,
        "progress_percent": 25,
        "page_count": 100,
        "page_count_complete": True,
        "updated_at": 1234.5,
    }


def test_reading_summary_marks_all_entries_completed():
    summary = _reading_summary([
        {"page_index": 9, "page_count": 10, "completed": 1, "updated_at": 100},
        {"page_index": 19, "page_count": 20, "completed": 1, "updated_at": 200},
    ], entry_count=2)

    assert summary["status"] == "completed"
    assert summary["progress_percent"] == 100
    assert summary["page_count"] == 30
    assert summary["updated_at"] == 200


def test_partial_nested_progress_does_not_claim_overall_percentage():
    summary = _reading_summary([{
        "page_index": 4,
        "page_count": 10,
        "completed": 0,
        "updated_at": 100,
    }], entry_count=2)

    assert summary["status"] == "reading"
    assert summary["progress_percent"] is None
    assert summary["page_count"] == 10
    assert summary["page_count_complete"] is False
