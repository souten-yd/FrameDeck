"""灰色レターボックスのトリミングと見開き分割(単ページモード)のテスト。

実例: スクリーンショット形式の漫画アーカイブ(16:9、左右と綴じ目が
濃いグレー、ページ内容は上下端まで達する)。
"""
import io
import zipfile

import pytest
from PIL import Image, ImageDraw

GRAY = (51, 51, 51)


def _spread_screenshot(width=2000, height=1000, bar=150, gutter=40,
                       bar_color=GRAY):
    """左右にグレー帯、中央にグレー綴じ目を持つ2ページ分の横長画像。"""
    img = Image.new("RGB", (width, height), bar_color)
    draw = ImageDraw.Draw(img)
    page_w = (width - 2 * bar - gutter) // 2
    for x0 in (bar, bar + page_w + gutter):
        draw.rectangle((x0, 0, x0 + page_w, height), fill=(255, 255, 255))
        draw.rectangle((x0 + 30, 40, x0 + page_w - 30, height - 40),
                       outline=(20, 20, 20), width=6)
        for y in range(80, height - 120, 90):
            draw.rectangle((x0 + 60, y, x0 + page_w - 60, y + 30),
                           fill=(40, 40, 40))
    return img


def _portrait_page(width=800, height=1200):
    img = Image.new("RGB", (width, height), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    draw.rectangle((50, 50, width - 50, height - 50), outline=(0, 0, 0), width=8)
    for y in range(100, height - 150, 120):
        draw.rectangle((90, y, width - 90, y + 40), fill=(60, 60, 60))
    return img


def _write_zip(path, images):
    with zipfile.ZipFile(path, "w") as zf:
        for i, img in enumerate(images):
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=90)
            zf.writestr(f"p{i:02d}.jpg", buf.getvalue())


def test_crop_detects_gray_letterbox_bars():
    """辺全体でなく四隅から背景を推定する(上下端に内容が接していても)。"""
    from framedeck.comic.crop_detector import detect_crop_box

    det = detect_crop_box(_spread_screenshot())
    assert det.border_type == "gray"
    assert det.crop_box is not None
    left, top, right, bottom = det.crop_box
    assert left == pytest.approx(150, abs=25)
    assert right == pytest.approx(2000 - 150, abs=25)
    assert top <= 15 and bottom >= 985  # 上下は内容が端まであるため切らない


def test_spread_detected_on_typical_cropped_aspect():
    """トリミング後の典型的な見開き比率(~1.4-1.7)で検出できる。"""
    from framedeck.comic.crop_detector import detect_crop_box
    from framedeck.comic.spread_detector import detect_spread

    img = _spread_screenshot()
    det = detect_crop_box(img)
    base = img.crop(det.crop_box)
    spread = detect_spread(base)
    assert spread.is_spread is True
    assert abs(spread.split_x - base.width / 2) < base.width * 0.05


def test_single_mode_splits_landscape_and_keeps_portrait(app_env, tmp_path):
    """見開きと単ページの混在: 横長のみ半分ずつ、縦長はそのまま表示。"""
    services, _, _ = app_env
    root = tmp_path / "mix"
    root.mkdir()
    _write_zip(root / "M.cbz",
               [_portrait_page(), _spread_screenshot(), _portrait_page()])

    sequence = services.sequence_builder.build_sequence(str(root))
    entry = sequence.entries_at(0)[0]
    engine = services.comic_engine
    state = engine.create_session(str(root), entry.id)
    sid = state.session_id
    state = engine.set_view_options(sid, view_mode="single")
    state = engine.goto_page(sid, 0)
    assert state.visible_page_sides == ("full",)

    # RTL: 横長ページは右半分 → 左半分の順
    state = engine.next_page(sid)
    assert state.visible_pages == (1,)
    assert state.visible_page_sides == ("right",)
    state = engine.next_page(sid)
    assert state.visible_pages == (1,)
    assert state.visible_page_sides == ("left",)
    state = engine.next_page(sid)
    assert state.visible_pages == (2,)
    assert state.visible_page_sides == ("full",)

    # 後退時は前ページの最後の半分(左)から
    state = engine.previous_page(sid)
    assert state.visible_pages == (1,)
    assert state.visible_page_sides == ("left",)
    state = engine.previous_page(sid)
    assert state.visible_page_sides == ("right",)
    state = engine.previous_page(sid)
    assert state.visible_pages == (0,)
    assert state.visible_page_sides == ("full",)


def test_split_disabled_keeps_full_pages(app_env, tmp_path):
    services, _, _ = app_env
    root = tmp_path / "nosplit"
    root.mkdir()
    _write_zip(root / "M.cbz", [_spread_screenshot(), _spread_screenshot()])

    sequence = services.sequence_builder.build_sequence(str(root))
    entry = sequence.entries_at(0)[0]
    engine = services.comic_engine
    state = engine.create_session(str(root), entry.id)
    sid = state.session_id
    state = engine.set_view_options(sid, view_mode="single")
    state = engine.set_view_options(sid, split_spread_in_single_mode=False)
    state = engine.goto_page(sid, 0)
    assert state.visible_page_sides == ("full",)
    state = engine.next_page(sid)
    assert state.visible_pages == (1,)
    assert state.visible_page_sides == ("full",)


def test_stable_analysis_rescues_page_with_unknown_border(app_env, tmp_path):
    """自ページの検出が失敗しても、一致する近傍の中央値で救済される。"""
    services, _, _ = app_env
    root = tmp_path / "rescue"
    root.mkdir()
    odd = _spread_screenshot(bar_color=GRAY)
    draw = ImageDraw.Draw(odd)
    # 四隅を別々の色にして単独検出を失敗させる(colored_or_unknown)
    draw.rectangle((0, 0, 60, 60), fill=(200, 30, 30))
    draw.rectangle((2000 - 60, 1000 - 60, 2000, 1000), fill=(30, 30, 200))
    pages = [_spread_screenshot(), _spread_screenshot(), odd,
             _spread_screenshot(), _spread_screenshot()]
    _write_zip(root / "R.cbz", pages)

    sequence = services.sequence_builder.build_sequence(str(root))
    entry = sequence.entries_at(0)[0]
    engine = services.comic_engine
    state = engine.create_session(str(root), entry.id)

    own = services.pipeline.analyze_page  # 単独解析
    stable = engine.analyze_page(state.session_id, 2)  # 合議解析
    assert stable.crop_box is not None
    left, top, right, bottom = stable.crop_box
    assert left == pytest.approx(150, abs=25)
    assert right == pytest.approx(2000 - 150, abs=25)
    assert stable.split_x is not None
    assert abs(stable.split_x - 1000) < 60


def test_disk_cache_prune_removes_oldest_first(app_env, tmp_path):
    """漫画ディスクキャッシュは上限超過時に古い順で削除される。"""
    import os
    services, _, _ = app_env
    pipeline = services.pipeline
    variant_dir = pipeline._variant_cache_dir
    page_dir = pipeline._page_cache_dir
    variant_dir.mkdir(parents=True, exist_ok=True)
    page_dir.mkdir(parents=True, exist_ok=True)

    old_file = variant_dir / "old.webp"
    old_file.write_bytes(b"x" * 600)
    os.utime(old_file, (1000, 1000))
    mid_file = page_dir / "mid.jpg"
    mid_file.write_bytes(b"x" * 600)
    os.utime(mid_file, (2000, 2000))
    new_file = variant_dir / "new.webp"
    new_file.write_bytes(b"x" * 600)

    pipeline.prune_disk_caches(max_bytes=1500)
    assert not old_file.exists()      # 合計1800Bが上限1500B超過 → 8割まで古い順に削除
    assert mid_file.exists()
    assert new_file.exists()

    # 上限0(無制限)では何も消さない
    mid_file2 = page_dir / "mid2.jpg"
    mid_file2.write_bytes(b"x" * 600)
    pipeline.prune_disk_caches(max_bytes=0)
    assert mid_file2.exists()
