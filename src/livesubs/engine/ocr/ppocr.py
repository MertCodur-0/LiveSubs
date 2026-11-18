"""Optional second OCR engine: a PP-OCR text-line recogniser on ONNX Runtime (CPU).

Windows OCR misreads single glyphs on legible subtitles ("Kittyl", "Coma", "walktn'") and drops
words on bright ground. PaddleOCR's English line recogniser (PP-OCRv5 mobile, Apache-2.0, 7.9 MB)
read 94 % of the offline evaluation frames exactly where the Windows reads the pipeline makes
reached 77 % (see NOTES.md of the ``ocr-engine`` branch). It is a *recogniser* only: it reads one
text line per image, so the lines are found here from the preprocessing's letters mask
(``find_segments``), and it is given the region's own colour pixels (our grey and mask images
made it worse).

Missing pieces never break the app: without ``onnxruntime`` or the model file,
``open_line_reader`` returns ``None`` and the pipeline reads with Windows OCR only.
"""

from __future__ import annotations

import importlib.util
import logging
import math
import os
import time
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from livesubs.core.geometry import Rect
from livesubs.engine.ocr.winrt_ocr import OcrLine, OcrResult
from livesubs.engine.vision.preprocess import glyph_height

log = logging.getLogger(__name__)

MODEL_NAME = "en_PP-OCRv5_rec_mobile.onnx"
MODEL_URL = (
    "https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/v3.10.0/onnx/PP-OCRv5/rec/"
    + MODEL_NAME
)
MODEL_SHA256 = "c3461add59bb4323ecba96a492ab75e06dda42467c9e3d0c18db5d1d21924be8"
MODELS_ENV_VAR = "LIVESUBS_OCR_MODELS"  # a folder holding MODEL_NAME (tests, development)
DISABLE_ENV_VAR = "LIVESUBS_NO_PPOCR"  # "1": Windows OCR only (comparisons)
INPUT_HEIGHT = 48  # the model's input height
MIN_WIDTH = 320  # narrower inputs are padded (as PaddleOCR does)
MAX_WIDTH = 3200  # a line wider than 66 glyph heights is squeezed
THREADS = 2  # light on a PC that may also run the game; more threads gained little
# The recogniser's mean character probability. Measured on six game clips: 99.9 % of the subtitle
# reads are above 0.9 (1st percentile 0.95), while half of what it makes of scenery, HUD icons
# and cut-off glyphs is below 0.5 - text it is not sure of is not text.
MIN_CONFIDENCE = 0.8

# Line finding, in glyph heights (``h``) of the letters mask.
MIN_LETTER, MAX_LETTER, MAX_LETTER_WIDTH = 0.5, 1.8, 2.5  # a glyph-like component
MAX_GAP = 1.2  # wider gaps end a segment (each segment is recognised alone)
MAX_BAND_SHIFT = 0.35  # a component's centre this far from the segment's: another row
MAX_SPAN_SHIFT = 0.6  # ... unless it is inside the segment's span, up to this far
ROW_JOIN_GAP = 3.0  # segments of one row closer than this form one OCR line
PAD_X, PAD_Y = 0.6, 0.3
INSIDE_SHARE = 0.5  # a segment box with this share of its area inside a larger one is dropped
# Colored glyphs beside a line (``colored_glyphs``): the preprocessing's defaults for brightness,
# local contrast and its kernel.
COLORED_MIN_BRIGHTNESS, COLORED_MIN_CONTRAST, COLORED_KERNEL = 80, 45, 15
WORD_GAP = 0.3  # narrower gaps between glyphs are inside a word
COLORED_MIN_SPREAD = 40  # max - min channel: pink objective glyphs 60-110, their grey halo ~10


def default_models_dir() -> Path:
    if folder := os.environ.get(MODELS_ENV_VAR):
        return Path(folder)
    return Path(__file__).resolve().parent.parent.parent / "assets" / "ocr"


def open_line_reader(models_dir: Path | None = None) -> LineReader | None:
    """The PP-OCR reader, or ``None`` when it is disabled, ``onnxruntime`` or the model is
    missing, or the model does not load (logged; Windows OCR then reads alone)."""
    if os.environ.get(DISABLE_ENV_VAR) == "1":
        return None
    path = (models_dir or default_models_dir()) / MODEL_NAME
    if importlib.util.find_spec("onnxruntime") is None:
        log.info("onnxruntime is not installed: Windows OCR only")
        return None
    if not path.is_file():
        log.info("PP-OCR model not found at %s: Windows OCR only", path)
        return None
    try:
        reader = LineReader(path)
        log.info("reading with PP-OCR (%s); Windows OCR is the fallback", path.name)
        return reader
    except Exception:  # ImportError, a corrupt file, an unsupported runtime
        log.exception("PP-OCR reader failed to load: Windows OCR only")
        return None


@dataclass(frozen=True, slots=True)
class Segment:
    box: Rect  # padded, in region pixels
    row: int  # index of the text row, top to bottom


def find_segments(letters: np.ndarray, colored: np.ndarray | None = None) -> list[Segment]:
    """Text segments of the letters mask in reading order: glyph-sized components chained
    left to right along a shared band. Clutter (irregular blobs on bright ground) seldom chains,
    and a label beside a subtitle stays a segment of its own, so neither is glued into the
    subtitle's text.

    ``colored`` (same size, optional): glyph candidates of any colour (``colored_glyphs``). Games
    color single words of a white line (GTA's objectives: "Land near the" white, "docks" pink);
    such words never start a line, but chained next to one on its band they become segments of
    their own. Each segment is read alone and must pass the recogniser's confidence, so scenery
    beside a subtitle is dropped without spoiling the subtitle's read."""
    h = glyph_height(letters)
    if h is None:
        return []
    chains = [c for c in _chain(_glyphs(letters, h), h) if c[5] >= 2]
    if colored is not None:
        for word in _colored_neighbours(chains, _glyphs(colored, h), h):
            # Touching a line without a word gap: the rest of a word the letters mask holds in
            # part ("Find Arn" + "old") - one segment, or it would read "Arn old".
            touching = [
                c
                for c in chains
                if abs(word[4] - c[4]) <= MAX_BAND_SHIFT * h
                and (abs(word[0] - c[2]) <= WORD_GAP * h or abs(c[0] - word[2]) <= WORD_GAP * h)
            ]
            if touching:
                line = touching[0]
                line[0], line[2] = min(line[0], word[0]), max(line[2], word[2])
            else:
                chains.append(word)
    height, width = letters.shape
    padded = []
    for c in chains:
        left, right = c[0] - PAD_X * h, c[2] + PAD_X * h
        for other in chains:  # never into a neighbour on the band: the glyphs would read twice
            if other is not c and abs(other[4] - c[4]) <= MAX_BAND_SHIFT * h:
                if other[2] <= c[0]:
                    left = max(left, (other[2] + c[0]) / 2)
                elif other[0] >= c[2]:
                    right = min(right, (c[2] + other[0]) / 2)
        x0, y0 = max(0, int(left)), max(0, int(c[1] - PAD_Y * h))
        x1, y1 = min(width, int(right)), min(height, int(c[3] + PAD_Y * h))
        padded.append(Rect(x0, y0, x1 - x0, y1 - y0))
    # A box lying mostly inside a larger one is a piece of that line (glyph parts over bright
    # ground chained off its band): read alone it would come back as a fragment ("Tha", "t duc").
    padded.sort(key=lambda b: b.width * b.height, reverse=True)
    boxes: list[Rect] = []
    for box in padded:
        if all(_overlap(box, big) < INSIDE_SHARE * box.width * box.height for big in boxes):
            boxes.append(box)
    boxes.sort(key=lambda b: b.y + b.height / 2)
    segments: list[Segment] = []
    row, row_centre = -1, -math.inf
    for box in boxes:
        centre = box.y + box.height / 2
        if centre - row_centre >= 0.5 * h:
            row, row_centre = row + 1, centre
        segments.append(Segment(box, row))
    segments.sort(key=lambda s: (s.row, s.box.x))
    return segments


def _glyphs(mask: np.ndarray, h: float) -> list[tuple[int, int, int, int]]:
    """Glyph-sized components of ``mask`` (x, y, w, h), left to right."""
    _, _, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    return sorted(
        (int(x), int(y), int(w), int(ch))
        for x, y, w, ch, _ in stats[1:]
        if MIN_LETTER * h <= ch <= MAX_LETTER * h and w <= MAX_LETTER_WIDTH * h
    )


def _chain(glyphs: list[tuple[int, int, int, int]], h: float) -> list[list[float]]:
    """Glyphs (left to right) chained along bands: [left, top, right, bottom, centre y, count]."""
    chains: list[list[float]] = []
    for x, y, w, ch in glyphs:
        centre = y + ch / 2
        for c in chains:
            if not -0.5 * h <= x - c[2] <= MAX_GAP * h:
                continue
            if abs(centre - c[4]) <= MAX_BAND_SHIFT * h:
                c[1], c[2], c[3] = min(c[1], y), max(c[2], x + w), max(c[3], y + ch)
                c[4] = (c[4] * c[5] + centre) / (c[5] + 1)
                c[5] += 1
                break
            if abs(centre - c[4]) <= MAX_SPAN_SHIFT * h and c[1] <= centre <= c[3]:
                # Off the band's mean but inside its span: a "!" or a capital after x-height
                # letters ("mean!" once became two segments whose padded boxes both read the
                # "!"). It lengthens the chain but never widens its span or moves its band,
                # so scenery next to a line cannot stretch the box row by row.
                c[2] = max(c[2], x + w)
                c[5] += 1
                break
        else:
            chains.append([x, y, x + w, y + ch, centre, 1])
    return chains


def _overlap(a: Rect, b: Rect) -> int:
    """The area two boxes share."""
    width = min(a.right, b.right) - max(a.x, b.x)
    height = min(a.bottom, b.bottom) - max(a.y, b.y)
    return max(0, width) * max(0, height)


def _colored_neighbours(
    lines: list[list[float]], glyphs: list[tuple[int, int, int, int]], h: float
) -> list[list[float]]:
    """Chains of colored glyphs on a line's band, beside it (not over it) and within a word gap
    of it, or of such a chain."""

    def on_a_line_band(x: int, y: int, w: int, ch: int) -> bool:
        centre = y + ch / 2
        same = [c for c in lines if abs(centre - c[4]) <= MAX_BAND_SHIFT * h]
        # beside the line's glyphs, not over them (those are read already)
        return bool(same) and all(x + w <= c[0] or x >= c[2] for c in same)

    near = [g for g in glyphs if on_a_line_band(*g)]
    found: list[list[float]] = []
    reach = list(lines)
    for c in _chain(near, h):
        if c[5] < 2:
            continue
        found.append(c)
    added = True
    while added:  # a colored word reaches a line directly or through another colored word
        added = False
        for c in list(found):
            if any(
                abs(c[4] - r[4]) <= MAX_BAND_SHIFT * h
                and (0 <= c[0] - r[2] <= MAX_GAP * h or 0 <= r[0] - c[2] <= MAX_GAP * h)
                for r in reach
            ):
                reach.append(c)
                found.remove(c)
                added = True
    return reach[len(lines) :]


def colored_glyphs(region: np.ndarray) -> np.ndarray:
    """Pixels of colored text (BGR ``region``): colourful (the channels at least
    ``COLORED_MIN_SPREAD`` apart; white and grey glyphs are in the letters mask already, and a
    grey halo around colored text would join its glyphs into one blob), the brightest channel at
    least ``COLORED_MIN_BRIGHTNESS`` and ``COLORED_MIN_CONTRAST`` above its surroundings (a white
    top-hat, as the preprocessing does per subtitle colour)."""
    if region.ndim != 3:
        return np.zeros(region.shape[:2], np.uint8)
    b, g, r = cv2.split(region)
    brightest = cv2.max(cv2.max(b, g), r)
    spread = cv2.subtract(brightest, cv2.min(cv2.min(b, g), r))
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (COLORED_KERNEL, COLORED_KERNEL))
    contrast = cv2.morphologyEx(brightest, cv2.MORPH_TOPHAT, kernel)
    return (
        cv2.compare(brightest, COLORED_MIN_BRIGHTNESS, cv2.CMP_GE)
        & cv2.compare(spread, COLORED_MIN_SPREAD, cv2.CMP_GE)
        & cv2.compare(contrast, COLORED_MIN_CONTRAST, cv2.CMP_GE)
    )


class LineReader:
    """``recognize(region, letters) -> OcrResult``: lines with boxes in region pixels."""

    def __init__(self, model: Path, threads: int = THREADS) -> None:
        import onnxruntime as ort  # optional dependency: imported only when used

        options = ort.SessionOptions()
        options.intra_op_num_threads = threads
        options.inter_op_num_threads = 1
        # No busy-waiting worker threads between reads: the game may share the CPU.
        options.add_session_config_entry("session.intra_op.allow_spinning", "0")
        self._session = ort.InferenceSession(
            str(model), options, providers=["CPUExecutionProvider"]
        )
        characters = self._session.get_modelmeta().custom_metadata_map["character"]
        self._alphabet = ["", *characters.splitlines(), " "]  # CTC blank first, space last
        self._input = self._session.get_inputs()[0].name

    def recognize(self, region: np.ndarray, letters: np.ndarray) -> OcrResult | None:
        """Read ``region`` (BGR/BGRA, the subtitle area) along the segments of ``letters`` (the
        preprocessing's letters mask, same size). ``None`` when the mask has no segment to read
        (too few glyphs to measure, e.g. "No."): the caller's other engine should look. A result
        without lines means there were segments and none was text."""
        started = time.perf_counter()
        if glyph_height(letters) is None:
            return None
        if region.ndim == 3 and region.shape[2] == 4:
            region = cv2.cvtColor(region, cv2.COLOR_BGRA2BGR)
        elif region.ndim == 2:
            region = cv2.cvtColor(region, cv2.COLOR_GRAY2BGR)
        segments = find_segments(letters, colored_glyphs(region))
        if not segments:
            return None
        read: list[tuple[Segment, str]] = []
        for segment in segments:
            b = segment.box
            text, confidence = self.read_line(region[b.y : b.bottom, b.x : b.right])
            if text and confidence >= MIN_CONFIDENCE:
                read.append((segment, text))
        h = glyph_height(letters) or 0.0
        return OcrResult(_join_rows(read, h), (time.perf_counter() - started) * 1000)

    def read_line(self, image: np.ndarray) -> tuple[str, float]:
        """One text line (BGR) -> ``(its text, the mean probability of its characters)``
        (greedy CTC decoding)."""
        height, width = image.shape[:2]
        if height < 2 or width < 2:
            return "", 0.0
        resized = min(MAX_WIDTH, math.ceil(INPUT_HEIGHT * width / height))
        batch = np.zeros((1, 3, INPUT_HEIGHT, max(MIN_WIDTH, resized)), np.float32)
        pixels = cv2.resize(image, (resized, INPUT_HEIGHT)).astype(np.float32)
        batch[0, :, :, :resized] = pixels.transpose(2, 0, 1) / 127.5 - 1.0
        probabilities = self._session.run(None, {self._input: batch})[0][0]
        best = probabilities.argmax(axis=1)
        keep = np.ones(len(best), bool)
        keep[1:] = best[1:] != best[:-1]
        keep &= best != 0
        text = "".join(self._alphabet[i] for i in best[keep]).strip()
        if not text:
            return "", 0.0
        return text, float(probabilities[keep, best[keep]].mean())


def _join_rows(read: list[tuple[Segment, str]], h: float) -> tuple[OcrLine, ...]:
    """Segments of one row close together form one OCR line (a subtitle with a wide word gap);
    a segment far to the side (a HUD label) stays a line of its own."""
    lines: list[tuple[int, Rect, str]] = []
    for segment, text in read:
        if lines:
            row, box, joined = lines[-1]
            if row == segment.row and segment.box.x - box.right <= ROW_JOIN_GAP * h:
                lines[-1] = (row, _union(box, segment.box), f"{joined} {text}")
                continue
        lines.append((segment.row, segment.box, text))
    return tuple(OcrLine(text, box) for _, box, text in lines)


def _union(a: Rect, b: Rect) -> Rect:
    left, top = min(a.x, b.x), min(a.y, b.y)
    right, bottom = max(a.right, b.right), max(a.bottom, b.bottom)
    return Rect(left, top, right - left, bottom - top)
