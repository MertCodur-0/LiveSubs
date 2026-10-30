"""Schedules translations: every subtitle line, in reading order.

GPU lane: one request in flight, the others wait in a short FIFO queue. A new line does **not**
cancel the line being translated: that used to drop exactly the short replies ("Yeah.", "No.")
which stay on screen less long than a translation takes. To keep the delay bounded, at most
``max_backlog`` lines wait; beyond that the oldest waiting line is dropped. An upgraded reading
of a line (same ``line_id``) replaces that line's waiting job, or cancels it while in flight.

Cache hits go through the same queue (they are answered at once when their turn comes), so
results are always published in reading order and history follows the dialogue. After
``history_timeout_s`` without a new line the history is dropped: the next line most likely
belongs to another scene, and the old lines would only mislead the model.

The CPU quality lane (optional, step 13) will plug in after the GPU result.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from livesubs.engine.translate import prompt
from livesubs.engine.translate.base import (
    CancelToken,
    ContextLine,
    TranslationBackend,
    TranslationCancelledError,
    TranslationError,
    TranslationRequest,
    TranslationResult,
)
from livesubs.engine.translate.cache import TranslationCache

log = logging.getLogger(__name__)

# Lines waiting behind the one in flight. At ~1.3 s per line, 3 waiting lines are ~4 s behind.
MAX_BACKLOG = 3
# Seconds without a new line after which the dialogue context is forgotten.
HISTORY_TIMEOUT_S = 60.0


@dataclass(frozen=True, slots=True)
class LineTranslated:
    line_id: int
    source: str
    result: TranslationResult


@dataclass(frozen=True, slots=True)
class LineFailed:
    line_id: int
    source: str
    error: TranslationError


TranslationEvent = LineTranslated | LineFailed


@dataclass(frozen=True, slots=True)
class _Job:
    line_id: int
    text: str


class TranslationOrchestrator:
    def __init__(
        self,
        gpu: TranslationBackend,
        cache: TranslationCache,
        on_event: Callable[[TranslationEvent], None],
        *,
        profile_id: str = "",
        history_size: int = 3,
        game_context: str = "",
        glossary: Mapping[str, str] | None = None,
        examples: Mapping[str, str] | None = None,
        max_backlog: int = MAX_BACKLOG,
        history_timeout_s: float = HISTORY_TIMEOUT_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._gpu = gpu
        self._cache = cache
        self._on_event = on_event
        self._game_context = game_context
        self._glossary = dict(glossary or {})
        self._examples = tuple((examples or {}).items())
        # Cache scope: the profile plus a digest of its translation settings.
        digest = prompt.fingerprint(game_context, self._glossary, self._examples)
        self._cache_scope = f"{profile_id}#{digest}"
        self._history: deque[tuple[int, ContextLine]] = deque(maxlen=max(history_size, 1))
        self._history_enabled = history_size > 0
        self._history_timeout_s = history_timeout_s
        self._clock = clock
        self._last_submit: float | None = None
        self._max_backlog = max(max_backlog, 1)

        self._cond = threading.Condition()
        self._pending: deque[_Job] = deque()
        self._in_flight: tuple[int, CancelToken] | None = None  # line_id, cancel
        self._closed = False
        self._worker = threading.Thread(target=self._run, name="translate-gpu", daemon=True)
        self._worker.start()

    def submit(self, line_id: int, text: str) -> None:
        """Queue ``text`` for ``line_id``; reuse a line_id for an upgraded reading."""
        with self._cond:
            now = self._clock()
            if self._last_submit is not None and now - self._last_submit > self._history_timeout_s:
                self._history.clear()  # a long silence: most likely another scene
            self._last_submit = now
            job = _Job(line_id, text)
            # An upgrade supersedes its own line's older reading, wherever that one is.
            self._pending = deque(j for j in self._pending if j.line_id != line_id)
            if self._in_flight is not None and self._in_flight[0] == line_id:
                self._in_flight[1].cancel()
            self._pending.append(job)
            while len(self._pending) > self._max_backlog:
                dropped = self._pending.popleft()
                log.info("Translation backlog full, skipping %r", dropped.text)
            self._cond.notify()

    def clear_history(self) -> None:
        """Forget the dialogue context (profile switch, scene change)."""
        with self._cond:
            self._history.clear()

    def close(self, timeout: float = 2.0) -> None:
        with self._cond:
            self._closed = True
            self._pending.clear()
            if self._in_flight is not None:
                self._in_flight[1].cancel()
            self._cond.notify()
        self._worker.join(timeout)

    def _run(self) -> None:
        while True:
            with self._cond:
                while not self._pending and not self._closed:
                    self._cond.wait()
                if self._closed:
                    return
                job = self._pending.popleft()
                cancel = CancelToken()
                self._in_flight = (job.line_id, cancel)
                request = self._request(job)
            try:
                result = self._translate(request, cancel)
            except TranslationCancelledError:
                continue
            except TranslationError as exc:
                log.warning("Translation failed for %r: %s", job.text, exc)
                self._emit(LineFailed(job.line_id, job.text, exc))
                continue
            finally:
                with self._cond:
                    if self._in_flight is not None and self._in_flight[1] is cancel:
                        self._in_flight = None
            if not cancel.cancelled:  # superseded by an upgrade while finishing
                self._publish(job, result)

    def _translate(self, request: TranslationRequest, cancel: CancelToken) -> TranslationResult:
        cached = self._cache.get(self._gpu.model, self._cache_scope, request.text)
        if cached is not None:
            return TranslationResult(cached, self._gpu.model, self._gpu.lane, 0.0, cached=True)
        result = self._gpu.translate(request, cancel)
        self._cache.put(self._gpu.model, self._cache_scope, request.text, result.text)
        return result

    def _request(self, job: _Job) -> TranslationRequest:
        # An upgraded reading must not see its own earlier version as context.
        history = [line for line_id, line in self._history if line_id != job.line_id]
        return TranslationRequest(
            text=job.text,
            history=history if self._history_enabled else [],
            game_context=self._game_context,
            glossary=self._glossary,
            examples=self._examples,
        )

    def _publish(self, job: _Job, result: TranslationResult) -> None:
        with self._cond:
            entry = (job.line_id, ContextLine(job.text, result.text))
            if self._history and self._history[-1][0] == job.line_id:
                self._history[-1] = entry
            else:
                self._history.append(entry)
        self._emit(LineTranslated(job.line_id, job.text, result))

    def _emit(self, event: TranslationEvent) -> None:
        try:
            self._on_event(event)
        except Exception:
            log.exception("Translation event handler failed")
