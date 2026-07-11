"""Virtual pages derived from physical comic images."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class VirtualPageRef:
    source_page_index: int
    crop_box: tuple[int, int, int, int]
    side: Literal["left", "right", "full"]
    display_order: int


def split_virtual_pages(
    source_page_index: int,
    source_size: tuple[int, int],
    split_x: int | None,
    reading_direction: str = "rtl",
) -> list[VirtualPageRef]:
    width, height = source_size
    if split_x is None or split_x <= 0 or split_x >= width:
        return [VirtualPageRef(source_page_index, (0, 0, width, height), "full", 0)]
    left = VirtualPageRef(source_page_index, (0, 0, split_x, height), "left", 0)
    right = VirtualPageRef(source_page_index, (split_x, 0, width, height), "right", 0)
    ordered = [right, left] if reading_direction == "rtl" else [left, right]
    return [
        VirtualPageRef(v.source_page_index, v.crop_box, v.side, i)
        for i, v in enumerate(ordered)
    ]
