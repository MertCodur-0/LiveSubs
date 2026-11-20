"""What a read row looks like: the brightness of its glyphs and the colour of its speaker name.

GTA VI with speaker names draws "Lucia:" in her colour and the sentence in white, and people in
the background in grey without a name. ``annotate`` measures, for each OCR row, the brightness of
its sentence's glyphs (``Row.tone``: a grey row under a white one is something else) and the
colour of the name in front of it (``Row.speaker_color``: the translation is drawn with it).
"""

from __future__ import annotations

from dataclasses import replace

import cv2
import numpy as np

from livesubs.engine.ocr.ppocr import colored_glyphs
from livesubs.engine.text import speakers
from livesubs.engine.text.stabilizer import Row

MIN_PIXELS = 12  # fewer glyph pixels than this measure nothing
TONE_PERCENTILE = 90  # glyph cores, not their anti-aliased edges
# Characters are not equally wide: the name is taken as a little less than its share of the
# row's characters, the sentence as starting a little after it.
NAME_SHARE = 0.8
SENTENCE_SHARE = 1.25


def annotate(rows: list[Row], region: np.ndarray, letters: np.ndarray) -> list[Row]:
    """``rows`` with ``tone`` and ``speaker_color`` set where they can be measured. ``region``
    is the BGR(A) subtitle area, ``letters`` the preprocessing's letters mask of the same size;
    the rows' boxes are 0-1 of it."""
    if region.ndim != 3 or letters.shape[:2] != region.shape[:2]:
        return rows
    if region.shape[2] == 4:
        region = cv2.cvtColor(region, cv2.COLOR_BGRA2BGR)
    return [_annotate(row, region, letters) for row in rows]


def _annotate(row: Row, region: np.ndarray, letters: np.ndarray) -> Row:
    if row.box is None or not row.text:
        return row
    h, w = letters.shape
    x0, y0 = round(row.box[0] * w), round(row.box[1] * h)
    x1, y1 = round(row.box[2] * w), round(row.box[3] * h)
    if x1 - x0 < 2 or y1 - y0 < 2:
        return row
    name, _ = speakers.split(row.text)
    share = (len(name) + 1) / len(row.text) if name else 0.0
    split = x0 + round((x1 - x0) * share * SENTENCE_SHARE)
    color = None
    if name:
        end = x0 + max(1, round((x1 - x0) * share * NAME_SHARE))
        color = name_color(region[y0:y1, x0:end])
    return replace(row, tone=tone(region[y0:y1, split:x1], letters[y0:y1, split:x1]),
                   speaker_color=color)  # fmt: skip


def tone(region: np.ndarray, letters: np.ndarray) -> float | None:
    """Brightness (grey level) of the glyph cores in ``region`` (BGR), where ``letters`` is set."""
    if region.size == 0:
        return None
    gray = cv2.cvtColor(region, cv2.COLOR_BGR2GRAY)
    values = gray[letters > 0]
    if len(values) < MIN_PIXELS:
        return None
    return float(np.percentile(values, TONE_PERCENTILE))


def name_color(region: np.ndarray) -> str | None:
    """``"#rrggbb"`` of the coloured glyphs in ``region`` (BGR), or None when they are not
    coloured (a white name) or too few."""
    if region.size == 0 or region.shape[0] < 3 or region.shape[1] < 3:
        return None
    pixels = region[colored_glyphs(region) > 0].astype(np.float32)
    if len(pixels) < MIN_PIXELS:
        return None
    # The glyph cores: the brighter half (edges blend into the background).
    brightest = pixels.max(axis=1)
    core = pixels[brightest >= np.median(brightest)]
    b, g, r = (round(float(v)) for v in np.median(core, axis=0))
    return f"#{r:02x}{g:02x}{b:02x}"
