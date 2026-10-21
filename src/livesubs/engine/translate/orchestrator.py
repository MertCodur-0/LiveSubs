"""Schedules translations so the newest subtitle always wins.

GPU lane: one request in flight. A new line cancels the request in flight (its subtitle is
already gone) and takes the single pending slot; lines that came and went in between are never
translated. Cached lines are answered at once on the caller's thread. A result is published only
if its line is still the newest, so a late answer can never overwrite a newer subtitle.

The CPU quality lane (optional, step 13) will plug in after the GPU result.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass

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
    seq: int
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
    ) -> None:
        self._gpu = gpu
        self._cache = cache
        self._on_event = on_event
        self._profile_id = profile_id
        self._game_context = game_context
        self._glossary = dict(glossary or {})
        self._history: deque[tuple[int, ContextLine]] = deque(maxlen=max(history_size, 1))
        self._history_enabled = history_size > 0

        self._cond = threading.Condition()
        self._seq = 0
        self._pending: _Job | None = None
        self._in_flight: CancelToken | None = None
        self._closed = False
        self._worker = threading.Thread(target=self._run, name="translate-gpu", daemon=True)
        self._worker.start()

    def submit(self, line_id: int, text: str) -> None:
        """Translate ``text`` for ``line_id``; reuse a line_id for an upgraded reading."""
        with self._cond:
            self._seq += 1
            job = _Job(self._seq, line_id, text)
            if self._in_flight is not None:
                self._in_flight.cancel()
            self._pending = None
            cached = self._cache.get(self._gpu.model, self._profile_id, text)
            if cached is None:
                self._pending = job
                self._cond.notify()
                return
        result = TranslationResult(cached, self._gpu.model, self._gpu.lane, 0.0, cached=True)
        self._publish(job, result)

    def clear_history(self) -> None:
        """Forget the dialogue context (profile switch, scene change)."""
        with self._cond:
            self._history.clear()

    def close(self, timeout: float = 2.0) -> None:
        with self._cond:
            self._closed = True
            self._pending = None
            if self._in_flight is not None:
                self._in_flight.cancel()
            self._cond.notify()
        self._worker.join(timeout)

    def _run(self) -> None:
        while True:
            with self._cond:
                while self._pending is None and not self._closed:
                    self._cond.wait()
                if self._closed:
                    return
                job, self._pending = self._pending, None
                cancel = self._in_flight = CancelToken()
                request = self._request(job)
            try:
                result = self._gpu.translate(request, cancel)
            except TranslationCancelledError:
                continue
            except TranslationError as exc:
                log.warning("Translation failed for %r: %s", job.text, exc)
                self._emit(LineFailed(job.line_id, job.text, exc), job.seq)
                continue
            finally:
                with self._cond:
                    if self._in_flight is cancel:
                        self._in_flight = None
            self._cache.put(self._gpu.model, self._profile_id, job.text, result.text)
            self._publish(job, result)

    def _request(self, job: _Job) -> TranslationRequest:
        # An upgraded reading must not see its own earlier version as context.
        history = [line for line_id, line in self._history if line_id != job.line_id]
        return TranslationRequest(
            text=job.text,
            history=history if self._history_enabled else [],
            game_context=self._game_context,
            glossary=self._glossary,
        )

    def _publish(self, job: _Job, result: TranslationResult) -> None:
        with self._cond:
            if job.seq != self._seq:
                return  # a newer line arrived meanwhile
            entry = (job.line_id, ContextLine(job.text, result.text))
            if self._history and self._history[-1][0] == job.line_id:
                self._history[-1] = entry
            else:
                self._history.append(entry)
        self._emit(LineTranslated(job.line_id, job.text, result), job.seq)

    def _emit(self, event: TranslationEvent, seq: int) -> None:
        with self._cond:
            if seq != self._seq:
                return
        try:
            self._on_event(event)
        except Exception:
            log.exception("Translation event handler failed")
