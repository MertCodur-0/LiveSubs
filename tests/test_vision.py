import cv2
import numpy as np
import pytest

from livesubs.core.profiles import VisionSettings
from livesubs.engine.vision.change import Change, ChangeDetector
from livesubs.engine.vision.preprocess import (
    UPSCALE_FACTOR,
    color_mask,
    hex_to_bgr,
    preprocess,
)
from sample_data import Sample, load_samples, sample_params

SAMPLES = load_samples()
POSITIVES = [s for s in SAMPLES if s.has_subtitle]
NEGATIVES = [s for s in SAMPLES if not s.has_subtitle]


def components(mask: np.ndarray) -> int:
    return cv2.connectedComponents(mask)[0] - 1


def caption(
    text: str,
    *,
    color: tuple[int, int, int] = (255, 255, 255),
    background: np.ndarray | None = None,
    box: bool = True,
) -> np.ndarray:
    """A 600x80 subtitle region: text on an optional dark caption box over a background."""
    rng = np.random.default_rng(len(text))
    image = background if background is not None else rng.integers(0, 256, (80, 600, 3), np.uint8)
    image = image.copy()
    if box:
        image[15:65, 20:580] = 20
    cv2.putText(image, text, (30, 52), cv2.FONT_HERSHEY_SIMPLEX, 1.1, color, 2, cv2.LINE_AA)
    return image


# --- Building blocks -----------------------------------------------------------------------------


def test_hex_to_bgr() -> None:
    assert hex_to_bgr("#FFD700") == (0, 215, 255)


def test_color_mask_matches_any_configured_color() -> None:
    image = np.zeros((1, 3, 3), np.uint8)
    image[0, 0] = (255, 255, 255)
    image[0, 1] = (0, 215, 255)  # gold
    image[0, 2] = (0, 0, 255)  # red
    mask = color_mask(image, ["#FFFFFF", "#FFD700"], tolerance=30)
    assert mask[0].tolist() == [255, 255, 0]


# --- Preprocessing -------------------------------------------------------------------------------


def test_text_on_noise_becomes_black_on_white() -> None:
    result = preprocess(caption("Get in the car"), VisionSettings())
    assert result.scale == UPSCALE_FACTOR
    assert result.mask.shape == (160, 1200)
    assert 10 <= components(result.mask) <= 16  # one per glyph, roughly
    # OCR input: white background, black text exactly where the mask is.
    assert np.array_equal(result.ocr_image, 255 - result.mask)


def test_bgra_and_non_contiguous_input() -> None:
    bgr = caption("Hello")
    bgra = cv2.cvtColor(bgr, cv2.COLOR_BGR2BGRA)
    view = np.dstack([bgr, np.full(bgr.shape[:2], 255, np.uint8)])[:, ::1]
    expected = preprocess(bgr, VisionSettings()).mask
    assert np.array_equal(preprocess(bgra, VisionSettings()).mask, expected)
    assert np.array_equal(preprocess(view[10:70, 5:595], VisionSettings()).mask.shape, (120, 1180))


def test_without_upscale() -> None:
    result = preprocess(caption("Hello"), VisionSettings(upscale=False))
    assert result.scale == 1
    assert result.mask.shape == (80, 600)


def test_large_blob_of_text_color_is_dropped() -> None:
    image = caption("Hello")
    image[0:15, 100:400] = 255  # a bright sky/light strip above the box
    result = preprocess(image, VisionSettings())
    assert not result.mask[:30].any()
    assert components(result.mask) >= 4


def test_outline_support_rejects_text_colored_scenery() -> None:
    plain = np.full((80, 600, 3), 150, np.uint8)  # mid-gray, nothing dark anywhere
    scenery = caption("Hello", background=plain, box=False)
    assert components(preprocess(scenery, VisionSettings(outline_support=False)).mask) > 0
    assert components(preprocess(scenery, VisionSettings(outline_support=True)).mask) == 0
    # A caption box provides the dark surroundings.
    boxed = caption("Hello", background=plain, box=True)
    assert components(preprocess(boxed, VisionSettings(outline_support=True)).mask) > 0


def test_yellow_subtitles() -> None:
    settings = VisionSettings(text_colors=["#FFD700"])
    dark = np.full((80, 600, 3), 30, np.uint8)
    yellow = preprocess(caption("Wasted", color=(0, 215, 255), background=dark), settings)
    white = preprocess(caption("Wasted", background=dark), settings)
    assert components(yellow.mask) >= 5
    assert components(white.mask) == 0


def test_otsu_mode_without_color_mask() -> None:
    result = preprocess(caption("Hello", box=True), VisionSettings(color_mask=False))
    assert components(result.mask) > 0


# --- Change detection ----------------------------------------------------------------------------


def test_change_sequence() -> None:
    detector = ChangeDetector()
    settings = VisionSettings()
    blank = preprocess(np.full((80, 600, 3), 20, np.uint8), settings).mask
    line_a = preprocess(caption("Get in the car"), settings).mask
    line_b = preprocess(caption("Who are you?"), settings).mask

    assert detector.update(blank) is Change.NONE
    assert detector.update(line_a) is Change.APPEARED
    assert detector.update(line_a) is Change.SAME
    assert detector.update(line_b) is Change.CHANGED
    assert detector.update(blank) is Change.CLEARED
    assert detector.update(blank) is Change.NONE


def test_background_motion_is_not_a_change() -> None:
    detector = ChangeDetector()
    settings = VisionSettings()
    rng = np.random.default_rng(1)
    for _ in range(5):
        background = rng.integers(0, 256, (80, 600, 3), np.uint8)
        change = detector.update(preprocess(caption("Hello", background=background), settings).mask)
        assert change in {Change.APPEARED, Change.SAME}
    assert change is Change.SAME


def test_reset_forgets_previous_line() -> None:
    detector = ChangeDetector()
    mask = preprocess(caption("Hello"), VisionSettings()).mask
    detector.update(mask)
    detector.reset()
    assert detector.update(mask) is Change.APPEARED


# --- Real screenshots (skipped when the images are not present) ---------------------------------


@pytest.mark.parametrize("sample", sample_params(POSITIVES))
def test_sample_subtitle_yields_one_component_per_glyph(sample: Sample) -> None:
    result = preprocess(sample.load_region(), sample.vision)
    glyphs = sum(not c.isspace() for c in sample.expected)
    # i/j dots, apostrophes and accents add a few components; scenery junk would add many.
    assert 0.9 * glyphs <= components(result.mask) <= 1.25 * glyphs
    assert ChangeDetector().has_text(result.mask)


@pytest.mark.parametrize("sample", sample_params(NEGATIVES))
def test_sample_without_subtitle_yields_no_text(sample: Sample) -> None:
    result = preprocess(sample.load_region(), sample.vision)
    assert components(result.mask) == 0
    assert not ChangeDetector().has_text(result.mask)


@pytest.mark.parametrize("sample", sample_params(POSITIVES))
def test_sample_background_motion_keeps_the_line(sample: Sample) -> None:
    region = sample.load_region()
    first = preprocess(region, sample.vision)
    # Repaint everything away from the caption with colored noise: a busy, moving scene.
    near_text = cv2.dilate(first.mask, np.ones((25, 25), np.uint8))
    near_text = cv2.resize(near_text, (region.shape[1], region.shape[0])) > 0
    rng = np.random.default_rng(7)
    noise = rng.integers(0, 256, region.shape, np.uint8)
    noise[..., 0] //= 2  # never pure white: keep it scenery, not text
    moved = np.where(near_text[..., None], region, noise)

    detector = ChangeDetector()
    assert detector.update(first.mask) is Change.APPEARED
    assert detector.update(preprocess(moved, sample.vision).mask) is Change.SAME


def test_samples_differ_from_each_other() -> None:
    available = [s for s in POSITIVES if s.path.is_file()]
    if len(available) < 2:
        pytest.skip("needs at least two sample images")
    detector = ChangeDetector()
    detector.update(preprocess(available[0].load_region(), available[0].vision).mask)
    for sample in available[1:]:
        assert detector.update(preprocess(sample.load_region(), sample.vision).mask) is (
            Change.CHANGED
        ), sample.name
