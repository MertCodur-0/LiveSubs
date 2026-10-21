"""Turn a subtitle region into a text mask and a clean OCR input.

Steps, each switchable in ``VisionSettings``:

1. color mask: pixels within ``color_tolerance`` (per channel) of a subtitle color;
   without it, an Otsu threshold of the brightness
2. outline support: keep text pixels that have something dark nearby (outline or caption box)
3. 2x upscale: OCR reads small game fonts far better at twice the size
4. morphology: an opening that removes sub-pixel speckle
5. component filter: drop specks and large blobs of the subtitle color (sky, skin, lights)
6. render: black text on white, the input Windows OCR handles best

The mask is what change detection compares, so background motion never looks like a new line.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import cv2
import numpy as np

from livesubs.core.profiles import VisionSettings

UPSCALE_FACTOR = 2
_OPEN_KERNEL = np.ones((2, 2), np.uint8)


@dataclass(frozen=True, slots=True)
class Preprocessed:
    mask: np.ndarray  # uint8 0/255 at OCR scale; 255 = text
    ocr_image: np.ndarray  # uint8 grayscale at OCR scale, black text on white
    scale: int  # OCR scale relative to the region's pixels

    @property
    def text_fraction(self) -> float:
        return cv2.countNonZero(self.mask) / self.mask.size


def hex_to_bgr(color: str) -> tuple[int, int, int]:
    value = int(color.removeprefix("#"), 16)
    return value & 0xFF, (value >> 8) & 0xFF, (value >> 16) & 0xFF


def color_mask(bgr: np.ndarray, colors: Sequence[str], tolerance: int) -> np.ndarray:
    """255 where a pixel is within ``tolerance`` of any color on every channel."""
    mask = np.zeros(bgr.shape[:2], np.uint8)
    for color in colors:
        target = np.array(hex_to_bgr(color), np.int16)
        lower = np.clip(target - tolerance, 0, 255).astype(np.uint8)
        upper = np.clip(target + tolerance, 0, 255).astype(np.uint8)
        mask |= cv2.inRange(bgr, lower, upper)
    return mask


def outline_filter(mask: np.ndarray, gray: np.ndarray, dark_level: int, radius: int) -> np.ndarray:
    """Keep mask pixels within ``radius`` of a pixel no brighter than ``dark_level``."""
    dark = cv2.compare(gray, dark_level, cv2.CMP_LE)
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * radius + 1, 2 * radius + 1))
    return mask & cv2.dilate(dark, kernel)


def filter_components(mask: np.ndarray, min_area: int, max_area: int) -> np.ndarray:
    """Remove 8-connected components smaller than ``min_area`` or larger than ``max_area``."""
    count, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    areas = stats[:, cv2.CC_STAT_AREA]
    keep = (areas >= min_area) & (areas <= max_area)
    keep[0] = False  # background
    return np.where(keep, 255, 0).astype(np.uint8)[labels] if count > 1 else mask


def preprocess(region: np.ndarray, settings: VisionSettings) -> Preprocessed:
    """``region``: the subtitle area of a frame, BGR or BGRA, any memory layout."""
    if region.ndim == 3 and region.shape[2] == 4:
        bgr = cv2.cvtColor(region, cv2.COLOR_BGRA2BGR)  # also makes it contiguous
    else:
        bgr = np.ascontiguousarray(region)
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)

    if settings.color_mask:
        mask = color_mask(bgr, settings.text_colors, settings.color_tolerance)
    else:
        _, mask = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
    if settings.outline_support:
        mask = outline_filter(mask, gray, settings.outline_dark_level, settings.outline_radius_px)

    scale = UPSCALE_FACTOR if settings.upscale else 1
    if scale > 1:
        size = (bgr.shape[1] * scale, bgr.shape[0] * scale)
        # Linear + threshold keeps glyph edges smooth instead of blocky.
        mask = cv2.resize(mask, size, interpolation=cv2.INTER_LINEAR)
        _, mask = cv2.threshold(mask, 127, 255, cv2.THRESH_BINARY)
    if settings.morphology:
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, _OPEN_KERNEL)
    if settings.component_filter:
        area_scale = scale * scale
        mask = filter_components(
            mask,
            min_area=settings.min_component_px * area_scale,
            max_area=max(1, int(settings.max_component_fraction * mask.size)),
        )

    return Preprocessed(mask=mask, ocr_image=cv2.bitwise_not(mask), scale=scale)
