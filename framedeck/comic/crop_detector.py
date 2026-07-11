"""White/gray/black margin detection for comic pages."""
from __future__ import annotations

from dataclasses import dataclass
from statistics import median

from PIL import Image, ImageStat


@dataclass(frozen=True)
class CropDetection:
    border_type: str
    crop_box: tuple[int, int, int, int] | None
    confidence: float
    background_rgb: tuple[int, int, int]


def _rgb(image: Image.Image) -> Image.Image:
    return image.convert("RGB") if image.mode != "RGB" else image


def _luminance(rgb: tuple[int, int, int]) -> float:
    r, g, b = rgb
    return (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255.0


def _saturation(rgb: tuple[int, int, int]) -> float:
    high = max(rgb)
    low = min(rgb)
    return 0.0 if high == 0 else (high - low) / high


def _classify_border(rgb: tuple[int, int, int], variance: float) -> str:
    lum = _luminance(rgb)
    sat = _saturation(rgb)
    if lum >= 0.90:
        return "white"
    if lum <= 0.10:
        return "black"
    if sat <= 0.08 and variance < 600:
        return "gray"
    return "colored_or_unknown"


def _stat_boxes(image: Image.Image,
                boxes: list[tuple[int, int, int, int]]
                ) -> tuple[list[tuple[int, int, int]], list[float]]:
    samples: list[tuple[int, int, int]] = []
    variances: list[float] = []
    for box in boxes:
        crop = image.crop(box)
        stat = ImageStat.Stat(crop)
        samples.append(tuple(int(v) for v in stat.median[:3]))
        variances.extend(float(v) for v in stat.var[:3])
    return samples, variances


def _sample_background(image: Image.Image) -> tuple[tuple[int, int, int], float]:
    """余白の背景色を推定する。

    まず四隅のパッチを見る。ページ内容が上下端(または左右端)まで達する
    レターボックス画像では辺全体のサンプリングが内容に汚染されるため、
    四隅が一致する場合はそれを背景とする。一致しない場合のみ従来の
    辺バンドへフォールバックする。
    """
    width, height = image.size
    band = max(2, min(width, height) // 40)
    corner_boxes = [
        (0, 0, band, band),
        (width - band, 0, width, band),
        (0, height - band, band, height),
        (width - band, height - band, width, height),
    ]
    corners, corner_vars = _stat_boxes(image, corner_boxes)
    max_diff = max(
        abs(corner[ch] - other[ch])
        for corner in corners for other in corners for ch in range(3)
    )
    if max_diff <= 24:
        rgb = tuple(int(median(channel)) for channel in zip(*corners))
        return rgb, float(median(corner_vars)) if corner_vars else 0.0

    edge_boxes = [
        (0, 0, width, band),
        (0, height - band, width, height),
        (0, 0, band, height),
        (width - band, 0, width, height),
    ]
    samples, variances = _stat_boxes(image, edge_boxes)
    rgb = tuple(int(median(channel)) for channel in zip(*samples))
    return rgb, float(median(variances)) if variances else 0.0


def _near_background(pixel: tuple[int, int, int], bg: tuple[int, int, int], tolerance: int) -> bool:
    return sum(abs(int(pixel[i]) - bg[i]) for i in range(3)) / 3 <= tolerance


def _blank_ratio_row(image: Image.Image, y: int, bg: tuple[int, int, int], tolerance: int) -> float:
    pixels = image.crop((0, y, image.width, y + 1)).getdata()
    near = sum(1 for p in pixels if _near_background(p, bg, tolerance))
    return near / max(1, image.width)


def _blank_ratio_col(image: Image.Image, x: int, bg: tuple[int, int, int], tolerance: int) -> float:
    pixels = image.crop((x, 0, x + 1, image.height)).getdata()
    near = sum(1 for p in pixels if _near_background(p, bg, tolerance))
    return near / max(1, image.height)


def _scan_top(image: Image.Image, bg: tuple[int, int, int], tolerance: int, threshold: float) -> int:
    for y in range(image.height):
        if _blank_ratio_row(image, y, bg, tolerance) < threshold:
            return y
    return 0


def _scan_bottom(image: Image.Image, bg: tuple[int, int, int], tolerance: int, threshold: float) -> int:
    for y in range(image.height - 1, -1, -1):
        if _blank_ratio_row(image, y, bg, tolerance) < threshold:
            return y + 1
    return image.height


def _scan_left(image: Image.Image, bg: tuple[int, int, int], tolerance: int, threshold: float) -> int:
    for x in range(image.width):
        if _blank_ratio_col(image, x, bg, tolerance) < threshold:
            return x
    return 0


def _scan_right(image: Image.Image, bg: tuple[int, int, int], tolerance: int, threshold: float) -> int:
    for x in range(image.width - 1, -1, -1):
        if _blank_ratio_col(image, x, bg, tolerance) < threshold:
            return x + 1
    return image.width


ANALYSIS_MAX_EDGE = 1280


def _analysis_scale(image: Image.Image,
                    max_edge: int = ANALYSIS_MAX_EDGE) -> tuple[Image.Image, float]:
    """検出はダウンスケール画像で行う(列/行の走査がPythonループのため)。"""
    scale = max(image.width, image.height) / float(max_edge)
    if scale <= 1.0:
        return image, 1.0
    size = (max(1, round(image.width / scale)), max(1, round(image.height / scale)))
    return image.resize(size, Image.Resampling.BILINEAR), scale


def detect_crop_box(
    image: Image.Image,
    tolerance: int = 18,
    safety_margin: int = 4,
    max_crop_ratio: float = 0.18,
    allowed_border_types: set[str] | None = None,
) -> CropDetection:
    full_width, full_height = image.size
    image = _rgb(image)
    image, scale = _analysis_scale(image)
    bg, variance = _sample_background(image)
    border_type = _classify_border(bg, variance)
    if border_type == "colored_or_unknown":
        return CropDetection(border_type, None, 0.0, bg)
    if allowed_border_types is not None and border_type not in allowed_border_types:
        return CropDetection(border_type, None, 0.0, bg)

    threshold = 0.97
    left = _scan_left(image, bg, tolerance, threshold)
    top = _scan_top(image, bg, tolerance, threshold)
    right = _scan_right(image, bg, tolerance, threshold)
    bottom = _scan_bottom(image, bg, tolerance, threshold)

    width, height = image.size
    crop_amounts = (left, top, width - right, height - bottom)
    if max(crop_amounts) <= 0:
        return CropDetection(border_type, None, 0.0, bg)

    if left >= right or top >= bottom:
        return CropDetection(border_type, None, 0.0, bg)

    ratios = (left / width, top / height, (width - right) / width, (height - bottom) / height)
    confidence = 0.95 - min(0.45, max(0.0, variance) / 2000.0)
    if max(ratios) > max_crop_ratio and confidence < 0.90:
        return CropDetection(border_type, None, confidence, bg)

    # 元解像度へ戻す。縮小走査の量子化誤差はsafety_marginへ吸収する
    margin = safety_margin + (int(scale) + 1 if scale > 1.0 else 0)
    box = (
        max(0, int(left * scale) - margin),
        max(0, int(top * scale) - margin),
        min(full_width, int(right * scale + 0.5) + margin),
        min(full_height, int(bottom * scale + 0.5) + margin),
    )
    if box == (0, 0, full_width, full_height):
        return CropDetection(border_type, None, confidence, bg)
    return CropDetection(border_type, box, confidence, bg)
