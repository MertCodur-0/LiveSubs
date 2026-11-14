import threading

import cv2
import numpy as np
import pytest
from rapidfuzz import fuzz
from winrt.runtime import ApartmentType, init_apartment, uninit_apartment

from livesubs.engine.ocr import language
from livesubs.engine.ocr.winrt_ocr import OcrUnavailableError, WinRtOcr
from livesubs.engine.pipeline import read_subtitle
from livesubs.engine.text.normalize import clean
from livesubs.engine.text.quality import ReadScorer
from livesubs.engine.text.stabilizer import LineConfirmed, ScreenChanged, Stabilizer
from livesubs.engine.vision.change import ChangeDetector
from livesubs.engine.vision.preprocess import preprocess
from livesubs.platform.spellcheck import open_spell_checker
from sample_data import Sample, load_samples, sample_params

SAMPLES = load_samples()
# Bright road texture merges with the first words ("What should"); read as in the app, the rest
# of the line is exact. Every other sample reads at >= 95.
HARD_SAMPLES = {"200236": 80}


@pytest.fixture(scope="module")
def scorer() -> ReadScorer:
    checker = open_spell_checker()
    return ReadScorer(checker.misspelled if checker else None)


def read(ocr: WinRtOcr, scorer: ReadScorer, sample: Sample) -> str:
    """The sample read the way the pipeline reads: both OCR inputs, the cleaner one."""
    return clean(read_subtitle(ocr, preprocess(sample.load_region(), sample.vision), scorer)[0])


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
def test_works_on_a_single_threaded_apartment(ocr: WinRtOcr) -> None:
    """Regression: on a thread where Qt had created windows (an STA), WinRT refused the
    blocking get() ("Cannot call blocking method from single-threaded apartment")."""
    out: list[str] = []

    def sta_worker() -> None:
        init_apartment(ApartmentType.SINGLE_THREADED)
        try:
            out.append(ocr.recognize(rendered("HELLO")).text)
        finally:
            uninit_apartment()

    worker = threading.Thread(target=sta_worker)
    worker.start()
    worker.join(timeout=10)
    assert out == ["HELLO"]


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
def test_sample_reads_expected_text(ocr: WinRtOcr, scorer: ReadScorer, sample: Sample) -> None:
    text = read(ocr, scorer, sample)
    expected = clean(sample.expected)
    if not expected:
        assert text == ""
    else:
        # 14 of 17 lines read exactly; the rest misread one word or accent (see samples.json).
        assert fuzz.ratio(text, expected) >= HARD_SAMPLES.get(sample.name[-6:], 95), text


@pytest.mark.ocr
def test_preprocessing_beats_raw_frames(ocr: WinRtOcr, scorer: ReadScorer) -> None:
    available = [s for s in SAMPLES if s.path.is_file() and s.has_subtitle]
    if not available:
        pytest.skip("no sample images")
    scores = {"pre": [], "raw": []}
    for sample in available:
        expected = clean(sample.expected)
        raw = ocr.recognize(sample.load_region()).text
        scores["pre"].append(fuzz.ratio(read(ocr, scorer, sample), expected))
        scores["raw"].append(fuzz.ratio(clean(raw), expected))
    assert np.mean(scores["pre"]) >= 98
    assert np.mean(scores["pre"]) > np.mean(scores["raw"])
    assert min(scores["pre"]) > min(scores["raw"])


@pytest.mark.ocr
def test_sample_stream_confirms_each_line_once(ocr: WinRtOcr, scorer: ReadScorer) -> None:
    """Samples played as a subtitle stream: each line stays up for a few frames, with a frame
    without subtitles in between. Every line is confirmed exactly once and cleared after."""
    lines = [s for s in SAMPLES if s.has_subtitle and s.path.is_file()]
    blank = next((s for s in SAMPLES if not s.has_subtitle and s.path.is_file()), None)
    if len(lines) < 2 or blank is None:
        pytest.skip("needs sample images")

    detector, stab, now = ChangeDetector(), Stabilizer(scorer=scorer.score), 0.0
    confirmed: list[list[str]] = []  # per sample shown: the lines confirmed while it was up
    cleared = 0
    for sample in [x for line in lines for x in (line, blank)]:
        processed = preprocess(sample.load_region(), sample.vision)
        pixels = cv2.countNonZero(processed.mask)
        if sample.has_subtitle:
            confirmed.append([])
        for _ in range(4):  # the same frame for ~0.25 s
            now += 0.065
            decision = stab.update(detector.update(processed.mask), pixels, now)
            events = list(decision.events)
            if decision.read:
                events += stab.on_read(read_subtitle(ocr, processed, scorer)[2], now)
            confirmed[-1] += [e.text for e in events if isinstance(e, LineConfirmed)]
            cleared += sum(e == ScreenChanged(()) for e in events)

    assert cleared == len(lines)
    for texts, sample in zip(confirmed, lines, strict=True):
        # Two-row captions are one line; a caption broken after a sentence ("Who is this guy?" /
        # "You know, ...") reads like two speakers: two lines, together the whole caption.
        assert 1 <= len(texts) <= 2, texts
        minimum = HARD_SAMPLES.get(sample.name[-6:], 95)
        assert fuzz.ratio(" ".join(texts), clean(sample.expected)) >= minimum, texts
