"""Turn a subtitle region into a text mask and a clean OCR input.

Steps, each switchable in ``VisionSettings``:

1. color mask: pixels within ``color_tolerance`` (per channel) of a subtitle color;
   without it, an Otsu threshold of the brightness
2. local contrast: keep only thin structures that stand out from their surroundings in
   closeness to the subtitle color (a white top-hat of the closeness map). Wide areas of the
   subtitle color (sky, a white road, skin) vanish; text on a faint caption box or no box at all
   stays, because it never needs a *dark* edge — only to differ from what is around it.
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


def closeness(bgr: np.ndarray, color: str) -> np.ndarray:
    """255 minus the largest per-channel difference to ``color``: 255 = exactly that color."""
    b, g, r = cv2.split(cv2.absdiff(bgr, (*hex_to_bgr(color), 0)))
    return cv2.bitwise_not(cv2.max(cv2.max(b, g), r))


def local_contrast(image: np.ndarray, min_contrast: int, kernel_px: int) -> np.ndarray:
    """255 where ``image`` exceeds its neighborhood by ``min_contrast``, in structures thinner
    than ``kernel_px`` (white top-hat). Glyph strokes qualify; wide bright areas never do."""
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (kernel_px, kernel_px))
    return cv2.compare(cv2.morphologyEx(image, cv2.MORPH_TOPHAT, kernel), min_contrast, cv2.CMP_GE)


def color_mask(
    bgr: np.ndarray,
    colors: Sequence[str],
    tolerance: int,
    min_contrast: int | None = None,
    kernel_px: int = 15,
) -> np.ndarray:
    """255 where a pixel is within ``tolerance`` of any color on every channel and, with
    ``min_contrast``, also stands out locally in closeness to that color."""
    mask = np.zeros(bgr.shape[:2], np.uint8)
    for color in colors:
        near = closeness(bgr, color)
        match = cv2.compare(near, 255 - tolerance, cv2.CMP_GE)
        if min_contrast is not None:
            match &= local_contrast(near, min_contrast, kernel_px)
        mask |= match
    return mask


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
    contrast = settings.min_local_contrast if settings.local_contrast else None
    kernel_px = settings.local_contrast_kernel_px

    if settings.color_mask:
        mask = color_mask(bgr, settings.text_colors, settings.color_tolerance, contrast, kernel_px)
    else:
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        _, mask = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
        if contrast is not None:
            mask &= local_contrast(gray, contrast, kernel_px)

    if settings.component_filter:
        # At native resolution: a quarter of the pixels to label compared with after upscaling.
        mask = filter_components(
            mask,
            min_area=settings.min_component_px,
            max_area=max(1, int(settings.max_component_fraction * mask.size)),
        )

    scale = UPSCALE_FACTOR if settings.upscale else 1
    if scale > 1:
        size = (bgr.shape[1] * scale, bgr.shape[0] * scale)
        # Linear + threshold keeps glyph edges smooth instead of blocky.
        mask = cv2.resize(mask, size, interpolation=cv2.INTER_LINEAR)
        _, mask = cv2.threshold(mask, 127, 255, cv2.THRESH_BINARY)
    if settings.morphology:
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, _OPEN_KERNEL)

    return Preprocessed(mask=mask, ocr_image=cv2.bitwise_not(mask), scale=scale)
