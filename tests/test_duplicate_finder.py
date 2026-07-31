"""重複候補抽出のテスト。

規則: 拡張子と評価タグを除いた名前を数字込みで比較し、差異2文字以下かつ
名前に含まれる数値が一致するものを重複とみなす。
"""
from framedeck.core.duplicate_finder import (
    DuplicateCandidate,
    bounded_distance,
    digit_signature,
    find_duplicate_groups,
    normalized_name,
    stale_duplicate_ids,
)


def test_normalized_name_drops_extension_and_rating_tag_but_keeps_digits():
    assert normalized_name("Comic Vol.12.cbz") == "comic vol.12"
    assert normalized_name("Ｃｏｍｉｃ　１２") == "comic 12"
    assert normalized_name("Folder 03", is_dir=True) == "folder 03"
    assert normalized_name("Comic 03{zpi$r=4}.zip") == "comic 03"


def test_digit_signature_ignores_zero_padding():
    assert digit_signature("第01巻") == digit_signature("第1巻") == (1,)
    assert digit_signature("第02巻") == (2,)
    assert digit_signature("なし") == ()


def test_bounded_distance_stops_at_limit():
    assert bounded_distance("abc", "abc", 2) == 0
    assert bounded_distance("abc", "abd", 2) == 1
    assert bounded_distance("abc", "axy", 2) == 2
    assert bounded_distance("abc", "xyz", 2) == 3          # 打ち切り値
    assert bounded_distance("abc", "abcdef", 2) == 3       # 長さ差で即打ち切り


def _c(id_, name, mtime, media_type="comic"):
    return DuplicateCandidate(id=id_, display_name=name, modified_at=mtime,
                              media_type=media_type)


def test_same_volume_with_different_extension_or_suffix_is_duplicate():
    """報告例: 拡張子違い・末尾1文字違いは同一とみなす。"""
    groups = find_duplicate_groups([
        _c("1", "転生したらスライムだった件_第01巻.zip", 100),
        _c("2", "転生したらスライムだった件_第01巻.rar", 300),
        _c("3", "転生したらスライムだった件_第01巻s.zip", 200),
        _c("4", "転生したらスライムだった件_第02巻.zip", 400),   # 巻が違う
        _c("5", "まったく別の作品.zip", 500),
    ])
    assert len(groups) == 1
    assert [c.id for c in groups[0]] == ["2", "3", "1"]      # 新しい順
    assert stale_duplicate_ids(groups) == ["3", "1"]         # 古い方を選択


def test_different_volume_numbers_are_not_duplicates():
    groups = find_duplicate_groups([
        _c("1", "Title 01.cbz", 100),
        _c("2", "Title 02.cbz", 200),
        _c("3", "Title 10.cbz", 300),
    ])
    assert groups == []


def test_zero_padding_and_rating_tag_do_not_split_groups():
    groups = find_duplicate_groups([
        _c("1", "Title 1.cbz", 100),
        _c("2", "Title 01{zpi$r=3}.cbz", 200),
    ])
    assert [c.id for c in groups[0]] == ["2", "1"]


def test_three_or_more_character_difference_is_not_duplicate():
    groups = find_duplicate_groups([
        _c("1", "Title 03.cbz", 100),
        _c("2", "Title 03!!!.cbz", 200),
    ])
    assert groups == []


def test_different_media_types_are_not_grouped():
    groups = find_duplicate_groups([
        _c("1", "Same.cbz", 100, "comic"),
        _c("2", "Same", 200, "folder"),
    ])
    assert groups == []


def test_extension_difference_is_a_duplicate():
    groups = find_duplicate_groups([
        _c("1", "Movie 1.mp4", 100, "video"),
        _c("2", "Movie 1.mkv", 200, "video"),
    ])
    assert [c.id for c in groups[0]] == ["2", "1"]


def test_extension_length_does_not_count_toward_the_difference():
    """拡張子は判定対象だが文字数の差には含めない(.mp4 と .mpeg でも同一)。"""
    groups = find_duplicate_groups([
        _c("1", "Movie 1.mp4", 100, "video"),
        _c("2", "Movie 1.mpeg", 200, "video"),
    ])
    assert len(groups[0]) == 2
