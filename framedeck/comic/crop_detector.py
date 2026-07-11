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


def _sample_background(image: Image.Image) -> tuple[tuple[int, int, int], float]:
    width, height = image.size
    band = max(2, min(width, height) // 40)
    boxes = [
        (0, 0, width, band),
        (0, height - band, width, height),
        (0, 0, band, height),
        (width - band, 0, width, height),
    ]
    samples: list[tuple[int, int, int]] = []
    variances: list[float] = []
    for box in boxes:
        crop = image.crop(box)
        stat = ImageStat.Stat(crop)
        samples.append(tuple(int(v) for v in stat.median[:3]))
        variances.extend(float(v) for v in stat.var[:3])
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


def detect_crop_box(
    image: Image.Image,
    tolerance: int = 18,
    safety_margin: int = 4,
    max_crop_ratio: float = 0.18,
    allowed_border_types: set[str] | None = None,
) -> CropDetection:
    image = _rgb(image)
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

    box = (
        max(0, left - safety_margin),
        max(0, top - safety_margin),
        min(width, right + safety_margin),
        min(height, bottom + safety_margin),
    )
    if box == (0, 0, width, height):
        return CropDetection(border_type, None, confidence, bg)
    return CropDetection(border_type, box, confidence, bg)
