"""The pipeline end to end on a scripted "video" of sample screenshots, real OCR, fake model."""

import threading
import time
from collections.abc import Iterator

import numpy as np
import pytest

from livesubs.core.config import OllamaSettings
from livesubs.core.geometry import Rect
from livesubs.core.messages import (
    EngineError,
    EngineState,
    ErrorKind,
    Event,
    FrameInfo,
    StartPipeline,
    StateChanged,
    SubtitleCleared,
    SubtitleRead,
    SubtitleTranslated,
)
from livesubs.core.profiles import MonitorSource, Profile
from livesubs.engine.capture.base import (
    BaseFrameSource,
    Frame,
    SourceInfo,
    SourceKind,
    SourceUnavailableError,
)
from livesubs.engine.ocr.language import english_ocr_language
from livesubs.engine.pipeline import Pipeline
from livesubs.engine.translate.base import (
    BackendUnavailableError,
    CancelToken,
    Lane,
    TranslationRequest,
    TranslationResult,
)
from sample_data import Sample, load_samples

pytestmark = [
    pytest.mark.ocr,
    pytest.mark.skipif(english_ocr_language() is None, reason="English OCR not installed"),
]
SAMPLES = load_samples()


class ScriptedSource(BaseFrameSource):
    """Plays (image, seconds) scenes in real time, like a capture source."""

    def __init__(self, scenes: list[tuple[np.ndarray, float]]) -> None:
        super().__init__()
        self._scenes = scenes
        self._thread = threading.Thread(target=self._play, daemon=True)
        self._stopped = threading.Event()
        self.finished = threading.Event()

    @property
    def info(self) -> SourceInfo:
        return SourceInfo(SourceKind.MONITOR, "scripted", "test", 30.0)

    def screen_rect(self) -> Rect | None:
        return None

    def _start(self) -> None:
        self._thread.start()

    def _stop(self) -> None:
        self._stopped.set()

    def _play(self) -> None:
        for image, seconds in self._scenes:
            end = time.monotonic() + seconds
            while time.monotonic() < end and not self._stopped.is_set():
                self._slot.put(Frame(image, time.monotonic()))  # a new frame every 33 ms
                time.sleep(1 / 30)
        self.finished.set()


class UpperCaseBackend:
    model = "fake:1b"
    lane = Lane.GPU

    def __init__(self) -> None:
        self.warmed_up = False

    def warm_up(self) -> float:
        self.warmed_up = True
        return 1.0

    def translate(self, request: TranslationRequest, cancel: CancelToken) -> TranslationResult:
        return TranslationResult(request.text.upper(), self.model, self.lane, 5.0)


class Recorder:
    def __init__(self) -> None:
        self.events: list[Event] = []
        self._lock = threading.Lock()

    def __call__(self, event: Event) -> None:
        with self._lock:
            self.events.append(event)

    def of(self, kind: type) -> list:
        with self._lock:
            return [e for e in self.events if isinstance(e, kind)]

    def states(self) -> list[EngineState]:
        return [e.state for e in self.of(StateChanged)]


def start_command(sample: Sample) -> StartPipeline:
    profile = Profile(
        name="test",
        source=MonitorSource(device_name="scripted"),
        region=sample.region,
        vision=sample.vision,
    )
    return StartPipeline(profile=profile, ollama=OllamaSettings(), profile_id="test")


@pytest.fixture
def two_lines_and_a_gap() -> tuple[list[Sample], list[tuple[np.ndarray, float]]]:
    lines = [s for s in SAMPLES if s.has_subtitle and s.path.is_file()][:2]
    blank = next((s for s in SAMPLES if not s.has_subtitle and s.path.is_file()), None)
    if len(lines) < 2 or blank is None:
        pytest.skip("needs sample images")
    a, b = lines
    scenes = [(a.load(), 1.0), (blank.load(), 0.8), (b.load(), 1.0), (blank.load(), 0.8)]
    return lines, scenes


def run(pipeline: Pipeline, source: ScriptedSource, timeout: float = 15.0) -> None:
    pipeline.start()
    assert source.finished.wait(timeout)
    time.sleep(0.3)
    pipeline.stop()


def test_lines_are_read_translated_and_cleared(
    two_lines_and_a_gap: tuple[list[Sample], list[tuple[np.ndarray, float]]],
) -> None:
    (a, _), scenes = two_lines_and_a_gap
    source, backend, recorder = ScriptedSource(scenes), UpperCaseBackend(), Recorder()
    pipeline = Pipeline(
        start_command(a),
        recorder,
        open_source=lambda spec: (source.open(), source)[1],
        backend_factory=lambda start: backend,
    )
    run(pipeline, source)

    assert backend.warmed_up
    assert recorder.states() == [
        EngineState.STARTING,
        EngineState.WARMING_UP,
        EngineState.RUNNING,
        EngineState.STOPPED,
    ]
    info = recorder.of(FrameInfo)[0]
    assert (info.width, info.height, info.screen_rect) == (*a.load().shape[1::-1], None)

    reads = [e for e in recorder.of(SubtitleRead) if not e.upgrade]
    assert [e.line_id for e in reads] == [1, 2]
    translated = recorder.of(SubtitleTranslated)
    assert [e.line_id for e in translated] == [1, 2]
    for read, done in zip(reads, translated, strict=True):
        assert done.source == read.text
        assert done.translation == read.text.upper()
        assert done.model == "fake:1b" and done.lane == "gpu"
        assert done.ocr_ms > 0
    assert len(recorder.of(SubtitleCleared)) == 2
    assert not recorder.of(EngineError)


def test_lines_shown_during_warm_up_are_not_lost(
    two_lines_and_a_gap: tuple[list[Sample], list[tuple[np.ndarray, float]]],
) -> None:
    """Regression: reading used to start only after the model warmed up (7.5 s in a real run),
    so the first subtitles after Start were never read."""
    (a, _), scenes = two_lines_and_a_gap

    class SlowToLoad(UpperCaseBackend):
        def warm_up(self) -> float:
            time.sleep(1.5)  # longer than the first subtitle stays on screen
            return super().warm_up()

    source, recorder = ScriptedSource(scenes), Recorder()
    pipeline = Pipeline(
        start_command(a),
        recorder,
        open_source=lambda spec: (source.open(), source)[1],
        backend_factory=lambda start: SlowToLoad(),
    )
    run(pipeline, source)

    first = next(e for e in recorder.of(SubtitleRead) if not e.upgrade)
    assert first.line_id == 1
    translated = {e.line_id: e for e in recorder.of(SubtitleTranslated)}
    assert translated[1].translation == first.text.upper()  # translated once the model was ready
    assert 2 in translated


def test_without_ollama_english_still_flows(
    two_lines_and_a_gap: tuple[list[Sample], list[tuple[np.ndarray, float]]],
) -> None:
    (a, _), scenes = two_lines_and_a_gap

    class Unreachable(UpperCaseBackend):
        def warm_up(self) -> float:
            raise BackendUnavailableError("Ollama is not reachable")

    source, recorder = ScriptedSource(scenes), Recorder()
    pipeline = Pipeline(
        start_command(a),
        recorder,
        open_source=lambda spec: (source.open(), source)[1],
        backend_factory=lambda start: Unreachable(),
    )
    run(pipeline, source)

    errors = recorder.of(EngineError)
    assert [(e.kind, e.fatal) for e in errors] == [(ErrorKind.OLLAMA_UNAVAILABLE, False)]
    assert EngineState.RUNNING in recorder.states()
    assert len([e for e in recorder.of(SubtitleRead) if not e.upgrade]) == 2
    assert recorder.of(SubtitleTranslated) == []


def test_unavailable_source_stops_with_an_error() -> None:
    profile = Profile(name="x", source=MonitorSource(device_name="nowhere"))
    recorder = Recorder()

    def refuse(spec: object) -> Iterator:
        raise SourceUnavailableError("monitor nowhere is not connected")

    pipeline = Pipeline(
        StartPipeline(profile=profile, ollama=OllamaSettings()),
        recorder,
        open_source=refuse,
        backend_factory=None,
    )
    pipeline.start()
    deadline = time.monotonic() + 5
    while pipeline.running and time.monotonic() < deadline:
        time.sleep(0.02)
    assert recorder.states() == [EngineState.STARTING, EngineState.STOPPED]
    assert [(e.kind, e.fatal) for e in recorder.of(EngineError)] == [
        (ErrorKind.SOURCE_UNAVAILABLE, True)
    ]


def test_pause_reads_nothing_until_resumed(
    two_lines_and_a_gap: tuple[list[Sample], list[tuple[np.ndarray, float]]],
) -> None:
    (a, _), scenes = two_lines_and_a_gap
    source, recorder = ScriptedSource(scenes), Recorder()
    pipeline = Pipeline(
        start_command(a),
        recorder,
        open_source=lambda spec: (source.open(), source)[1],
        backend_factory=None,
    )
    pipeline.set_paused(True)  # paused before the first subtitle
    pipeline.start()
    time.sleep(1.2)  # the first line comes and goes while paused
    assert recorder.of(SubtitleRead) == []
    pipeline.set_paused(False)
    assert source.finished.wait(10)
    time.sleep(0.3)
    pipeline.stop()

    states = recorder.states()
    assert EngineState.PAUSED in states
    assert states.index(EngineState.PAUSED) < len(states) - 1
    assert len([e for e in recorder.of(SubtitleRead) if not e.upgrade]) == 1  # only the second
