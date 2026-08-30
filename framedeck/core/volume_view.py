"""漫画フォルダを「読む単位」に正規化するための軽量解析。

ファイルシステムの並び順は信用せず、名前やアーカイブ内部の巻ディレクトリから
巻・巻範囲・話・外伝等を抽出する。解析不能なら従来表示へ安全に戻せる。
"""
from __future__ import annotations

from dataclasses import dataclass
import os
import re
import zipfile
from typing import Iterable


_ARCHIVE_EXTENSIONS = {".zip", ".cbz", ".rar", ".cbr", ".7z"}
_SPECIAL_WORDS = (
    "異聞", "外伝", "番外編", "特別編", "短編集", "特典", "おまけ", "extra", "special",
)
_SORT_LAST = 1_000_000_000

# 第01-03巻 / 第1巻～第3巻 / Vol.01-03 / BLACK_LAGOON_01b-03b
_VOLUME_RANGE_PATTERNS = (
    re.compile(r"第\s*(\d{1,4})\s*(?:巻\s*)?[\-–—〜～~]\s*(?:第\s*)?(\d{1,4})\s*巻", re.I),
    re.compile(r"\bvol(?:ume)?[.\s_-]*(\d{1,4})\s*[\-–—〜～~]\s*(\d{1,4})\b", re.I),
    # 「第」が無い compact range はstem末尾だけに限定する。
    re.compile(r"(?:^|[_\s])(?P<start>\d{1,3})[a-z]?\s*[\-–—〜～~]\s*(?P<end>\d{1,3})[a-z]?\s*(?:巻)?$", re.I),
)
_VOLUME_SET_PATTERN = re.compile(
    r"第?\s*(\d{1,4})\s*(?:巻)?\s*[・,+＋＆&]\s*第?\s*(\d{1,4})\s*巻",
    re.I,
)
_VOLUME_PATTERN = re.compile(r"第\s*(\d{1,4})\s*巻", re.I)
_VOL_PATTERN = re.compile(r"\bvol(?:ume)?[.\s_-]*(\d{1,4})\b", re.I)
_CHAPTER_PATTERN = re.compile(r"(?:第\s*)?(\d{1,5}(?:\.\d+)?)\s*(?:話|章)\b|\b(?:ch(?:apter)?)[.\s_-]*(\d{1,5}(?:\.\d+)?)\b", re.I)


@dataclass(frozen=True)
class VolumeDescriptor:
    kind: str
    label: str
    sort_key: tuple
    recognized: bool
    start: int | float | None = None
    end: int | float | None = None
    special: str | None = None
    confidence: float = 0.0


def _stem(name: str) -> str:
    base = os.path.basename(name)
    stem, ext = os.path.splitext(base)
    return stem if ext.lower() in _ARCHIVE_EXTENSIONS else base


def _special_label(text: str) -> str | None:
    low = text.lower()
    for word in _SPECIAL_WORDS:
        if word.lower() in low:
            if word.lower() == "extra":
                return "Extra"
            if word.lower() == "special":
                return "Special"
            return word
    return None


def _number_label(number: int | float) -> str:
    if isinstance(number, float) and not number.is_integer():
        return str(number).rstrip("0").rstrip(".")
    return f"{int(number):02d}"


def _archive_volume_dirs(path: str) -> list[int]:
    """ZIP/CBZのトップ階層に明示的な巻ディレクトリがあれば返す。

    画像を展開せずcentral directoryだけを読む。フラットな画像群から巻境界を
    推測することはしない。
    """
    if os.path.splitext(path)[1].lower() not in {".zip", ".cbz"}:
        return []
    try:
        with zipfile.ZipFile(path) as zf:
            roots: set[str] = set()
            for info in zf.infolist():
                normalized = info.filename.replace("\\", "/").strip("/")
                if not normalized or "/" not in normalized:
                    continue
                roots.add(normalized.split("/", 1)[0])
                if len(roots) > 64:
                    break
    except (OSError, zipfile.BadZipFile, RuntimeError):
        return []
    volumes: set[int] = set()
    for root in roots:
        match = _VOLUME_PATTERN.search(root) or _VOL_PATTERN.search(root)
        if match:
            volumes.add(int(match.group(1)))
    return sorted(volumes)


def _volume_sort_group(special: str | None) -> int:
    # 外伝・異聞等は本編の巻列に混ぜず、位置不明の特別系列として後段へ送る。
    return 4 if special else 1


def _descriptor_from_internal(name: str, volumes: list[int]) -> VolumeDescriptor | None:
    if not volumes:
        return None
    text = _stem(name)
    if len(volumes) == 1:
        number = volumes[0]
        return VolumeDescriptor(
            "volume", _number_label(number), (1, number, number, text.casefold()), True,
            number, number, confidence=0.90,
        )
    start, end = volumes[0], volumes[-1]
    contiguous = volumes == list(range(start, end + 1))
    label = (
        f"{_number_label(start)}–{_number_label(end)}"
        if contiguous else "・".join(_number_label(number) for number in volumes)
    )
    return VolumeDescriptor(
        "volume_range" if contiguous else "volume_set",
        label,
        (1, start, end, text.casefold()),
        True,
        start,
        end,
        confidence=1.0,
    )


def parse_volume_descriptor(name: str, *, path: str | None = None) -> VolumeDescriptor:
    text = _stem(name)
    special = _special_label(text)
    internal = _archive_volume_dirs(path) if path else []

    for pattern in _VOLUME_RANGE_PATTERNS:
        match = pattern.search(text)
        if match:
            groups = match.groupdict()
            if groups.get("start") is not None:
                start, end = int(groups["start"]), int(groups["end"])
            else:
                start, end = int(match.group(1)), int(match.group(2))
            if end < start:
                start, end = end, start
            confirmed = internal == list(range(start, end + 1))
            label = f"{_number_label(start)}–{_number_label(end)}"
            if special:
                label = f"{special} {label}"
            group = _volume_sort_group(special)
            return VolumeDescriptor(
                "volume_range", label, (group, start, end, text.casefold()), True,
                start, end, special, 1.0 if confirmed else 0.95,
            )

    match = _VOLUME_SET_PATTERN.search(text)
    if match:
        numbers = sorted({int(match.group(1)), int(match.group(2))})
        label = "・".join(_number_label(number) for number in numbers)
        if special:
            label = f"{special} {label}"
        group = _volume_sort_group(special)
        return VolumeDescriptor(
            "volume_set", label, (group, numbers[0], numbers[-1], text.casefold()), True,
            numbers[0], numbers[-1], special,
            1.0 if internal == numbers else 0.93,
        )

    match = _VOLUME_PATTERN.search(text) or _VOL_PATTERN.search(text)
    if match:
        number = int(match.group(1))
        label = _number_label(number)
        if special:
            label = f"{special} {label}"
        group = _volume_sort_group(special)
        return VolumeDescriptor(
            "volume", label, (group, number, number, text.casefold()), True,
            number, number, special, 0.98,
        )

    match = _CHAPTER_PATTERN.search(text)
    if match:
        raw = match.group(1) or match.group(2)
        number = float(raw) if "." in raw else int(raw)
        label = f"Ch. {_number_label(number)}"
        if special:
            label = f"{special} {label}"
        group = 5 if special else 2
        return VolumeDescriptor(
            "chapter", label, (group, float(number), float(number), text.casefold()), True,
            number, number, special, 0.90,
        )

    if special:
        return VolumeDescriptor(
            "special", special, (6, _SORT_LAST, _SORT_LAST, text.casefold()), True,
            special=special, confidence=0.75,
        )

    # 名前が無意味でも、内部に明示された巻ディレクトリがあれば利用する。
    internal_descriptor = _descriptor_from_internal(name, internal)
    if internal_descriptor:
        return internal_descriptor

    return VolumeDescriptor(
        "unknown", os.path.basename(name), (9, _SORT_LAST, _SORT_LAST, text.casefold()), False,
    )


def recommend_display_mode(descriptors: Iterable[VolumeDescriptor], *, has_folders: bool = False) -> tuple[str, float]:
    """auto表示の推奨値と認識率を返す。

    下位フォルダがある場所は作品一覧の可能性が高いため従来表示を優先する。
    ファイルだけなら70%以上を巻/話として認識できた時だけ巻表示にする。
    """
    values = list(descriptors)
    if not values or has_folders:
        return "files", 0.0
    recognized = sum(1 for value in values if value.recognized)
    ratio = recognized / len(values)
    return ("volume" if ratio >= 0.70 else "files"), ratio
