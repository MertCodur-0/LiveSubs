"""One running chain: source -> region -> preprocess -> change -> stabilizer -> OCR -> translation.

Runs on its own thread inside the engine process and reports everything through ``emit``
(``core.messages`` events). The loop runs at the profile's fps, and every ``fast_reread_s`` while
a new line waits for its confirming read. Frames are only processed when they are new, or when
the stabilizer wants another read. OCR runs inline (10-20 ms); translation runs on the
orchestrator's thread, so a slow model never delays reading the next subtitle. Each read OCRs both
of the preprocessor's images and keeps the one that looks more like English (``text.quality``, with
Windows' spell checker). The model warms up on
a side thread while reading already runs; the newest line read meanwhile is translated once the
model is ready. The region may hold several subtitle lines at once (people talking over each
other); the stabilizer tracks each one, so every line is translated once, on its own, and
``SubtitleLines`` tells the UI which lines are on screen. A subtitle that continues the previous
one's sentence (``text.sentences``) comes as an upgrade of that line: the whole sentence is
translated and replaces the first piece's translation.

Without Ollama (or without the model) the pipeline keeps running and shows the English lines.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path

import cv2
import numpy as np
from rapidfuzz import fuzz

from livesubs.core.geometry import NormRect
from livesubs.core.messages import (
    DEBUG_FPS,
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
    SubtitleLines,
    SubtitleRead,
    SubtitleTranslated,
)
from livesubs.core.profiles import Source, VisionSettings
from livesubs.engine.capture import FrameSource, SourceLostError, SourceUnavailableError
from livesubs.engine.capture import open_source as default_open_source
from livesubs.engine.ocr.language import english_ocr_language
from livesubs.engine.ocr.ppocr import LineReader, open_line_reader
from livesubs.engine.ocr.winrt_ocr import OcrLine, OcrResult, WinRtOcr
from livesubs.engine.text import filters
from livesubs.engine.text.normalize import clean, key
from livesubs.engine.text.quality import ReadScorer
from livesubs.engine.text.stabilizer import (
    Event as LineEvent,
)
from livesubs.engine.text.stabilizer import (
    LineConfirmed,
    LineUpgraded,
    Row,
    ScreenChanged,
    Stabilizer,
)
from livesubs.engine.translate.base import (
    BackendUnavailableError,
    ModelNotFoundError,
    TranslationBackend,
    TranslationError,
)
from livesubs.engine.translate.cache import TranslationCache
from livesubs.engine.translate.ollama import OllamaBackend
from livesubs.engine.translate.orchestrator import (
    LineFailed,
    LineTranslated,
    TranslationEvent,
    TranslationOrchestrator,
)
from livesubs.engine.vision.change import Change, ChangeDetector
from livesubs.engine.vision.preprocess import Preprocessed, preprocess
from livesubs.platform.spellcheck import open_spell_checker

log = logging.getLogger(__name__)

FIRST_FRAME_TIMEOUT_S = 5.0
CAPTURE_MAX_FPS = 30.0
# Room a translation needs after the prompt; a prompt closer to num_ctx than this gets a warning
# (a cut prompt reports num_ctx - 1 tokens).
CONTEXT_MARGIN_TOKENS = 128

SourceOpener = Callable[[Source], FrameSource]
BackendFactory = Callable[[StartPipeline], TranslationBackend]


def ollama_backend(start: StartPipeline) -> OllamaBackend:
    settings = start.ollama
    return OllamaBackend(
        settings.gpu_model,
        url=settings.url,
        num_ctx=settings.num_ctx,
        num_gpu=settings.gpu_num_gpu,
        keep_alive=settings.keep_alive,
    )


def _error_kind(error: TranslationError) -> ErrorKind:
    if isinstance(error, BackendUnavailableError):
        return ErrorKind.OLLAMA_UNAVAILABLE
    if isinstance(error, ModelNotFoundError):
        return ErrorKind.MODEL_NOT_FOUND
    return ErrorKind.TRANSLATION_FAILED


class Pipeline:
    def __init__(
        self,
        start: StartPipeline,
        emit: Callable[[Event], None],
        *,
        open_source: SourceOpener | None = None,
        backend_factory: BackendFactory | None = ollama_backend,
        cache_path: Path | None = None,
    ) -> None:
        """``backend_factory=None`` runs without translation (English only)."""
        if start.profile.source is None:
            raise ValueError("the profile has no source")
        self._start = start
        self._emit = emit
        self._open_source = open_source or (
            lambda spec: default_open_source(
                spec, max_fps=CAPTURE_MAX_FPS, shared_frames=start.shared_frames
            )
        )
        self._backend_factory = backend_factory
        self._cache_path = cache_path
        self._region = start.profile.region or NormRect(x=0.1, y=0.75, width=0.8, height=0.2)
        self._region_changed = False
        self._paused = False
        self._vision = start.profile.vision
        # Calibration: what the Inspector shows.
        self._debug = False
        self._last_debug = 0.0
        self._last_read = ""
        self._last_rejected = ""
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="pipeline", daemon=True)
        self._ocr_ms: dict[int, float] = {}
        # Set by the warm-up thread once the model answers; lines read before that wait.
        self._lock = threading.Lock()
        self._orchestrator: TranslationOrchestrator | None = None
        self._backend: TranslationBackend | None = None
        self._waiting: tuple[int, str] | None = None
        self._translated: set[int] = set()  # lines whose translation went out
        self._early: str | None = None  # the text whose translation was started early
        self._context_warned = False

    def start(self) -> None:
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread.is_alive() and self._thread is not threading.current_thread():
            self._thread.join(timeout)

    def set_region(self, region: NormRect) -> None:
        self._region = region
        self._region_changed = True

    def set_paused(self, paused: bool) -> None:
        """Keep the source open but read and translate nothing (hotkey while not needed)."""
        self._paused = paused

    def set_debug(self, enabled: bool) -> None:
        """Calibration: emit DebugFrame events (at most DEBUG_FPS)."""
        self._debug = enabled

    def set_vision(self, vision: VisionSettings) -> None:
        """New preprocessing settings: the mask means something else now, so start over."""
        self._vision = vision
        self._region_changed = True

    @property
    def running(self) -> bool:
        return self._thread.is_alive()

    # --- thread ----------------------------------------------------------------------------------

    def _run(self) -> None:
        source: FrameSource | None = None
        cache: TranslationCache | None = None
        spell = None
        try:
            self._emit(StateChanged(EngineState.STARTING))
            source = self._open(self._start.profile.source)
            ocr = self._ocr()
            if source is None or ocr is None:
                return
            first = source.wait_for_frame(0.0, timeout=FIRST_FRAME_TIMEOUT_S)
            if first is None:
                self._fatal(ErrorKind.SOURCE_UNAVAILABLE, "no frame from the source (minimized?)")
                return
            rect = source.screen_rect()
            screen = (rect.x, rect.y, rect.width, rect.height) if rect else None
            self._emit(FrameInfo(first.width, first.height, screen))

            cache = TranslationCache(self._cache_path)
            if self._backend_factory is None:
                self._emit(StateChanged(EngineState.RUNNING))
            else:
                # Read from the first frame on: warming the model up can take 10-40 s, and
                # subtitles shown meanwhile must not be lost.
                threading.Thread(
                    target=self._prepare_translation, args=(cache,), name="warm-up", daemon=True
                ).start()
            spell = open_spell_checker()  # COM object: created and used on this thread only
            self._loop(source, ocr, ReadScorer(spell.misspelled if spell else None))
        except SourceLostError as exc:
            self._fatal(ErrorKind.SOURCE_LOST, str(exc))
        except Exception as exc:
            log.exception("Pipeline crashed")
            self._fatal(ErrorKind.INTERNAL, f"{type(exc).__name__}: {exc}")
        finally:
            self._stop.set()  # also tells a warm-up still in progress to give up
            with self._lock:
                orchestrator, self._orchestrator = self._orchestrator, None
                backend, self._backend = self._backend, None
            if orchestrator is not None:
                orchestrator.close()
            _close(backend)
            if source is not None:
                source.close()
            if cache is not None:
                cache.close()
            if spell is not None:
                spell.close()
            self._emit(StateChanged(EngineState.STOPPED))

    def _open(self, spec: Source) -> FrameSource | None:
        try:
            return self._open_source(spec)
        except SourceUnavailableError as exc:
            self._fatal(ErrorKind.SOURCE_UNAVAILABLE, str(exc))
            return None

    def _ocr(self) -> WinRtOcr | None:
        language = english_ocr_language()
        if language is None:
            self._fatal(ErrorKind.OCR_UNAVAILABLE, "the English OCR language pack is missing")
            return None
        return WinRtOcr(language)

    def _prepare_translation(self, cache: TranslationCache) -> None:
        """Warm-up thread: load the model, then let waiting and new lines through."""
        assert self._backend_factory is not None
        backend: TranslationBackend | None = None
        try:
            backend = self._backend_factory(self._start)
            self._emit(StateChanged(EngineState.WARMING_UP, backend.model))
            warm_up = getattr(backend, "warm_up", None)
            if warm_up is not None:
                warm_up()
        except Exception as exc:
            _close(backend)
            if self._stop.is_set():
                return  # stopped while the model was loading: nothing to report
            # Keep reading subtitles; the UI shows English only and explains why.
            if isinstance(exc, TranslationError):
                self._emit(EngineError(_error_kind(exc), str(exc)))
            else:  # never leave the UI in "warming up" because of a bug or an odd reply
                log.exception("Preparing the translation failed")
                self._emit(EngineError(ErrorKind.INTERNAL, f"{type(exc).__name__}: {exc}"))
            self._emit(StateChanged(self._ready_state()))
            return
        profile = self._start.profile
        orchestrator = TranslationOrchestrator(
            backend,
            cache,
            self._on_translation,
            profile_id=self._start.profile_id,
            history_size=self._start.ollama.context_lines,
            game_context=profile.translation.context,
            glossary=profile.translation.glossary,
            examples=profile.translation.examples,
        )
        with self._lock:
            if self._stop.is_set():  # stopped while the model was loading
                orchestrator.close()
                _close(backend)
                return
            self._orchestrator = orchestrator
            self._backend = backend
            waiting, self._waiting = self._waiting, None
        self._emit(StateChanged(self._ready_state()))
        if waiting is not None:
            orchestrator.submit(*waiting)

    def _ready_state(self) -> EngineState:
        """The state once the model is ready (or given up on): paused stays paused. The warm-up
        used to report RUNNING over a pause made meanwhile, and the pause key then did nothing
        at its first press."""
        return EngineState.PAUSED if self._paused else EngineState.RUNNING

    def _loop(self, source: FrameSource, ocr: WinRtOcr, scorer: ReadScorer) -> None:
        profile = self._start.profile
        tick = 1.0 / profile.capture.fps
        detector = ChangeDetector()
        backoff = EmptyReadBackoff()
        stabilizer = Stabilizer(
            merge_split_sentences=profile.translation.merge_split_sentences, scorer=scorer.score
        )
        last_timestamp = -1.0
        paused = False
        lines = open_line_reader()  # None: Windows OCR reads alone
        while not self._stop.wait(
            stabilizer.fast_reread_s if stabilizer.awaiting_confirmation else tick
        ):
            if self._paused != paused:
                paused = self._paused
                self._emit(StateChanged(EngineState.PAUSED if paused else EngineState.RUNNING))
                self._region_changed = True  # start over either way: forget the current line
            if self._region_changed:
                self._region_changed = False
                last_timestamp = -1.0  # process the current frame again, with the new settings
                detector.reset()  # a new region: whatever was read before is gone
                backoff.reset()
                for event in stabilizer.update(Change.CLEARED, 0, time.monotonic()).events:
                    self._on_line(event, 0.0)
                self._start_early(stabilizer.expected)
            if paused:
                continue
            frame = source.latest_frame()
            if frame is None:
                continue
            if frame.timestamp == last_timestamp and not stabilizer.awaiting_confirmation:
                continue  # nothing new on screen
            last_timestamp = frame.timestamp

            r = self._region.to_pixels(frame.width, frame.height)
            region = frame.image[r.y : r.bottom, r.x : r.right]
            processed = preprocess(region, self._vision)
            change = detector.update(processed.mask)
            now = time.monotonic()
            if self._debug and now - self._last_debug >= 1 / DEBUG_FPS:
                self._last_debug = now
                self._emit(self._debug_frame(region, processed, change))
            pixels = cv2.countNonZero(processed.mask)
            decision = stabilizer.update(change, pixels, now)
            for event in decision.events:
                self._on_line(event, 0.0)
            self._start_early(stabilizer.expected)
            idle = not stabilizer.current and not stabilizer.awaiting_confirmation
            if not decision.read or backoff.skip(pixels, now, idle=idle):
                continue
            text, ocr_ms, rows = read_subtitle(ocr, processed, scorer, lines, region)
            backoff.on_read(bool(rows), pixels, now, idle=idle)
            self._last_read = clean(text)
            reason = filters.check(self._last_read).reason
            self._last_rejected = str(reason) if reason else ""
            for event in stabilizer.on_read(rows, time.monotonic()):
                self._on_line(event, ocr_ms)
            self._start_early(stabilizer.expected)  # after the submits: they claim early work

    def _start_early(self, expected: str | None) -> None:
        """Translate a new line from its first read on (``Stabilizer.expected``); the
        orchestrator shows the result only if the confirming read sends the same text."""
        if expected == self._early:
            return
        self._early = expected
        with self._lock:
            orchestrator = self._orchestrator
        if orchestrator is not None:
            orchestrator.prepare(expected)

    def _debug_frame(
        self, region: np.ndarray, processed: Preprocessed, change: Change
    ) -> DebugFrame:
        if region.ndim == 3 and region.shape[2] == 4:
            region = cv2.cvtColor(region, cv2.COLOR_BGRA2BGR)
        return DebugFrame(
            region=_shrink(region),
            ocr_input=_shrink(cv2.bitwise_not(processed.mask)),  # what was found as text
            text_fraction=processed.text_fraction,
            change=str(change),
            last_read=self._last_read,
            rejected=self._last_rejected,
        )

    def _on_line(self, event: LineEvent, ocr_ms: float) -> None:
        match event:
            case ScreenChanged(line_ids=line_ids):
                self._emit(SubtitleLines(line_ids))
                if not line_ids:
                    self._emit(SubtitleCleared())
            case LineConfirmed(line_id=line_id, text=text, repeat=True):
                self._emit(SubtitleRead(line_id, text, ocr_ms, repeat=True))
                self._translate_again(line_id, text)
            case LineConfirmed(line_id=line_id, text=text):
                self._submit(line_id, text, ocr_ms, upgrade=False)
            case LineUpgraded(line_id=line_id, text=text):
                self._submit(line_id, text, ocr_ms, upgrade=True)

    def _submit(self, line_id: int, text: str, ocr_ms: float, *, upgrade: bool) -> None:
        self._ocr_ms[line_id] = ocr_ms
        self._emit(SubtitleRead(line_id, text, ocr_ms, upgrade=upgrade))
        with self._lock:
            orchestrator = self._orchestrator
            if orchestrator is None:
                self._waiting = (line_id, text)  # the model is still warming up: newest wins
        if orchestrator is not None:
            orchestrator.submit(line_id, text)

    def _translate_again(self, line_id: int, text: str) -> None:
        """A line that came back keeps its translation - when it has one. Its request may have
        failed, been dropped from a full backlog, or been read while the model warmed up: then
        it is sent now, instead of staying untranslated however long it is on screen."""
        if line_id in self._translated:
            return
        with self._lock:
            orchestrator = self._orchestrator
            if orchestrator is None:
                self._waiting = (line_id, text)
        if orchestrator is not None and not orchestrator.pending(line_id):
            orchestrator.submit(line_id, text)

    def _on_translation(self, event: TranslationEvent) -> None:
        match event:
            case LineTranslated(line_id=line_id, source=source, result=result):
                self._translated.add(line_id)
                self._check_context(result.prompt_tokens)
                self._emit(
                    SubtitleTranslated(
                        line_id=line_id,
                        source=source,
                        translation=result.text,
                        model=result.model,
                        lane=str(result.lane),
                        ocr_ms=self._ocr_ms.get(line_id, 0.0),
                        translate_ms=result.elapsed_ms,
                        cached=result.cached,
                    )
                )
            case LineFailed(error=error):
                self._emit(EngineError(_error_kind(error), str(error)))

    def _check_context(self, prompt_tokens: int | None) -> None:
        """Warn once per start when the prompt (nearly) fills ``num_ctx``: Ollama then silently
        drops its oldest messages, the examples first, and the translations get worse."""
        num_ctx = self._start.ollama.num_ctx
        if self._context_warned or prompt_tokens is None:
            return
        if prompt_tokens > num_ctx - CONTEXT_MARGIN_TOKENS:
            self._context_warned = True
            self._emit(EngineError(ErrorKind.CONTEXT_FULL, f"{prompt_tokens}/{num_ctx}"))

    def _fatal(self, kind: ErrorKind, message: str) -> None:
        self._emit(EngineError(kind, message, fatal=True))


class EmptyReadBackoff:
    """Fewer OCR reads while the region holds scenery only.

    The text mask picks up scenery (signs, foliage, lights) as glyphs, and every frame whose mask
    moved then asks for a read that finds nothing: half of all reads on the game clips. After a
    read that found nothing while no line was on screen or waiting, further reads wait up to
    ``hold_s`` - unless the mask grows by more than ``growth`` since that read (a subtitle that
    appears adds a row of glyphs). A line on screen or waiting for confirmation is never slowed
    down."""

    def __init__(self, hold_s: float = 0.5, growth: float = 0.15) -> None:
        self.hold_s = hold_s
        self.growth = growth
        self._empty_at: float | None = None  # last read that found nothing while idle
        self._empty_pixels = 0

    def skip(self, text_pixels: int, now: float, *, idle: bool) -> bool:
        """``idle``: no line on screen and none waiting (``Stabilizer.current`` /
        ``awaiting_confirmation``)."""
        return (
            idle
            and self._empty_at is not None
            and now - self._empty_at < self.hold_s
            and text_pixels <= self._empty_pixels * (1 + self.growth)
        )

    def on_read(self, found: bool, text_pixels: int, now: float, *, idle: bool) -> None:
        if idle and not found:
            self._empty_at, self._empty_pixels = now, text_pixels
        else:
            self._empty_at = None

    def reset(self) -> None:
        self._empty_at = None


def read_subtitle(
    ocr: WinRtOcr,
    processed: Preprocessed,
    scorer: ReadScorer,
    lines: LineReader | None = None,
    region: np.ndarray | None = None,
) -> tuple[str, float, list[Row]]:
    """OCR the subtitle; ``(the read, total OCR ms, its rows top to bottom)`` - the rows are the
    subtitle lines the stabilizer tracks one by one.

    With a PP-OCR ``lines`` reader and the raw ``region`` its read is used alone (it read 94 % of
    the evaluation frames exactly, Windows' best of two 77 %; letting the scorer pick between
    them lost 2-5 points). When the mask gives it no segment to read (a line of two glyphs), or
    without it, Windows OCR reads every input of ``processed`` and the cleanest read is kept:
    every input is read even when the first finds nothing (on short, sparse lines over bright
    ground the grey image often reads nothing and the mask image reads the line); a row of the
    cleanest read is replaced by the same row of the other input where that one reads cleaner."""
    if lines is not None and region is not None:
        read = lines.recognize(region, processed.mask)
        if read is not None:
            return read.text, read.elapsed_ms, rows_of(read, processed.mask.shape[1])
    images = processed.ocr_inputs
    reads = [ocr.recognize(image) for image in images]
    best = max(range(len(reads)), key=lambda i: scorer.score(reads[i].text))  # first on a tie
    others = [line for i, read in enumerate(reads) if i != best for line in read.lines]
    lines = [_cleanest_row(line, others, scorer) for line in reads[best].lines]
    rows = rows_of(OcrResult(tuple(lines), 0.0), images[best].shape[1])
    return reads[best].text, sum(read.elapsed_ms for read in reads), rows


# A row of another input with keys this close (rapidfuzz ratio) at the same height is the same row.
SAME_ROW_SIMILARITY = 70.0


def _cleanest_row(line: OcrLine, others: list[OcrLine], scorer: ReadScorer) -> OcrLine:
    """``line`` or the same row read from another input (same height, similar text), whichever
    scores higher; ``line`` on a tie. All inputs share one size, so their boxes compare."""

    def same(other: OcrLine) -> bool:
        a, b = line.box, other.box
        return (
            b.y <= a.y + a.height / 2 < b.y + b.height
            and a.y <= b.y + b.height / 2 < a.y + a.height
            and fuzz.ratio(key(line.text), key(other.text)) >= SAME_ROW_SIMILARITY
        )

    return max([line, *filter(same, others)], key=lambda row: scorer.score(row.text))


def rows_of(read: OcrResult, width: int) -> list[Row]:
    """The read's rows, top to bottom, with their horizontal centres (0-1 of ``width``)."""
    lines = sorted(read.lines, key=lambda line: line.box.y)
    return [Row(line.text, (line.box.x + line.box.width / 2) / width) for line in lines]


def _close(backend: TranslationBackend | None) -> None:
    """Release a backend's connections (each Start makes a new one)."""
    close = getattr(backend, "close", None)
    if close is not None:
        try:
            close()
        except Exception:
            log.exception("Closing the translation backend failed")


def _shrink(image: np.ndarray) -> np.ndarray:
    """A small, contiguous copy for the Inspector (it crosses the process boundary)."""
    if image.shape[1] <= DEBUG_MAX_WIDTH:
        return np.ascontiguousarray(image)
    scale = DEBUG_MAX_WIDTH / image.shape[1]
    return cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
