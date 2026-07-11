"""ファイル名末尾 `{zpi$r=N}` 形式の評価アダプター。

内部では評価をメタデータとして扱い、ファイル名の解析・リネームは
このモジュールに閉じ込める。
"""
from __future__ import annotations

import os
import re

RATING_PATTERN = re.compile(r"\{zpi\$r=([1-5])\}$")


def natural_key(text: str):
    return [int(part) if part.isdigit() else part.lower()
            for part in re.split(r"(\d+)", text)]


def parse_name(stem: str) -> tuple[str, int | None]:
    match = RATING_PATTERN.search(stem)
    if match:
        return stem[: match.start()], int(match.group(1))
    return stem, None


def build_name(clean_stem: str, rating: int | None, ext: str) -> str:
    if rating:
        return f"{clean_stem}{{zpi$r={rating}}}{ext}"
    return f"{clean_stem}{ext}"


def rating_of_path(path: str) -> int | None:
    name = os.path.basename(path.rstrip(os.sep))
    if os.path.isdir(path):
        stem, ext = name, ""
    else:
        stem, ext = os.path.splitext(name)
    return parse_name(stem)[1]


def display_name_of_path(path: str) -> str:
    name = os.path.basename(path.rstrip(os.sep))
    if os.path.isdir(path):
        stem, ext = name, ""
    else:
        stem, ext = os.path.splitext(name)
    clean, _ = parse_name(stem)
    return f"{clean}{ext}"


class RatingService:
    """評価の読み取りと、既存仕様どおりのリネームによる保存。"""

    def __init__(self, storage=None):
        self._storage = storage

    def get_rating(self, path: str) -> int | None:
        return rating_of_path(path)

    def set_rating(self, path: str, rating: int | None) -> str:
        """評価を設定し、リネーム後の新パスを返す。"""
        if rating is not None and rating not in (1, 2, 3, 4, 5):
            raise ValueError(f"評価は1〜5またはNoneです: {rating}")
        folder = os.path.dirname(path.rstrip(os.sep))
        name = os.path.basename(path.rstrip(os.sep))
        if os.path.isdir(path):
            stem, ext = name, ""
        else:
            stem, ext = os.path.splitext(name)
        clean, _ = parse_name(stem)
        new_name = build_name(clean, rating, ext)
        if new_name == name:
            return path
        new_path = os.path.join(folder, new_name)
        if os.path.exists(new_path):
            raise FileExistsError(f"同名のファイルが既に存在します: {new_name}")
        os.rename(path, new_path)
        return new_path
