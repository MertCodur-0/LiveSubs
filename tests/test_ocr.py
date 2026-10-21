import threading

import cv2
import numpy as np
import pytest
from rapidfuzz import fuzz

from livesubs.engine.ocr import language
from livesubs.engine.ocr.winrt_ocr import OcrUnavailableError, WinRtOcr
from livesubs.engine.vision.preprocess import preprocess
from sample_data import Sample, fold, load_samples, sample_params

SAMPLES = load_samples()


# --- Language detection (no OCR engine needed) ---------------------------------------------------


@pytest.mark.parametrize(
    ("tags", "expected"),
    [
        (["tr", "en-US"], "en-US"),
        (["en-GB", "en-US"], "en-US"),
        (["tr", "en-GB"], "en-GB"),
        (["tr", "de-DE"], None),
        ([], None),
    ],
)
def test_best_english(tags: list[str], expected: str | None) -> None:
    assert language.best_english(tags) == expected


def test_install_command_names_the_capability() -> None:
    assert language.INSTALL_COMMAND.endswith("Language.OCR~~~en-US~0.0.1.0")


def test_install_request_reports_declined_elevation(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    def fake_shell_execute(*args: object) -> int:
        calls.append(args)
        return 5  # SE_ERR_ACCESSDENIED: the user said no to UAC

    monkeypatch.setattr(language, "_ShellExecuteW", fake_shell_execute)
    assert language.request_english_install() is False
    assert calls[0][1] == "runas"
    assert language.EN_US_CAPABILITY in calls[0][3]


# --- Engine --------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def ocr() -> WinRtOcr:
    if language.english_ocr_language() is None:
        pytest.skip("English OCR language pack not installed")
    engine = WinRtOcr(language.english_ocr_language())
    engine.recognize(np.full((40, 40), 255, np.uint8))  # warm-up
    return engine


def rendered(text: str, scale: float = 1.4) -> np.ndarray:
    image = np.full((90, 40 + int(len(text) * 30 * scale)), 255, np.uint8)
    cv2.putText(image, text, (20, 60), cv2.FONT_HERSHEY_SIMPLEX, scale, 0, 3, cv2.LINE_AA)
    return image


def test_unknown_language_is_reported() -> None:
    with pytest.raises(OcrUnavailableError):
        WinRtOcr("xx-XX")


@pytest.mark.ocr
def test_reads_rendered_text(ocr: WinRtOcr) -> None:
    result = ocr.recognize(rendered("GET IN THE CAR"))
    assert result.text == "GET IN THE CAR"
    assert len(result.lines) == 1
    box = result.lines[0].box
    assert 10 <= box.x <= 40 and box.width > 300
    assert result.elapsed_ms > 0


@pytest.mark.ocr
def test_blank_image_reads_nothing(ocr: WinRtOcr) -> None:
    assert ocr.recognize(np.full((100, 600), 255, np.uint8)).lines == ()


@pytest.mark.ocr
def test_color_input_and_oversized_image(ocr: WinRtOcr) -> None:
    bgr = cv2.cvtColor(rendered("HELLO"), cv2.COLOR_GRAY2BGR)
    assert ocr.recognize(bgr).text == "HELLO"
    wide = np.full((90, ocr.max_dimension + 2000), 255, np.uint8)
    wide[:, : rendered("HELLO").shape[1]] = rendered("HELLO")
    result = ocr.recognize(wide)
    assert result.text == "HELLO"
    assert result.lines[0].box.x < 60  # box mapped back to the original pixels


@pytest.mark.ocr
def test_works_from_a_worker_thread(ocr: WinRtOcr) -> None:
    out: list[str] = []
    worker = threading.Thread(target=lambda: out.append(ocr.recognize(rendered("HELLO")).text))
    worker.start()
    worker.join(timeout=10)
    assert out == ["HELLO"]


# --- Real screenshots ----------------------------------------------------------------------------


@pytest.mark.ocr
@pytest.mark.parametrize("sample", sample_params(SAMPLES))
def test_sample_reads_expected_text(ocr: WinRtOcr, sample: Sample) -> None:
    text = fold(ocr.recognize(preprocess(sample.load_region(), sample.vision).ocr_image).text)
    expected = fold(sample.expected)
    if not expected:
        assert text == ""
    else:
        # 15 of 18 samples read exactly; the rest lose a word on near-white ground or pick up
        # a stray mark (see samples.json notes).
        assert fuzz.ratio(text, expected) >= 95, text


@pytest.mark.ocr
def test_preprocessing_beats_raw_frames(ocr: WinRtOcr) -> None:
    available = [s for s in SAMPLES if s.path.is_file() and s.has_subtitle]
    if not available:
        pytest.skip("no sample images")
    scores = {"pre": [], "raw": []}
    for sample in available:
        region = sample.load_region()
        expected = fold(sample.expected)
        pre = ocr.recognize(preprocess(region, sample.vision).ocr_image).text
        raw = ocr.recognize(region).text
        scores["pre"].append(fuzz.ratio(fold(pre), expected))
        scores["raw"].append(fuzz.ratio(fold(raw), expected))
    assert np.mean(scores["pre"]) >= 99
    assert np.mean(scores["pre"]) > np.mean(scores["raw"])
    assert min(scores["pre"]) > min(scores["raw"])
