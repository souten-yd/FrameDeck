"""ファイル名の近似一致による重複候補の抽出。

判定規則(UI仕様):
  - 比較対象は表示名から**拡張子と評価タグを除いた**文字列。
    拡張子は判定対象(違っても重複扱い)だが、文字数の差には数えない。
  - 数字も含めて比較し、編集距離(Levenshtein)が閾値(既定2文字)以下なら
    同一グループ。
  - ただし名前に含まれる**数値が一致すること**を条件にする。
    数値が違うものは別の巻・話であり、1文字差でも重複ではないため
    (例: 第01巻 と 第02巻)。
  - グループ内で更新日時が最も新しいものを「残す」候補、それ以外を
    「選択(削除候補)」として返す。

    例(すべて同一グループ):
        転生したらスライムだった件_第01巻.zip
        転生したらスライムだった件_第01巻.rar
        転生したらスライムだった件_第01巻s.zip

削除は行わない。誤検出時に取り返しがつかないため、UI側では常に
「選択するだけ」に留める。
"""
from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass

from .comic_edition import is_color_edition

_DIGITS = re.compile(r"[0-9]+")
_RATING_TAG = re.compile(r"\{zpi\$r=[1-5]\}")

DEFAULT_MAX_DISTANCE = 2
MAX_COMPARE_ITEMS = 4000


@dataclass(frozen=True)
class DuplicateCandidate:
    id: str
    display_name: str
    modified_at: float
    media_type: str = "file"
    rating: int | None = None
    size: int | None = None


def normalized_name(display_name: str, is_dir: bool = False) -> str:
    """比較用に正規化した名前(拡張子と評価タグを除去、数字は残す)。"""
    stem = display_name if is_dir else os.path.splitext(display_name)[0]
    text = unicodedata.normalize("NFKC", stem).casefold()
    return _RATING_TAG.sub("", text).strip()


def digit_signature(normalized: str) -> tuple[int, ...]:
    """名前に含まれる数値の並び。`01` と `1` は同じ値として扱う。"""
    return tuple(int(part) for part in _DIGITS.findall(normalized))


def bounded_distance(a: str, b: str, max_distance: int) -> int:
    """編集距離。max_distance を超えることが確定した時点で打ち切る。

    打ち切った場合は max_distance + 1 を返す。
    """
    if a == b:
        return 0
    if abs(len(a) - len(b)) > max_distance:
        return max_distance + 1
    if len(a) > len(b):
        a, b = b, a
    previous = list(range(len(a) + 1))
    for j, cb in enumerate(b, start=1):
        current = [j]
        best = j
        for i, ca in enumerate(a, start=1):
            cost = 0 if ca == cb else 1
            value = min(previous[i] + 1, current[i - 1] + 1, previous[i - 1] + cost)
            current.append(value)
            if value < best:
                best = value
        if best > max_distance:
            return max_distance + 1
        previous = current
    return previous[-1]


def _group_indexes(names: list[str], max_distance: int) -> list[list[int]]:
    parent = list(range(len(names)))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: int, y: int) -> None:
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[max(rx, ry)] = min(rx, ry)

    order = sorted(range(len(names)), key=lambda i: len(names[i]))
    for pos, i in enumerate(order):
        for j in order[pos + 1:]:
            if len(names[j]) - len(names[i]) > max_distance:
                break  # 長さ順なので以降も候補外
            if find(i) == find(j):
                continue
            if bounded_distance(names[i], names[j], max_distance) <= max_distance:
                union(i, j)

    buckets: dict[int, list[int]] = {}
    for index in range(len(names)):
        buckets.setdefault(find(index), []).append(index)
    return [sorted(group) for group in buckets.values() if len(group) > 1]


def find_duplicate_groups(
    candidates: list[DuplicateCandidate],
    max_distance: int = DEFAULT_MAX_DISTANCE,
) -> list[list[DuplicateCandidate]]:
    """重複グループを返す。各グループは更新日時の新しい順に並ぶ。"""
    if len(candidates) > MAX_COMPARE_ITEMS:
        candidates = candidates[:MAX_COMPARE_ITEMS]
    # 種別と数値の並びが一致するものだけを比較対象にする
    buckets: dict[tuple, list[tuple[DuplicateCandidate, str]]] = {}
    for candidate in candidates:
        name = normalized_name(candidate.display_name,
                               candidate.media_type == "folder")
        # 通常版とカラー版は同じ巻数・近い名前でも別商品として共存させる。
        # 動画のタイトルに ``color`` が含まれるケースには影響させない。
        color_edition = (
            is_color_edition(candidate.display_name)
            if candidate.media_type in ("comic", "folder") else False
        )
        key = (candidate.media_type, digit_signature(name), color_edition)
        buckets.setdefault(key, []).append((candidate, name))

    groups: list[list[DuplicateCandidate]] = []
    for bucket in buckets.values():
        if len(bucket) < 2:
            continue
        names = [name for _, name in bucket]
        for indexes in _group_indexes(names, max_distance):
            group = [bucket[i][0] for i in indexes]
            group.sort(key=lambda item: (-item.modified_at, item.display_name.lower()))
            groups.append(group)
    groups.sort(key=lambda group: group[0].display_name.lower())
    return groups


def stale_duplicate_ids(groups: list[list[DuplicateCandidate]]) -> list[str]:
    """各グループの最新1件を除いたID(=古い方)を返す。"""
    stale: list[str] = []
    for group in groups:
        stale.extend(item.id for item in group[1:])
    return stale
