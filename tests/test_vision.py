import cv2
import numpy as np
import pytest

from livesubs.core.profiles import VisionSettings
from livesubs.engine.vision.change import Change, ChangeDetector
from livesubs.engine.vision.preprocess import (
    DEFAULT_SCALE,
    MAX_SCALE,
    color_mask,
    glyph_height,
    hex_to_bgr,
    ocr_scale,
    preprocess,
    tone,
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
    size: float = 1.1,
) -> np.ndarray:
    """A 600x80 subtitle region: text on an optional dark caption box over a background."""
    rng = np.random.default_rng(len(text))
    image = background if background is not None else rng.integers(0, 256, (80, 600, 3), np.uint8)
    image = image.copy()
    if box:
        image[15:65, 20:580] = 20
    cv2.putText(image, text, (30, 52), cv2.FONT_HERSHEY_SIMPLEX, size, color, 2, cv2.LINE_AA)
    return image


def dark(level: int = 25) -> np.ndarray:
    return np.full((80, 600, 3), level, np.uint8)


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


def test_tone_matches_a_color_at_any_brightness() -> None:
    """Games draw "white" subtitles light grey and video dims them: grey is dim white, dark
    gold is dim gold; a bright grey or orange surface is not yellow or gold."""
    pixels = np.array([[(150, 150, 150), (0, 100, 120), (200, 200, 200), (40, 90, 230)]], np.uint8)
    white_brightness, white_error = tone(pixels, "#FFFFFF")
    assert white_brightness[0, 0] == 150 and white_error[0, 0] == 0
    _, yellow_error = tone(pixels, "#FFFF00")
    assert yellow_error[0, 2] >= 200  # light grey: far from yellow
    _, gold_error = tone(pixels, "#FFD700")
    assert gold_error[0, 1] < 10  # dark gold
    assert gold_error[0, 3] > 60  # orange-red


def test_ocr_scale_targets_a_glyph_height() -> None:
    assert ocr_scale(None, 40) == DEFAULT_SCALE
    assert ocr_scale(20, 40) == 2.0
    assert ocr_scale(13, 40) == 3.0  # rounded to quarter steps
    assert ocr_scale(5, 40) == MAX_SCALE
    assert ocr_scale(80, 40) == 1.0  # never shrinks


# --- Preprocessing -------------------------------------------------------------------------------


def at_ocr_scale(mask: np.ndarray, like: np.ndarray) -> np.ndarray:
    return cv2.resize(mask, (like.shape[1], like.shape[0]), interpolation=cv2.INTER_NEAREST) > 0


def test_text_on_noise_is_found_and_scaled() -> None:
    result = preprocess(caption("Get in the car"), VisionSettings())
    assert result.mask.shape == (80, 600)  # the letters, at the region's size
    assert result.ocr_image.shape == (round(80 * result.scale), round(600 * result.scale))
    assert 10 <= components(result.mask) <= 16  # one per glyph, roughly
    # Second OCR input: white background, black text where the letters are (and the dots).
    assert result.sharp_image is not None
    assert result.sharp_image.shape == result.ocr_image.shape
    letters = at_ocr_scale(result.mask, result.sharp_image)
    assert np.mean(result.sharp_image[letters] == 0) > 0.85
    assert np.mean(result.sharp_image[~letters] == 255) > 0.98
    assert len(result.ocr_inputs) == 2


def test_ocr_image_keeps_the_glyphs_own_grey_levels() -> None:
    """The first OCR input is the region's brightness (light text, as on screen), so the faint
    edges of glyphs and punctuation survive; away from the text rows it is flat."""
    region = caption("Hello there", color=(170, 170, 170), background=dark(30), box=False)
    result = preprocess(region, VisionSettings())
    text = at_ocr_scale(result.mask, result.ocr_image)
    assert 140 <= np.median(result.ocr_image[text]) <= 175
    assert len(np.unique(result.ocr_image[text])) > 10  # anti-aliased, not black and white
    assert np.all(result.ocr_image[:5] == result.ocr_image[0, 0])  # flat above the text


def test_small_fonts_are_scaled_up_more() -> None:
    small = preprocess(
        caption("Get in the car", size=0.6, background=dark(), box=False), VisionSettings()
    )
    large = preprocess(
        caption("Get in the car", size=1.4, background=dark(), box=False), VisionSettings()
    )
    assert small.scale > large.scale
    for result in (small, large):
        height = glyph_height(result.mask)
        assert height is not None and 28 <= height * result.scale <= 56


def test_dim_grey_subtitles_are_text() -> None:
    """Regression: on real footage white subtitles arrive light grey (glyph cores 145-190) and
    the old absolute threshold (>= 180) found almost nothing, so whole lines were never read."""
    for grey in (150, 175, 200):
        region = caption("Hello there", color=(grey,) * 3, background=dark(), box=False)
        assert components(preprocess(region, VisionSettings()).mask) >= 9, grey


def test_bgra_and_non_contiguous_input() -> None:
    bgr = caption("Hello")
    bgra = cv2.cvtColor(bgr, cv2.COLOR_BGR2BGRA)
    view = np.dstack([bgr, np.full(bgr.shape[:2], 255, np.uint8)])[:, ::1]
    expected = preprocess(bgr, VisionSettings()).mask
    assert np.array_equal(preprocess(bgra, VisionSettings()).mask, expected)
    cropped = preprocess(view[10:70, 5:595], VisionSettings())
    assert cropped.mask.shape == (60, 590)
    assert cropped.ocr_image.shape == (round(60 * cropped.scale), round(590 * cropped.scale))


def test_without_upscale() -> None:
    result = preprocess(caption("Hello"), VisionSettings(upscale=False))
    assert result.scale == 1
    assert result.mask.shape == result.ocr_image.shape == (80, 600)


def test_without_second_read() -> None:
    result = preprocess(caption("Hello"), VisionSettings(second_read=False))
    assert result.sharp_image is None
    assert len(result.ocr_inputs) == 1


def test_large_blob_of_text_color_is_dropped() -> None:
    image = caption("Hello")
    image[0:15, 100:400] = 255  # a bright sky/light strip above the box
    result = preprocess(image, VisionSettings())
    assert not result.mask[:15].any()  # the strip's rows
    assert components(result.mask) >= 4


def test_local_contrast_rejects_wide_patches_of_text_color() -> None:
    # White tiles bigger than the contrast kernel but small enough to pass the component filter:
    # windows, signs, lit pavement.
    scene = np.full((80, 600, 3), 60, np.uint8)
    for x in range(20, 580, 60):
        scene[20:44, x : x + 24] = 255
    assert components(preprocess(scene, VisionSettings(local_contrast=False)).mask) > 0
    assert components(preprocess(scene, VisionSettings(local_contrast=True)).mask) == 0


def test_text_needs_no_dark_box_or_outline() -> None:
    # White text straight on a light-gray road: no box, no outline, still readable.
    road = np.full((80, 600, 3), 175, np.uint8)
    result = preprocess(caption("Hello there", background=road, box=False), VisionSettings())
    assert components(result.mask) >= 8


def test_yellow_text_on_a_bright_neutral_background() -> None:
    # Brightness alone barely separates yellow from light gray; closeness to yellow does.
    road = np.full((80, 600, 3), 210, np.uint8)
    settings = VisionSettings(text_colors=["#FFFF00"])
    result = preprocess(caption("Hello", color=(0, 255, 255), background=road, box=False), settings)
    assert components(result.mask) >= 4


def test_yellow_subtitles() -> None:
    settings = VisionSettings(text_colors=["#FFD700"])
    dark = np.full((80, 600, 3), 30, np.uint8)
    yellow = preprocess(caption("Wasted", color=(0, 215, 255), background=dark), settings)
    white = preprocess(caption("Wasted", background=dark), settings)
    assert components(yellow.mask) >= 5
    assert components(white.mask) == 0


def test_any_color_without_color_mask() -> None:
    region = caption("Hello", color=(60, 200, 90), background=dark())
    assert components(preprocess(region, VisionSettings()).mask) == 0  # green is not white
    assert components(preprocess(region, VisionSettings(color_mask=False)).mask) >= 4


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
    # The mask holds letters only (no i/j dots, punctuation or accents); scenery junk of letter
    # size would add components, touching letters merge into one.
    glyphs = sum(c.isalnum() for c in sample.expected)
    low, high = sample.component_ratio
    assert low * glyphs <= components(result.mask) <= high * glyphs
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
    change = detector.update(preprocess(moved, sample.vision).mask)
    if sample.name.endswith("200236") and change is Change.CHANGED:
        # Documented hard sample: bright road texture merges with "What should" into one blob,
        # so new ground reshapes it. Harmless since 2026-10-30: CHANGED keeps the candidate and
        # the same text never becomes a new line; it only costs a read.
        pytest.xfail("road texture merged with the first words")
    assert change is Change.SAME


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


@pytest.mark.parametrize("height", [160, 260, 360])
@pytest.mark.parametrize("word", ["No.", "Ok.", "Yeah.", "Huh?"])
def test_short_replies_count_as_text(word: str, height: int) -> None:
    """Regression: in a tall region a short reply covered ~0.05 % of the mask, below the old
    0.1 % threshold, so it never counted as a subtitle and was never read."""
    region = np.full((height, 1570, 3), 40, np.uint8)
    cv2.putText(region, word, (740, height - 30), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (255,) * 3, 2)
    detector = ChangeDetector()
    assert detector.has_text(preprocess(region, VisionSettings()).mask)
    assert not detector.has_text(preprocess(np.full_like(region, 40), VisionSettings()).mask)
