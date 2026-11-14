"""The optional PP-OCR line reader: line finding, fallback, and (with the model) real reads."""

from __future__ import annotations

import cv2
import numpy as np
import pytest

from livesubs.core.geometry import Rect
from livesubs.core.profiles import VisionSettings
from livesubs.engine.ocr import ppocr
from livesubs.engine.ocr.ppocr import Segment, find_segments, open_line_reader
from livesubs.engine.ocr.winrt_ocr import OcrLine, OcrResult
from livesubs.engine.pipeline import read_subtitle
from livesubs.engine.text.quality import ReadScorer
from livesubs.engine.vision.preprocess import preprocess


def _glyphs(mask: np.ndarray, x: int, y: int, count: int, step: int = 14) -> int:
    """Draw ``count`` letter-like bars (10 x 20 px) from ``x``; returns the end x."""
    for i in range(count):
        cv2.rectangle(mask, (x + i * step, y), (x + i * step + 9, y + 19), 255, -1)
    return x + count * step


def test_segments_follow_rows_and_split_far_labels() -> None:
    mask = np.zeros((200, 900), np.uint8)
    end = _glyphs(mask, 100, 40, 6)
    _glyphs(mask, end + 10, 40, 5)  # a word gap: same segment
    _glyphs(mask, 750, 42, 4)  # a label far to the right of the first row
    _glyphs(mask, 150, 120, 8)  # a second row
    segments = find_segments(mask)
    assert [s.row for s in segments] == [0, 0, 1]
    first, label, second = (s.box for s in segments)
    assert first.x < 100 and first.right > end + 50
    assert label.x > 700
    assert second.y > first.bottom


def test_specks_and_lone_components_are_no_segment() -> None:
    mask = np.zeros((100, 400), np.uint8)
    _glyphs(mask, 50, 30, 3)  # glyph height reference
    cv2.rectangle(mask, (300, 30), (309, 49), 255, -1)  # a lone glyph-sized blob
    cv2.rectangle(mask, (200, 80), (380, 99), 255, -1)  # a wide bar (no letter)
    segments = find_segments(mask)
    assert len(segments) == 1 and segments[0].box.x < 50


def test_empty_mask_has_no_segments() -> None:
    assert find_segments(np.zeros((50, 200), np.uint8)) == []


def test_close_segments_of_a_row_form_one_line() -> None:
    a, b, far = Rect(0, 0, 100, 30), Rect(130, 0, 100, 30), Rect(600, 0, 50, 30)
    lines = ppocr._join_rows(
        [(Segment(a, 0), "Hello"), (Segment(b, 0), "there"), (Segment(far, 0), "HUD")], h=20
    )
    assert [line.text for line in lines] == ["Hello there", "HUD"]
    assert lines[0].box == Rect(0, 0, 230, 30)


def test_missing_model_or_switch_means_windows_only(tmp_path, monkeypatch) -> None:
    assert open_line_reader(tmp_path) is None
    monkeypatch.setenv(ppocr.DISABLE_ENV_VAR, "1")
    assert open_line_reader() is None


class _FakeWindows:
    def __init__(self) -> None:
        self.calls = 0

    def recognize(self, image: np.ndarray) -> OcrResult:
        self.calls += 1
        return OcrResult((OcrLine("from windows", Rect(0, 0, 50, 10)),), 1.0)


class _FakeLines:
    def __init__(self, text: str) -> None:
        self.text = text

    def recognize(self, region: np.ndarray, letters: np.ndarray) -> OcrResult:
        lines = (OcrLine(self.text, Rect(10, 5, 80, 20)),) if self.text else ()
        return OcrResult(lines, 2.0)


def _processed():
    region = np.zeros((60, 200, 3), np.uint8)
    cv2.putText(region, "Hello there", (10, 40), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255,) * 3, 2)
    return region, preprocess(region, VisionSettings())


def test_read_subtitle_uses_the_line_reader_alone() -> None:
    region, processed = _processed()
    windows = _FakeWindows()
    text, ms, rows = read_subtitle(
        windows, processed, ReadScorer(None), _FakeLines("from ppocr"), region
    )
    assert (text, ms, windows.calls) == ("from ppocr", 2.0, 0)
    assert [row.text for row in rows] == ["from ppocr"]
    assert rows[0].center == pytest.approx(50 / 200)


def test_read_subtitle_falls_back_to_windows_when_nothing_is_read() -> None:
    region, processed = _processed()
    windows = _FakeWindows()
    text, _, _ = read_subtitle(windows, processed, ReadScorer(None), _FakeLines(""), region)
    assert text == "from windows" and windows.calls == len(processed.ocr_inputs)


@pytest.fixture(scope="module")
def reader():
    pytest.importorskip("onnxruntime")
    found = open_line_reader()
    if found is None:
        pytest.skip("PP-OCR model missing (scripts/get_ocr_model.py)")
    return found


def test_reads_rendered_subtitle(reader) -> None:
    region = np.full((120, 900, 3), (40, 60, 50), np.uint8)
    cv2.putText(region, "Come on, you fool!", (60, 75), cv2.FONT_HERSHEY_DUPLEX, 1.4, (235,) * 3, 2)
    processed = preprocess(region, VisionSettings())
    read = reader.recognize(region, processed.mask)
    assert read.text == "Come on, you fool!"
    box = read.lines[0].box
    assert 0 <= box.x < 60 and box.right > 400
