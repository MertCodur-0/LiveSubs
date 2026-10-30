"""The pipeline end to end on a scripted "video" of sample screenshots, real OCR, fake model."""

import dataclasses
import threading
import time
from collections.abc import Iterator

import cv2
import numpy as np
import pytest

from livesubs.core.config import OllamaSettings
from livesubs.core.geometry import NormRect, Rect
from livesubs.core.messages import (
    DEBUG_MAX_WIDTH,
    DebugFrame,
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
from livesubs.core.profiles import MonitorSource, Profile, VideoDeviceSource, VisionSettings
from livesubs.engine.capture.base import (
    BaseFrameSource,
    Frame,
    SourceInfo,
    SourceKind,
    SourceUnavailableError,
)
from livesubs.engine.capture.shared_frames import SharedFrameWriter
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
    return start_command_for(sample.region, sample.vision)


def start_command_for(region: NormRect, vision: VisionSettings | None = None) -> StartPipeline:
    profile = Profile(
        name="test",
        source=MonitorSource(device_name="scripted"),
        region=region,
        vision=vision or VisionSettings(),
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


def test_a_prompt_that_fills_the_context_is_reported_once(
    two_lines_and_a_gap: tuple[list[Sample], list[tuple[np.ndarray, float]]],
) -> None:
    """Ollama silently cuts a prompt longer than num_ctx (the examples first)."""
    (a, _), scenes = two_lines_and_a_gap

    class LongPrompt(UpperCaseBackend):
        def translate(self, request: TranslationRequest, cancel: CancelToken) -> TranslationResult:
            result = super().translate(request, cancel)
            return dataclasses.replace(result, prompt_tokens=2047)  # num_ctx 2048, cut

    source, recorder = ScriptedSource(scenes), Recorder()
    pipeline = Pipeline(
        start_command(a),
        recorder,
        open_source=lambda spec: (source.open(), source)[1],
        backend_factory=lambda start: LongPrompt(),
    )
    run(pipeline, source)

    assert len(recorder.of(SubtitleTranslated)) >= 2
    errors = recorder.of(EngineError)
    assert [(e.kind, e.message, e.fatal) for e in errors] == [
        (ErrorKind.CONTEXT_FULL, "2047/2048", False)
    ]


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


def test_debug_frames_and_live_vision_changes(
    two_lines_and_a_gap: tuple[list[Sample], list[tuple[np.ndarray, float]]],
) -> None:
    (a, _), scenes = two_lines_and_a_gap
    source, recorder = ScriptedSource([scenes[0]] * 3), Recorder()  # line A for ~3 s
    pipeline = Pipeline(
        start_command(a),
        recorder,
        open_source=lambda spec: (source.open(), source)[1],
        backend_factory=None,
    )
    pipeline.start()
    time.sleep(0.6)
    assert recorder.of(DebugFrame) == []  # nothing unless calibration asks for it
    pipeline.set_debug(True)
    time.sleep(1.0)
    frames = recorder.of(DebugFrame)
    assert 2 <= len(frames) <= 7  # throttled to ~5 per second
    frame = frames[-1]
    assert frame.region.ndim == 3 and frame.region.shape[2] == 3
    assert frame.region.shape[1] <= DEBUG_MAX_WIDTH and frame.ocr_input.ndim == 2
    assert frame.text_fraction > 0.01
    assert frame.last_read and frame.rejected == ""

    # Calibration: subtitles are not red, so with only red selected nothing looks like text.
    pipeline.set_vision(VisionSettings(text_colors=["#FF0000"]))
    time.sleep(0.8)
    assert recorder.of(DebugFrame)[-1].text_fraction < 0.001
    pipeline.stop()


def test_short_reply_between_long_lines_reaches_the_screen(
    two_lines_and_a_gap: tuple[list[Sample], list[tuple[np.ndarray, float]]],
) -> None:
    """User report: short replies after long lines were skipped. A short reply is on screen for
    less time than a translation takes; it must still be read and translated, in order."""
    (a, _), scenes = two_lines_and_a_gap
    frame_a, frame_b = scenes[0][0], scenes[2][0]
    short = frame_a.copy()
    r = a.region.to_pixels(short.shape[1], short.shape[0])
    short[r.y : r.bottom, r.x : r.right] = 30  # the long caption is gone...
    cv2.putText(  # ...and a short reply is up instead
        short,
        "Yeah.",
        (r.x + r.width // 2 - 40, r.bottom - 20),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.9,
        (255, 255, 255),
        2,
        cv2.LINE_AA,
    )

    class Slow(UpperCaseBackend):
        def translate(self, request: TranslationRequest, cancel: CancelToken) -> TranslationResult:
            time.sleep(0.8)  # longer than the short reply stays on screen
            return super().translate(request, cancel)

    source = ScriptedSource([(frame_a, 1.5), (short, 0.6), (frame_b, 1.5), (scenes[1][0], 1.5)])
    recorder = Recorder()
    pipeline = Pipeline(
        start_command(a),
        recorder,
        open_source=lambda spec: (source.open(), source)[1],
        backend_factory=lambda start: Slow(),
    )
    run(pipeline, source)

    reads = [e.text for e in recorder.of(SubtitleRead) if not e.upgrade]
    translated = [e.source for e in recorder.of(SubtitleTranslated)]
    assert len(reads) == 3 and reads[1] == "Yeah."
    assert translated == reads  # every line translated, in reading order


def _play_into(writer: SharedFrameWriter, scenes: list[tuple[np.ndarray, float]]) -> None:
    """The device host's side: every scene written at 30 fps, then the card goes away."""
    for image, seconds in scenes:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            writer.write(image, time.monotonic())
            time.sleep(1 / 30)
    writer.close()


def test_capture_card_frames_come_from_the_device_host(
    two_lines_and_a_gap: tuple[list[Sample], list[tuple[np.ndarray, float]]],
) -> None:
    (a, _), scenes = two_lines_and_a_gap
    scenes = [(cv2.resize(image[:, :, :3], (1920, 1080)), s) for image, s in scenes]
    writer = SharedFrameWriter(1920, 1080)
    writer.write(scenes[0][0], time.monotonic())
    start = start_command(a)
    profile = start.profile.model_copy(update={"source": VideoDeviceSource(device_name="Card")})
    recorder = Recorder()
    pipeline = Pipeline(
        StartPipeline(profile=profile, ollama=OllamaSettings(), shared_frames=writer.name),
        recorder,
        backend_factory=lambda start: UpperCaseBackend(),
    )
    pipeline.start()  # default source opener: reads the shared frames
    _play_into(writer, scenes)
    deadline = time.monotonic() + 5
    while pipeline.running and time.monotonic() < deadline:
        time.sleep(0.02)

    assert recorder.of(FrameInfo) == [FrameInfo(1920, 1080, None)]
    reads = [e.text for e in recorder.of(SubtitleRead) if not e.upgrade]
    assert len(reads) == 2
    assert len(recorder.of(SubtitleTranslated)) >= 2
    # closing the writer = the card was closed: the pipeline stops with "source lost"
    assert [(e.kind, e.fatal) for e in recorder.of(EngineError)] == [(ErrorKind.SOURCE_LOST, True)]
    assert recorder.states()[-1] is EngineState.STOPPED


def _caption(text: str) -> np.ndarray:
    """A dark 720p frame with a white caption at the bottom (inside ``CAPTION_REGION``)."""
    frame = np.full((720, 1280, 3), 30, np.uint8)
    cv2.putText(frame, text, (360, 660), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (255, 255, 255), 2)
    return frame


CAPTION_REGION = NormRect(x=0.2, y=0.85, width=0.6, height=0.12)


@pytest.mark.parametrize("merge", [True, False])
def test_a_sentence_split_over_two_subtitles_is_translated_whole(merge: bool) -> None:
    """User report (Spider-Man): "I knew Fisk" then "wouldn't go quietly." - translated apart,
    the first piece means something else. The second piece is sent as an upgrade of the first
    line, so the whole sentence is translated and replaces the first piece's translation."""

    class Slow(UpperCaseBackend):
        def translate(self, request: TranslationRequest, cancel: CancelToken) -> TranslationResult:
            time.sleep(0.6)
            return super().translate(request, cancel)

    source = ScriptedSource(
        [
            (_caption("I knew Fisk"), 1.0),
            (_caption("would not go quietly."), 1.2),
            (_caption("Next line here."), 1.2),
            (_caption(""), 1.5),
        ]
    )
    start = start_command_for(CAPTION_REGION)
    start.profile.translation.merge_split_sentences = merge
    recorder = Recorder()
    pipeline = Pipeline(
        start,
        recorder,
        open_source=lambda spec: (source.open(), source)[1],
        backend_factory=lambda start: Slow(),
    )
    run(pipeline, source)

    reads = [(e.line_id, e.text, e.upgrade) for e in recorder.of(SubtitleRead)]
    translated = [(e.line_id, e.source) for e in recorder.of(SubtitleTranslated)]
    if merge:
        assert reads == [
            (1, "I knew Fisk", False),
            (1, "I knew Fisk would not go quietly.", True),
            (2, "Next line here.", False),
        ]
        # The first piece's own translation may come first; the whole sentence replaces it.
        assert dict(translated) == {1: "I knew Fisk would not go quietly.", 2: "Next line here."}
    else:
        assert [r[1] for r in reads] == ["I knew Fisk", "would not go quietly.", "Next line here."]
        assert [t[1] for t in translated] == [r[1] for r in reads]
