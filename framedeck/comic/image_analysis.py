"""Comic image analysis result models."""
from __future__ import annotations

from dataclasses import dataclass

ANALYSIS_VERSION = "comic-analysis-v1"


@dataclass(frozen=True)
class ComicImageAnalysis:
    source_width: int
    source_height: int
    border_type: str
    crop_box: tuple[int, int, int, int] | None
    crop_confidence: float
    is_spread: bool
    spread_confidence: float
    split_x: int | None
    center_gutter_type: str | None
    has_center_crossing_content: bool
    analysis_version: str = ANALYSIS_VERSION

    def to_dict(self) -> dict:
        return {
            "source_width": self.source_width,
            "source_height": self.source_height,
            "border_type": self.border_type,
            "crop_box": self.crop_box,
            "crop_confidence": self.crop_confidence,
            "is_spread": self.is_spread,
            "spread_confidence": self.spread_confidence,
            "split_x": self.split_x,
            "center_gutter_type": self.center_gutter_type,
            "has_center_crossing_content": self.has_center_crossing_content,
            "analysis_version": self.analysis_version,
        }
