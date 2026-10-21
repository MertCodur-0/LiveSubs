"""Windows OCR via WinRT ``OcrEngine``.

Measured on the samples: 5-13 ms per preprocessed subtitle region (2x upscaled), after a
one-off warm-up of ~20 ms. ``recognize_async(...).get()`` blocks the calling thread, which is
exactly what the engine's OCR thread wants; no asyncio loop is involved.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import cv2
import numpy as np
from winrt.windows.globalization import Language
from winrt.windows.graphics.imaging import BitmapPixelFormat, SoftwareBitmap
from winrt.windows.media.ocr import OcrEngine
from winrt.windows.storage.streams import Buffer

from livesubs.core.geometry import Rect
from livesubs.engine.ocr.language import ENGLISH_US


class OcrUnavailableError(RuntimeError):
    """No OCR engine for the requested language (its Windows language pack is missing)."""


@dataclass(frozen=True, slots=True)
class OcrLine:
    text: str
    box: Rect  # in the pixels of the image that was recognized


@dataclass(frozen=True, slots=True)
class OcrResult:
    lines: tuple[OcrLine, ...]
    elapsed_ms: float

    @property
    def text(self) -> str:
        """All lines joined with spaces: a two-line subtitle is one sentence."""
        return " ".join(line.text for line in self.lines)


def to_software_bitmap(gray: np.ndarray) -> SoftwareBitmap:
    """Copy an 8-bit grayscale image into a Gray8 ``SoftwareBitmap``."""
    height, width = gray.shape
    data = np.ascontiguousarray(gray, dtype=np.uint8)
    buffer = Buffer(data.nbytes)
    buffer.length = data.nbytes
    np.frombuffer(memoryview(buffer), np.uint8)[:] = data.ravel()
    return SoftwareBitmap.create_copy_from_buffer(buffer, BitmapPixelFormat.GRAY8, width, height)


def _line_box(line: object) -> Rect:
    rects = [word.bounding_rect for word in line.words]
    left = min(r.x for r in rects)
    top = min(r.y for r in rects)
    right = max(r.x + r.width for r in rects)
    bottom = max(r.y + r.height for r in rects)
    return Rect(round(left), round(top), round(right - left), round(bottom - top))


class WinRtOcr:
    def __init__(self, language: str = ENGLISH_US) -> None:
        engine = OcrEngine.try_create_from_language(Language(language))
        if engine is None:
            raise OcrUnavailableError(f"no Windows OCR engine for {language!r}")
        self._engine = engine
        self.language = engine.recognizer_language.language_tag
        self.max_dimension = OcrEngine.max_image_dimension

    def recognize(self, image: np.ndarray) -> OcrResult:
        """Read ``image`` (grayscale, BGR or BGRA). Dark text on a light background reads best."""
        started = time.perf_counter()
        gray = image if image.ndim == 2 else cv2.cvtColor(image, _TO_GRAY[image.shape[2]])
        scale = self.max_dimension / max(gray.shape)
        if scale < 1:  # OCR rejects larger images; boxes are reported in the original pixels
            gray = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        result = self._engine.recognize_async(to_software_bitmap(gray)).get()
        lines = tuple(
            OcrLine(line.text, _unscale(_line_box(line), min(scale, 1.0)))
            for line in result.lines
            if line.text.strip()
        )
        return OcrResult(lines, (time.perf_counter() - started) * 1000)


_TO_GRAY = {3: cv2.COLOR_BGR2GRAY, 4: cv2.COLOR_BGRA2GRAY}


def _unscale(box: Rect, scale: float) -> Rect:
    if scale == 1.0:
        return box
    return Rect(
        round(box.x / scale),
        round(box.y / scale),
        round(box.width / scale),
        round(box.height / scale),
    )
