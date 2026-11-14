"""Turn a subtitle region into a text mask and OCR inputs.

Games draw "white" subtitles light grey, and video, capture cards and dark scenes dim them further
(measured on real footage: glyph cores at 145-190). So nothing here looks for an absolute color.

1. tone: per subtitle color, how bright each pixel is *along that color* and how far it is from
   that color scaled to its own brightness. Dim grey still matches white, dim gold still matches
   gold; a bright colored or neutral surface does not match yellow. Without the color mask, any
   color counts by its grey level.
2. text = right tone AND at least ``min_brightness`` AND locally brighter than its surroundings
   (a white top-hat of the brightness >= ``min_contrast``: thin strokes stand out, wide bright
   scenery never does, no dark box or outline is needed) AND not ``brightness_drop`` darker than
   the brightest such pixels of the region (texture next to a bright subtitle).
3. component filter: drop specks and large blobs (sky, skin, lights)
4. scale: the glyph height is measured from the mask and the OCR inputs are scaled so that glyphs
   are about ``text_height_px`` tall (Windows OCR misreads small fonts; a fixed 2x left 720p
   footage too small)
5. OCR input: the region's own brightness, upscaled smoothly, kept only in the text rows (the rest
   flattened to its median). Real grey levels keep the faint parts of glyphs and punctuation that
   a black-and-white mask loses.
6. second OCR input (``second_read``): the mask itself, black on white. On busy bright ground it
   reads better; the pipeline keeps whichever read looks more like English.

``Preprocessed.mask`` holds the letters only (components at least half the glyph height), at the
region's own resolution: it is what change detection compares, so neither background specks nor a
different OCR scale ever look like a new line.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import cv2
import numpy as np

from livesubs.core.profiles import VisionSettings

DEFAULT_SCALE = 2.0  # when no glyph height can be measured (no text)
MAX_SCALE = 4.0
LETTER_SHARE = 0.5  # of the glyph height: shorter components are dots, commas or specks
_SCALE_STEP = 0.25  # scales are rounded to this, so the OCR input does not jitter frame to frame
_OPEN_KERNEL = np.ones((2, 2), np.uint8)


@dataclass(frozen=True, slots=True)
class Preprocessed:
    mask: np.ndarray  # uint8 0/255 at the region's size; 255 = letters (no dots or specks)
    ocr_image: np.ndarray  # uint8 at OCR scale: the region's brightness, text rows only
    sharp_image: np.ndarray | None  # uint8 at OCR scale: all text pixels, black on white
    scale: float  # OCR scale relative to the region's pixels

    @property
    def text_fraction(self) -> float:
        return cv2.countNonZero(self.mask) / self.mask.size

    @property
    def ocr_inputs(self) -> list[np.ndarray]:
        return [self.ocr_image] if self.sharp_image is None else [self.ocr_image, self.sharp_image]


def hex_to_bgr(color: str) -> tuple[int, int, int]:
    value = int(color.removeprefix("#"), 16)
    return value & 0xFF, (value >> 8) & 0xFF, (value >> 16) & 0xFF


def tone(bgr: np.ndarray, color: str) -> tuple[np.ndarray, np.ndarray]:
    """``(brightness, error)`` of every pixel against ``color`` at any brightness.

    brightness: 0-255 along the color's main channels (those >= 128); error: the largest channel
    distance from the color scaled to that brightness."""
    bgr_target = hex_to_bgr(color)
    if all(c in (0, 255) for c in bgr_target) and any(bgr_target):
        return _pure_tone(bgr, bgr_target)
    target = np.array(bgr_target, np.float32)
    pixels = bgr.astype(np.float32)
    main = target >= 128
    if not main.any():  # a dark subtitle color: judge it by its strongest channel
        main = target == target.max()
    scale = 255.0 / np.maximum(target[main], 1.0)
    brightness = np.minimum((pixels[..., main] * scale).min(axis=2), 255.0)
    error = np.abs(pixels - brightness[..., None] * (target / 255.0)).max(axis=2)
    return brightness.astype(np.uint8), error


def _pure_tone(bgr: np.ndarray, target: tuple[int, int, int]) -> tuple[np.ndarray, np.ndarray]:
    """``tone`` for colors made of full and empty channels (white, yellow, cyan...), in uint8:
    brightness = the weakest full channel; error = the spread of the full channels or the
    strongest empty channel, whichever is larger."""
    channels = cv2.split(bgr)
    full = [ch for ch, c in zip(channels, target, strict=True) if c == 255]
    empty = [ch for ch, c in zip(channels, target, strict=True) if c == 0]
    low, high = full[0], full[0]
    for ch in full[1:]:
        low, high = cv2.min(low, ch), cv2.max(high, ch)
    error = cv2.subtract(high, low)
    for ch in empty:
        error = cv2.max(error, ch)
    return low, error


def top_hat(image: np.ndarray, kernel_px: int) -> np.ndarray:
    """How much each pixel exceeds its neighborhood, in structures thinner than ``kernel_px``."""
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (kernel_px, kernel_px))
    return cv2.morphologyEx(image, cv2.MORPH_TOPHAT, kernel)


def text_maps(bgr: np.ndarray, settings: VisionSettings) -> tuple[np.ndarray, np.ndarray]:
    """``(mask, brightness)``: text pixels (before the component filter) and the brightness the
    OCR input is made from."""
    if settings.color_mask:
        b, g, r = cv2.split(bgr)
        lowest = cv2.min(cv2.min(b, g), r)
        tones = [tone(bgr, color) for color in settings.text_colors]
        brightness = lowest
    else:
        tones = [(cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY), None)]
        brightness = tones[0][0]
    mask = np.zeros(bgr.shape[:2], np.uint8)
    for value, error in tones:
        match = cv2.compare(value, settings.min_brightness, cv2.CMP_GE)
        if error is not None:
            in_tone = cv2.compare(error, float(settings.tone_tolerance), cv2.CMP_LE)
            match &= in_tone
            # Where the pixel has this color's tone, its brightness along the color counts.
            brightness = cv2.max(brightness, value & in_tone)
        if settings.local_contrast:
            contrast = top_hat(value, settings.local_contrast_kernel_px)
            match &= cv2.compare(contrast, settings.min_contrast, cv2.CMP_GE)
        mask |= match
    if settings.brightness_drop < 255 and cv2.countNonZero(mask) > 50:
        floor = _percentile(brightness, mask, 98) - settings.brightness_drop
        mask &= cv2.compare(brightness, floor, cv2.CMP_GE)
    return mask, brightness


def _percentile(image: np.ndarray, mask: np.ndarray | None, q: float) -> int:
    """The ``q``-th percentile of a uint8 image (where ``mask`` is set), from its histogram."""
    hist = cv2.calcHist([image], [0], mask, [256], [0, 256]).ravel().cumsum()
    return int(np.searchsorted(hist, hist[-1] * q / 100.0))


def filter_components(mask: np.ndarray, min_area: int, max_area: int) -> np.ndarray:
    """Remove 8-connected components smaller than ``min_area`` or larger than ``max_area``."""
    count, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    areas = stats[:, cv2.CC_STAT_AREA]
    keep = (areas >= min_area) & (areas <= max_area)
    keep[0] = False  # background
    return np.where(keep, 255, 0).astype(np.uint8)[labels] if count > 1 else mask


def glyph_height(mask: np.ndarray) -> float | None:
    """Typical glyph height in pixels: the 75th percentile of plausible component heights (tall
    letters rather than dots and commas); ``None`` with fewer than three glyphs."""
    return _letters(mask)[0]


def _letters(mask: np.ndarray) -> tuple[float | None, np.ndarray]:
    """``(glyph height, letters)``: ``letters`` keeps the components at least
    ``LETTER_SHARE`` of the glyph height tall, i.e. no specks, dots or commas."""
    count, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    heights = stats[1:, cv2.CC_STAT_HEIGHT]
    widths = stats[1:, cv2.CC_STAT_WIDTH]
    plausible = heights[(heights >= 4) & (heights <= mask.shape[0] // 2) & (widths <= 3 * heights)]
    if len(plausible) < 3:
        return None, mask
    height = float(np.percentile(plausible, 75))
    keep = np.zeros(count, bool)
    keep[1:] = heights >= LETTER_SHARE * height
    return height, np.where(keep, 255, 0).astype(np.uint8)[labels]


def ocr_scale(height: float | None, target: int) -> float:
    """The upscale that makes glyphs of ``height`` about ``target`` pixels tall (1x-4x)."""
    if height is None:
        return DEFAULT_SCALE
    scale = min(MAX_SCALE, max(1.0, target / height))
    return round(scale / _SCALE_STEP) * _SCALE_STEP


def preprocess(region: np.ndarray, settings: VisionSettings) -> Preprocessed:
    """``region``: the subtitle area of a frame, BGR or BGRA, any memory layout."""
    if region.ndim == 3 and region.shape[2] == 4:
        bgr = cv2.cvtColor(region, cv2.COLOR_BGRA2BGR)  # also makes it contiguous
    else:
        bgr = np.ascontiguousarray(region)

    mask, brightness = text_maps(bgr, settings)
    if settings.component_filter:
        # At native resolution: far fewer pixels to label than after upscaling.
        mask = filter_components(
            mask,
            min_area=settings.min_component_px,
            max_area=max(1, int(settings.max_component_fraction * mask.size)),
        )
    height, letters = _letters(mask)

    # Keep the brightness in the text rows only: watermarks, HUD and scenery away from the
    # subtitle can then never be read into it.
    if height is not None:
        reach = cv2.getStructuringElement(
            cv2.MORPH_RECT, (int(height * 3) | 1, int(height * 1.2) | 1)
        )
        rows = cv2.dilate(letters, reach)
        brightness = brightness.copy()
        brightness[rows == 0] = _percentile(brightness, None, 50)

    scale = ocr_scale(height, settings.text_height_px) if settings.upscale else 1.0
    size = (round(bgr.shape[1] * scale), round(bgr.shape[0] * scale))
    if scale != 1.0:
        ocr_image = cv2.resize(brightness, size, interpolation=cv2.INTER_CUBIC)
    else:
        ocr_image = np.ascontiguousarray(brightness)
    # The change detector compares letters only, at the region's own resolution: specks of a busy
    # background never look like a new line, and a different OCR scale never changes the mask.
    # The sharp OCR input keeps everything, punctuation included.
    sharp = None
    if settings.second_read:
        sharp = cv2.bitwise_not(_upscaled_mask(mask, size, settings.morphology))
    return Preprocessed(mask=letters, ocr_image=ocr_image, sharp_image=sharp, scale=scale)


def _upscaled_mask(mask: np.ndarray, size: tuple[int, int], opening: bool) -> np.ndarray:
    if (mask.shape[1], mask.shape[0]) != size:
        # Linear + threshold keeps glyph edges smooth instead of blocky.
        mask = cv2.resize(mask, size, interpolation=cv2.INTER_LINEAR)
        _, mask = cv2.threshold(mask, 127, 255, cv2.THRESH_BINARY)
    return cv2.morphologyEx(mask, cv2.MORPH_OPEN, _OPEN_KERNEL) if opening else mask


def color_mask(bgr: np.ndarray, colors: Sequence[str], tolerance: int) -> np.ndarray:
    """255 where a pixel has the tone of any of ``colors``, at any brightness."""
    mask = np.zeros(bgr.shape[:2], np.uint8)
    for color in colors:
        mask |= (tone(bgr, color)[1] <= tolerance).astype(np.uint8) * 255
    return mask
