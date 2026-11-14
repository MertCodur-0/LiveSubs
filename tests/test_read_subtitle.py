"""``pipeline.read_subtitle``: which OCR inputs are read, and the rows it returns (fake OCR)."""

from __future__ import annotations

import numpy as np

from livesubs.core.geometry import Rect
from livesubs.engine.ocr.winrt_ocr import OcrLine, OcrResult
from livesubs.engine.pipeline import EmptyReadBackoff, read_subtitle
from livesubs.engine.text.quality import ReadScorer
from livesubs.engine.vision.preprocess import Preprocessed


class ScriptedOcr:
    """Answers each OCR input by its fill value: ``{value: [(text, box), ...]}``."""

    def __init__(self, answers: dict[int, list[tuple[str, Rect]]]) -> None:
        self.answers = answers
        self.calls: list[tuple[int, tuple[int, ...]]] = []

    def recognize(self, image: np.ndarray) -> OcrResult:
        value = int(image.flat[0])
        self.calls.append((value, image.shape))
        lines = tuple(OcrLine(text, box) for text, box in self.answers.get(value, []))
        return OcrResult(lines, 1.0)


GREY, SHARP = 100, 200


def processed(width: int = 400, height: int = 100) -> Preprocessed:
    mask = np.zeros((height // 2, width // 2), np.uint8)
    return Preprocessed(
        mask=mask,
        ocr_image=np.full((height, width), GREY, np.uint8),
        sharp_image=np.full((height, width), SHARP, np.uint8),
        scale=2.0,
    )


def test_the_mask_image_is_read_even_when_the_grey_one_finds_nothing() -> None:
    ocr = ScriptedOcr({SHARP: [("Cool.", Rect(150, 10, 100, 30))]})
    text, _, _ = read_subtitle(ocr, processed(), ReadScorer())
    assert [value for value, _ in ocr.calls] == [GREY, SHARP]
    assert text == "Cool."


def test_both_inputs_are_read_and_the_cleaner_read_wins() -> None:
    ocr = ScriptedOcr(
        {
            GREY: [("He1y Get 0ut!", Rect(100, 10, 200, 30))],
            SHARP: [("Hey! Get out!", Rect(100, 10, 200, 30))],
        }
    )
    text, _, rows = read_subtitle(ocr, processed(), ReadScorer())
    assert [value for value, _ in ocr.calls] == [GREY, SHARP]
    assert text == "Hey! Get out!"
    assert [row.text for row in rows] == ["Hey! Get out!"]
    assert rows[0].center == 0.5


def test_backoff_slows_reads_of_scenery_only_while_idle() -> None:
    backoff = EmptyReadBackoff(hold_s=0.5, growth=0.15)
    assert not backoff.skip(1000, 0.0, idle=True)
    backoff.on_read(False, 1000, 0.0, idle=True)  # scenery: nothing found
    assert backoff.skip(1050, 0.2, idle=True)  # same amount of "glyphs": wait
    assert not backoff.skip(1200, 0.2, idle=True)  # the mask grew: a subtitle may have appeared
    assert not backoff.skip(1000, 0.2, idle=False)  # a line is on screen or waiting
    assert not backoff.skip(1000, 0.5, idle=True)  # held long enough
    backoff.on_read(True, 1000, 0.5, idle=True)  # found text: no more waiting
    assert not backoff.skip(1000, 0.6, idle=True)


def test_backoff_never_starts_from_a_read_while_lines_are_tracked() -> None:
    backoff = EmptyReadBackoff()
    backoff.on_read(False, 1000, 0.0, idle=False)  # a line may be gone: the stabilizer decides
    assert not backoff.skip(1000, 0.1, idle=True)
