"""Pixel and normalized rectangles.

Regions are stored normalized (0-1) to a source's frame space so they survive window moves,
resizes and resolution changes; they are turned into pixels only at the point of use.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field, model_validator

# Tolerance for float round-off when a normalized rect touches the right/bottom edge.
_EDGE_EPSILON = 1e-6


@dataclass(frozen=True, slots=True)
class Rect:
    """Axis-aligned rectangle in integer pixels (right/bottom exclusive)."""

    x: int
    y: int
    width: int
    height: int

    @property
    def right(self) -> int:
        return self.x + self.width

    @property
    def bottom(self) -> int:
        return self.y + self.height

    @property
    def is_empty(self) -> bool:
        return self.width <= 0 or self.height <= 0

    def intersection(self, other: Rect) -> Rect | None:
        left, top = max(self.x, other.x), max(self.y, other.y)
        right, bottom = min(self.right, other.right), min(self.bottom, other.bottom)
        if right <= left or bottom <= top:
            return None
        return Rect(left, top, right - left, bottom - top)

    def intersects(self, other: Rect) -> bool:
        return self.intersection(other) is not None

    def translated(self, dx: int, dy: int) -> Rect:
        return Rect(self.x + dx, self.y + dy, self.width, self.height)


class NormRect(BaseModel):
    """Rectangle normalized to a frame: (0, 0) is top-left, (1, 1) bottom-right."""

    model_config = ConfigDict(frozen=True)

    x: float = Field(ge=0.0, le=1.0)
    y: float = Field(ge=0.0, le=1.0)
    width: float = Field(gt=0.0, le=1.0)
    height: float = Field(gt=0.0, le=1.0)

    @model_validator(mode="after")
    def _inside_unit_square(self) -> NormRect:
        if self.x + self.width > 1.0 + _EDGE_EPSILON or self.y + self.height > 1.0 + _EDGE_EPSILON:
            raise ValueError("rectangle extends past the frame")
        return self

    def to_pixels(self, frame_width: int, frame_height: int) -> Rect:
        """Map onto a frame, clamped to it and never smaller than 1x1 pixel."""
        if frame_width <= 0 or frame_height <= 0:
            raise ValueError(f"invalid frame size {frame_width}x{frame_height}")
        left = min(round(self.x * frame_width), frame_width - 1)
        top = min(round(self.y * frame_height), frame_height - 1)
        right = min(max(round((self.x + self.width) * frame_width), left + 1), frame_width)
        bottom = min(max(round((self.y + self.height) * frame_height), top + 1), frame_height)
        return Rect(left, top, right - left, bottom - top)

    @classmethod
    def from_pixels(cls, rect: Rect, frame_width: int, frame_height: int) -> NormRect:
        """Normalize a pixel rect; the part outside the frame is cut off."""
        if frame_width <= 0 or frame_height <= 0:
            raise ValueError(f"invalid frame size {frame_width}x{frame_height}")
        visible = rect.intersection(Rect(0, 0, frame_width, frame_height))
        if visible is None:
            raise ValueError("rectangle does not overlap the frame")
        return cls(
            x=visible.x / frame_width,
            y=visible.y / frame_height,
            width=visible.width / frame_width,
            height=visible.height / frame_height,
        )
