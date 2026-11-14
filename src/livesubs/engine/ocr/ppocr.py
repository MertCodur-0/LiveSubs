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

# Line finding, in glyph heights (``h``) of the letters mask.
MIN_LETTER, MAX_LETTER, MAX_LETTER_WIDTH = 0.5, 1.8, 2.5  # a glyph-like component
MAX_GAP = 1.2  # wider gaps end a segment (each segment is recognised alone)
MAX_BAND_SHIFT = 0.35  # a component's centre this far from the segment's: another row
ROW_JOIN_GAP = 3.0  # segments of one row closer than this form one OCR line
PAD_X, PAD_Y = 0.6, 0.3


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


def find_segments(letters: np.ndarray) -> list[Segment]:
    """Text segments of the letters mask in reading order: glyph-sized components chained
    left to right along a shared band. Clutter (irregular blobs on bright ground) seldom chains,
    and a label beside a subtitle stays a segment of its own, so neither is glued into the
    subtitle's text."""
    h = glyph_height(letters)
    if h is None:
        return []
    _, _, stats, _ = cv2.connectedComponentsWithStats(letters, connectivity=8)
    comps = sorted(
        (int(x), int(y), int(w), int(ch))
        for x, y, w, ch, _ in stats[1:]
        if MIN_LETTER * h <= ch <= MAX_LETTER * h and w <= MAX_LETTER_WIDTH * h
    )
    chains: list[list[float]] = []  # [left, top, right, bottom, centre y, count]
    for x, y, w, ch in comps:
        centre = y + ch / 2
        for c in chains:
            if -0.5 * h <= x - c[2] <= MAX_GAP * h and abs(centre - c[4]) <= MAX_BAND_SHIFT * h:
                c[1], c[2], c[3] = min(c[1], y), max(c[2], x + w), max(c[3], y + ch)
                c[4] = (c[4] * c[5] + centre) / (c[5] + 1)
                c[5] += 1
                break
        else:
            chains.append([x, y, x + w, y + ch, centre, 1])
    height, width = letters.shape
    boxes = []
    for left, top, right, bottom, _, count in chains:
        if count < 2:
            continue
        x0, y0 = max(0, int(left - PAD_X * h)), max(0, int(top - PAD_Y * h))
        x1, y1 = min(width, int(right + PAD_X * h)), min(height, int(bottom + PAD_Y * h))
        boxes.append(Rect(x0, y0, x1 - x0, y1 - y0))
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

    def recognize(self, region: np.ndarray, letters: np.ndarray) -> OcrResult:
        """Read ``region`` (BGR/BGRA, the subtitle area) along the segments of ``letters`` (the
        preprocessing's letters mask, same size)."""
        started = time.perf_counter()
        if region.ndim == 3 and region.shape[2] == 4:
            region = cv2.cvtColor(region, cv2.COLOR_BGRA2BGR)
        elif region.ndim == 2:
            region = cv2.cvtColor(region, cv2.COLOR_GRAY2BGR)
        read: list[tuple[Segment, str]] = []
        for segment in find_segments(letters):
            b = segment.box
            text = self.read_line(region[b.y : b.bottom, b.x : b.right]).strip()
            if text:
                read.append((segment, text))
        h = glyph_height(letters) or 0.0
        return OcrResult(_join_rows(read, h), (time.perf_counter() - started) * 1000)

    def read_line(self, image: np.ndarray) -> str:
        """One text line (BGR) -> its text (greedy CTC decoding)."""
        height, width = image.shape[:2]
        if height < 2 or width < 2:
            return ""
        resized = min(MAX_WIDTH, math.ceil(INPUT_HEIGHT * width / height))
        batch = np.zeros((1, 3, INPUT_HEIGHT, max(MIN_WIDTH, resized)), np.float32)
        pixels = cv2.resize(image, (resized, INPUT_HEIGHT)).astype(np.float32)
        batch[0, :, :, :resized] = pixels.transpose(2, 0, 1) / 127.5 - 1.0
        best = self._session.run(None, {self._input: batch})[0][0].argmax(axis=1)
        keep = np.ones(len(best), bool)
        keep[1:] = best[1:] != best[:-1]
        keep &= best != 0
        return "".join(self._alphabet[i] for i in best[keep])


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
