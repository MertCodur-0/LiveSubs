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


def test_colored_words_beside_a_line_are_segments_but_never_start_one() -> None:
    letters = np.zeros((200, 900), np.uint8)
    end = _glyphs(letters, 100, 40, 6)
    colored = np.zeros_like(letters)
    word = end + 10
    word_end = _glyphs(colored, word, 40, 5)  # a colored word after the white ones
    _glyphs(colored, 300, 130, 8)  # colored glyphs alone on another row (scenery, a sign)
    line, extra = find_segments(letters, colored)
    assert line.row == extra.row == 0
    # Each is read alone, and their boxes never share a glyph.
    assert line.box.x < 100 and end - 4 <= line.box.right <= word
    assert extra.box.x >= end - 4 and extra.box.right >= word_end - 4


def test_a_colored_word_touching_a_line_joins_its_segment() -> None:
    # The letters mask holds "Find Arn" of "Find Arnold" (bright pink passes as white) and the
    # colored glyphs the rest: one segment, or the reads would be "Arn" + "old".
    letters = np.zeros((100, 600), np.uint8)
    end = _glyphs(letters, 100, 40, 6)
    colored = np.zeros_like(letters)
    word_end = _glyphs(colored, end + 2, 40, 3)
    (segment,) = find_segments(letters, colored)
    assert segment.box.x < 100 and segment.box.right >= word_end - 4


def test_a_tall_glyph_after_short_ones_stays_in_the_segment() -> None:
    # "mean! And": x-height letters, then a "!" and a capital that sit higher. The "!" used to
    # start a segment of its own, and both padded boxes read it ("mean!! And also crazy.").
    # Sizes as measured on the frame: x-height 18 px, "!" 17 px whose centre is 6.5 px higher.
    mask = np.zeros((120, 600), np.uint8)
    for i in range(4):
        cv2.rectangle(mask, (100 + 20 * i, 46), (113 + 20 * i, 63), 255, -1)  # "mean"
    cv2.rectangle(mask, (188, 40), (190, 56), 255, -1)  # "!" bar
    cv2.rectangle(mask, (205, 40), (224, 63), 255, -1)  # "A"
    for i in range(6):
        cv2.rectangle(mask, (229 + 20 * i, 46), (242 + 20 * i, 63), 255, -1)  # "nd also"
    (segment,) = find_segments(mask)
    assert segment.box.x < 100 and segment.box.right > 340


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
    """``text`` None: no segment to read; "": segments, none of them text."""

    def __init__(self, text: str | None) -> None:
        self.text = text

    def recognize(self, region: np.ndarray, letters: np.ndarray) -> OcrResult | None:
        if self.text is None:
            return None
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


def test_read_subtitle_falls_back_to_windows_when_there_is_no_segment() -> None:
    region, processed = _processed()
    windows = _FakeWindows()
    text, _, _ = read_subtitle(windows, processed, ReadScorer(None), _FakeLines(None), region)
    assert text == "from windows" and windows.calls == len(processed.ocr_inputs)


def test_segments_that_are_no_text_are_not_read_again_by_windows() -> None:
    region, processed = _processed()
    windows = _FakeWindows()
    text, _, rows = read_subtitle(windows, processed, ReadScorer(None), _FakeLines(""), region)
    assert (text, rows, windows.calls) == ("", [], 0)


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


def test_reads_a_colored_word_of_a_white_line(reader) -> None:
    # GTA's objectives: "Land near the" white, "docks" pink; the mask holds the white only.
    region = np.full((120, 900, 3), (40, 35, 30), np.uint8)
    font = cv2.FONT_HERSHEY_DUPLEX
    cv2.putText(region, "Land near the", (60, 75), font, 1.4, (235,) * 3, 2)
    x = 60 + cv2.getTextSize("Land near the ", font, 1.4, 2)[0][0]
    cv2.putText(region, "docks", (x, 75), font, 1.4, (160, 90, 235), 2)  # BGR pink
    processed = preprocess(region, VisionSettings())
    assert reader.recognize(region, processed.mask).text == "Land near the docks"


def test_a_clear_line_is_read_with_high_confidence(reader) -> None:
    region = np.full((120, 900, 3), (40, 60, 50), np.uint8)
    cv2.putText(region, "Get in the car!", (60, 75), cv2.FONT_HERSHEY_DUPLEX, 1.4, (235,) * 3, 2)
    (segment,) = find_segments(preprocess(region, VisionSettings()).mask)
    box = segment.box
    text, confidence = reader.read_line(region[box.y : box.bottom, box.x : box.right])
    assert text == "Get in the car!"
    assert confidence > 0.9


def test_scribbles_are_not_read_as_text(reader) -> None:
    # Glyph-sized strokes in a row, like fence posts or window frames: the mask chains them into
    # a segment, the recogniser is unsure of every "character" and the segment is dropped.
    rng = np.random.default_rng(7)
    region = np.full((120, 900, 3), (40, 60, 50), np.uint8)
    for i in range(16):
        x = 80 + 44 * i
        points = np.column_stack([x + rng.integers(0, 30, 5), 45 + rng.integers(0, 34, 5)])
        cv2.polylines(region, [points.astype(np.int32)], False, (235,) * 3, 3)
    processed = preprocess(region, VisionSettings())
    assert find_segments(processed.mask)  # the mask alone would call it text
    assert reader.recognize(region, processed.mask).lines == ()


def test_a_mask_without_segments_is_left_to_the_other_engine(reader) -> None:
    region = np.zeros((60, 200, 3), np.uint8)
    assert reader.recognize(region, np.zeros((60, 200), np.uint8)) is None
