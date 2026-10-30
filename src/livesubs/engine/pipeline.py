"""One running chain: source -> region -> preprocess -> change -> stabilizer -> OCR -> translation.

Runs on its own thread inside the engine process and reports everything through ``emit``
(``core.messages`` events). The loop runs at the profile's fps, and every ``fast_reread_s`` while
a new line waits for its confirming read. Frames are only processed when they are new, or when
the stabilizer wants another read. OCR runs inline (10-20 ms); translation runs on the
orchestrator's thread, so a slow model never delays reading the next subtitle. The model warms up on
a side thread while reading already runs; the newest line read meanwhile is translated once the
model is ready. A subtitle that continues the previous one's sentence (``text.sentences``) is sent
as an upgrade of that line: the whole sentence is translated and replaces the first piece's
translation.

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
    SubtitleRead,
    SubtitleTranslated,
)
from livesubs.core.profiles import Source, VisionSettings
from livesubs.engine.capture import FrameSource, SourceLostError, SourceUnavailableError
from livesubs.engine.capture import open_source as default_open_source
from livesubs.engine.ocr.language import english_ocr_language
from livesubs.engine.ocr.winrt_ocr import WinRtOcr
from livesubs.engine.text import filters, sentences
from livesubs.engine.text.normalize import clean
from livesubs.engine.text.stabilizer import (
    Event as LineEvent,
)
from livesubs.engine.text.stabilizer import (
    LineCleared,
    LineConfirmed,
    LineUpgraded,
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

log = logging.getLogger(__name__)

FIRST_FRAME_TIMEOUT_S = 5.0
CAPTURE_MAX_FPS = 30.0
# Room a translation needs after the prompt; a prompt closer to num_ctx than this gets a warning
# (a cut prompt reports num_ctx - 1 tokens).
CONTEXT_MARGIN_TOKENS = 128
# Longest gap between two pieces of one sentence: from the last frame that showed the previous
# piece to the next piece's confirmation.
MERGE_GAP_S = 1.5

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
        self._line_id = 0
        self._ocr_ms: dict[int, float] = {}
        # Pieces of the sentence the current line_id stands for, and when a line was last seen.
        self._pieces: list[str] = []
        self._line_seen = 0.0
        # Set by the warm-up thread once the model answers; lines read before that wait.
        self._lock = threading.Lock()
        self._orchestrator: TranslationOrchestrator | None = None
        self._waiting: tuple[int, str] | None = None
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
            self._loop(source, ocr)
        except SourceLostError as exc:
            self._fatal(ErrorKind.SOURCE_LOST, str(exc))
        except Exception as exc:
            log.exception("Pipeline crashed")
            self._fatal(ErrorKind.INTERNAL, f"{type(exc).__name__}: {exc}")
        finally:
            self._stop.set()  # also tells a warm-up still in progress to give up
            with self._lock:
                orchestrator, self._orchestrator = self._orchestrator, None
            if orchestrator is not None:
                orchestrator.close()
            if source is not None:
                source.close()
            if cache is not None:
                cache.close()
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
        backend = self._backend_factory(self._start)
        self._emit(StateChanged(EngineState.WARMING_UP, backend.model))
        warm_up = getattr(backend, "warm_up", None)
        try:
            if warm_up is not None:
                warm_up()
        except TranslationError as exc:
            # Keep reading subtitles; the UI shows English only and explains why.
            self._emit(EngineError(_error_kind(exc), str(exc)))
            if not self._stop.is_set():
                self._emit(StateChanged(EngineState.RUNNING))
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
                return
            self._orchestrator = orchestrator
            waiting, self._waiting = self._waiting, None
        self._emit(StateChanged(EngineState.RUNNING))
        if waiting is not None:
            orchestrator.submit(*waiting)

    def _loop(self, source: FrameSource, ocr: WinRtOcr) -> None:
        profile = self._start.profile
        tick = 1.0 / profile.capture.fps
        detector, stabilizer = ChangeDetector(), Stabilizer()
        last_timestamp = -1.0
        paused = False
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
                event = stabilizer.update(Change.CLEARED, 0, time.monotonic()).event
                if event is not None:
                    self._on_line(event, 0.0)
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
            decision = stabilizer.update(change, cv2.countNonZero(processed.mask), now)
            if stabilizer.current is not None:
                self._line_seen = now
            if decision.event is not None:
                self._on_line(decision.event, 0.0)
            if not decision.read:
                continue
            read = ocr.recognize(processed.ocr_image)
            self._last_read = clean(read.text)
            reason = filters.check(self._last_read).reason
            self._last_rejected = str(reason) if reason else ""
            event = stabilizer.on_read(read.text, time.monotonic())
            if event is not None:
                self._on_line(event, read.elapsed_ms)

    def _debug_frame(
        self, region: np.ndarray, processed: Preprocessed, change: Change
    ) -> DebugFrame:
        if region.ndim == 3 and region.shape[2] == 4:
            region = cv2.cvtColor(region, cv2.COLOR_BGRA2BGR)
        return DebugFrame(
            region=_shrink(region),
            ocr_input=_shrink(processed.ocr_image),
            text_fraction=processed.text_fraction,
            change=str(change),
            last_read=self._last_read,
            rejected=self._last_rejected,
        )

    def _on_line(self, event: LineEvent, ocr_ms: float) -> None:
        match event:
            case LineCleared():
                self._emit(SubtitleCleared())
            case LineConfirmed(text=text, repeat=True):
                self._emit(SubtitleRead(self._line_id, text, ocr_ms, repeat=True))
            case LineConfirmed(text=text) if self._continues(text):
                self._pieces.append(text)
                self._submit(self._line_id, " ".join(self._pieces), ocr_ms, upgrade=True)
            case LineConfirmed(text=text):
                self._line_id += 1
                self._pieces = [text]
                self._submit(self._line_id, text, ocr_ms, upgrade=False)
            case LineUpgraded(text=text):
                self._pieces[-1:] = [text]
                self._submit(self._line_id, " ".join(self._pieces), ocr_ms, upgrade=True)

    def _continues(self, text: str) -> bool:
        """``text`` is the next piece of the sentence on screen just before it."""
        return (
            self._start.profile.translation.merge_split_sentences
            and 0 < len(self._pieces) < sentences.MAX_PIECES
            and time.monotonic() - self._line_seen <= MERGE_GAP_S
            and sentences.continues(self._pieces[-1], text)
        )

    def _submit(self, line_id: int, text: str, ocr_ms: float, *, upgrade: bool) -> None:
        self._ocr_ms[line_id] = ocr_ms
        self._emit(SubtitleRead(line_id, text, ocr_ms, upgrade=upgrade))
        with self._lock:
            orchestrator = self._orchestrator
            if orchestrator is None:
                self._waiting = (line_id, text)  # the model is still warming up: newest wins
        if orchestrator is not None:
            orchestrator.submit(line_id, text)

    def _on_translation(self, event: TranslationEvent) -> None:
        match event:
            case LineTranslated(line_id=line_id, source=source, result=result):
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


def _shrink(image: np.ndarray) -> np.ndarray:
    """A small, contiguous copy for the Inspector (it crosses the process boundary)."""
    if image.shape[1] <= DEBUG_MAX_WIDTH:
        return np.ascontiguousarray(image)
    scale = DEBUG_MAX_WIDTH / image.shape[1]
    return cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
