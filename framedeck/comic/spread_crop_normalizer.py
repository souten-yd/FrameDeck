"""Spread-level crop normalization for paired comic pages."""
from __future__ import annotations

from dataclasses import dataclass

from .image_analysis import ComicImageAnalysis


@dataclass(frozen=True)
class RenderedPageGeometry:
    source_width: int
    source_height: int
    crop_box: tuple[int, int, int, int]
    cropped_width: int
    cropped_height: int
    output_width: int
    output_height: int


@dataclass(frozen=True)
class SpreadRenderPlan:
    left_page: RenderedPageGeometry
    right_page: RenderedPageGeometry
    common_height: int
    total_width: int
    left_padding: tuple[int, int, int, int]
    right_padding: tuple[int, int, int, int]
    normalization_mode: str


@dataclass(frozen=True)
class SpreadCropResult:
    left_crop_box: tuple[int, int, int, int]
    right_crop_box: tuple[int, int, int, int]
    mode: str


def _full_box(analysis: ComicImageAnalysis) -> tuple[int, int, int, int]:
    return (0, 0, analysis.source_width, analysis.source_height)


def _ratios(analysis: ComicImageAnalysis) -> tuple[float, float, float, float]:
    left, top, right, bottom = analysis.crop_box or _full_box(analysis)
    width = max(1, analysis.source_width)
    height = max(1, analysis.source_height)
    return (
        left / width,
        top / height,
        (width - right) / width,
        (height - bottom) / height,
    )


def _box_from_ratios(
    analysis: ComicImageAnalysis,
    ratios: tuple[float, float, float, float],
) -> tuple[int, int, int, int]:
    left_ratio, top_ratio, right_ratio, bottom_ratio = ratios
    width = analysis.source_width
    height = analysis.source_height
    left = max(0, min(width - 1, round(width * left_ratio)))
    top = max(0, min(height - 1, round(height * top_ratio)))
    right = max(left + 1, min(width, round(width * (1.0 - right_ratio))))
    bottom = max(top + 1, min(height, round(height * (1.0 - bottom_ratio))))
    return (left, top, right, bottom)


class SpreadCropNormalizer:
    def normalize(
        self,
        left_analysis: ComicImageAnalysis,
        right_analysis: ComicImageAnalysis,
        *,
        mode: str = "shared_vertical",
    ) -> SpreadCropResult:
        if mode == "off":
            return SpreadCropResult(_full_box(left_analysis), _full_box(right_analysis), mode)
        left = _ratios(left_analysis)
        right = _ratios(right_analysis)
        if mode == "independent":
            return SpreadCropResult(
                left_analysis.crop_box or _full_box(left_analysis),
                right_analysis.crop_box or _full_box(right_analysis),
                mode,
            )
        if mode == "shared_all":
            shared = tuple(min(left[i], right[i]) for i in range(4))
            return SpreadCropResult(
                _box_from_ratios(left_analysis, shared),
                _box_from_ratios(right_analysis, shared),
                mode,
            )
        # shared_vertical: top/bottom shared, horizontal remains independent.
        shared_top = min(left[1], right[1])
        shared_bottom = min(left[3], right[3])
        left_shared = (left[0], shared_top, left[2], shared_bottom)
        right_shared = (right[0], shared_top, right[2], shared_bottom)
        return SpreadCropResult(
            _box_from_ratios(left_analysis, left_shared),
            _box_from_ratios(right_analysis, right_shared),
            "shared_vertical",
        )


def plan_spread_layout(
    left_size: tuple[int, int],
    right_size: tuple[int, int],
    available_width: int,
    available_height: int,
    *,
    allow_upscale: bool = False,
    mode: str = "shared_vertical",
) -> SpreadRenderPlan:
    lw, lh = max(1, left_size[0]), max(1, left_size[1])
    rw, rh = max(1, right_size[0]), max(1, right_size[1])
    common_height = min(max(lh, rh), max(1, available_height))
    if not allow_upscale:
        common_height = min(common_height, lh, rh)
    left_width = max(1, round(lw * common_height / lh))
    right_width = max(1, round(rw * common_height / rh))
    total_width = left_width + right_width
    if total_width > available_width:
        scale = max(0.0, available_width / max(1, total_width))
        common_height = max(1, int(common_height * scale))
        left_width = max(1, int(left_width * scale))
        right_width = max(1, available_width - left_width)
        total_width = left_width + right_width
    left = RenderedPageGeometry(lw, lh, (0, 0, lw, lh), lw, lh, left_width, common_height)
    right = RenderedPageGeometry(rw, rh, (0, 0, rw, rh), rw, rh, right_width, common_height)
    return SpreadRenderPlan(left, right, common_height, total_width, (0, 0, 0, 0), (0, 0, 0, 0), mode)
