"""Conservative spread-page detection."""
from __future__ import annotations

from dataclasses import dataclass
from statistics import median

from PIL import Image, ImageFilter, ImageOps, ImageStat


@dataclass(frozen=True)
class SpreadDetection:
    is_spread: bool
    confidence: float
    split_x: int | None
    center_gutter_type: str | None
    has_center_crossing_content: bool


def _gray(image: Image.Image) -> Image.Image:
    return ImageOps.grayscale(image)


def _luminance_type(value: float) -> str:
    if value >= 230:
        return "white"
    if value <= 25:
        return "black"
    return "gray"


def _column_stats(gray: Image.Image, x: int, band: int) -> tuple[float, float]:
    half = max(1, band // 2)
    left = max(0, x - half)
    right = min(gray.width, x + half + 1)
    crop = gray.crop((left, 0, right, gray.height))
    stat = ImageStat.Stat(crop)
    return float(stat.mean[0]), float(stat.var[0])


def _edge_density(image: Image.Image, box: tuple[int, int, int, int]) -> float:
    crop = _gray(image.crop(box)).filter(ImageFilter.FIND_EDGES)
    hist = crop.histogram()
    total = crop.width * crop.height
    strong = sum(hist[32:])
    return strong / max(1, total)


def _content_density(gray: Image.Image, box: tuple[int, int, int, int]) -> float:
    crop = gray.crop(box)
    stat = ImageStat.Stat(crop)
    mean = stat.mean[0]
    data = crop.getdata()
    return sum(1 for v in data if abs(v - mean) > 18) / max(1, crop.width * crop.height)


def find_best_gutter(image: Image.Image, search_start: int | None = None,
                     search_end: int | None = None) -> tuple[int, str, float]:
    gray = _gray(image)
    width = gray.width
    start = search_start if search_start is not None else int(width * 0.40)
    end = search_end if search_end is not None else int(width * 0.60)
    band = max(3, width // 200)
    candidates = []
    for x in range(max(1, start), min(width - 1, end)):
        mean, var = _column_stats(gray, x, band)
        bg_score = max(mean / 255.0, 1.0 - mean / 255.0, 1.0 - abs(mean - 128) / 128.0)
        stability = max(0.0, 1.0 - var / 2200.0)
        distance = 1.0 - abs(x - width / 2) / max(1.0, width * 0.10)
        candidates.append((bg_score * 0.45 + stability * 0.45 + max(0.0, distance) * 0.10, x, mean))
    score, x, mean = max(candidates, default=(0.0, width // 2, 255.0))
    return x, _luminance_type(mean), score


DETECT_MAX_WIDTH = 1000


def detect_spread(image: Image.Image, threshold: float = 0.68) -> SpreadDetection:
    full_width, height = image.size
    if height <= 0:
        return SpreadDetection(False, 0.0, None, None, False)
    aspect = full_width / height
    if aspect < 1.35:
        return SpreadDetection(False, 0.0, None, None, False)

    # 中身の走査がPythonループのため縮小画像で検出し、座標だけ戻す
    scale = full_width / float(DETECT_MAX_WIDTH)
    if scale > 1.0:
        size = (DETECT_MAX_WIDTH, max(1, round(image.height / scale)))
        image = image.resize(size, Image.Resampling.BILINEAR)
    else:
        scale = 1.0
    width, height = image.size

    split_x, gutter_type, gutter_score = find_best_gutter(image)
    center_width = max(4, width // 80)
    center_box = (max(0, split_x - center_width), 0, min(width, split_x + center_width), height)
    center_edge = _edge_density(image, center_box)
    # A real gutter should be mostly calm. Strong edge activity in the center
    # band usually means art, panel lines, or text crosses the split candidate.
    has_crossing = center_edge > 0.13
    center_gutter_score = max(0.0, min(1.0, gutter_score - center_edge * 1.8))

    gray = _gray(image)
    margin = max(1, width // 40)
    left_box = (margin, 0, max(margin + 1, split_x - margin), height)
    right_box = (min(width - 1, split_x + margin), 0, max(split_x + margin + 1, width - margin), height)
    left_content = _content_density(gray, left_box)
    right_content = _content_density(gray, right_box)
    if max(left_content, right_content) < 0.08:
        return SpreadDetection(False, 0.0, None, gutter_type, has_crossing)
    balance = 1.0 - abs(left_content - right_content) / max(left_content, right_content, 0.01)
    balance = max(0.0, min(1.0, balance))

    # 典型的な見開き(B判2ページ分 ≒ 1.4)が満点近くになるようにする。
    # 1.35のゲートを通過した時点で横長は確定しているため、比率の寄与は小さく、
    # 綴じ目の明瞭さ・左右バランス・分割位置を主な根拠とする。
    aspect_score = min(1.0, max(0.0, (aspect - 1.30) / 0.20))
    page_shape_score = 1.0 if 0.42 <= split_x / width <= 0.58 else 0.0
    confidence = (
        aspect_score * 0.15
        + center_gutter_score * 0.45
        + balance * 0.20
        - (0.25 if has_crossing else 0.0)
        + page_shape_score * 0.20
    )
    confidence = max(0.0, min(1.0, confidence))
    if confidence < threshold:
        return SpreadDetection(False, confidence, None, gutter_type, has_crossing)
    return SpreadDetection(True, confidence, int(round(split_x * scale)),
                           gutter_type, has_crossing)
