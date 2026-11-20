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

The history **grows and is cut in one go** (``history_size`` lines up to twice that minus one,
then back to ``history_size``) instead of sliding by one line per line: llama.cpp cannot rewind
the prompt cache of a sliding-window model (Gemma), so a prompt that only *continues* the last
one (the same lines plus the new one) costs the new line alone, while one whose first history
line changed is processed again from the examples on. With gemma4:26b that is ~280 ms of prompt
time instead of ~530 ms on the lines between two cuts.

**Early start** (``prepare``): the reader confirms a new line with a second read, ~80 ms after
the first. While the lane is idle, the text of the first read is translated at once, as a job
that belongs to no line yet. ``submit`` of exactly that text takes the job over (in flight or
finished); anything else cancels it, and its result is never published, cached or used as
context. So only confirmed lines reach the screen, ~80 ms earlier.

A wrong guess must be cheap for this to pay: Ollama finishes a request's prompt batch before it
notices that the client left. With a small model on the GPU that is 20-40 ms; with a MoE model
whose experts sit in RAM (gemma4:26b) ~650 ms, which the next line then waits. So the lane times
how long a dropped early request keeps the backend busy, and the first one over
``early_cancel_limit_s`` turns early starts off for the rest of the run.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field

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
# A finished early translation nobody claimed is dropped after this long.
PREPARED_TTL_S = 2.0
# A dropped early request that keeps the backend busy longer than this ends early starts: at a
# 70 % hit rate (measured on the game clips) a wrong guess may cost ~190 ms to break even.
EARLY_CANCEL_LIMIT_S = 0.15
# Idle seconds after which the model's keep-alive is renewed; below the shortest keep-alive
# choice (5 min), so the model stays loaded as long as translation runs. Ollama's own timer
# still unloads it once translation stops.
KEEP_WARM_S = 240.0


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


@dataclass(slots=True, eq=False)
class _Job:
    line_id: int | None  # None: started early (``prepare``), no line owns it yet
    text: str
    result: TranslationResult | None = None  # translated early: only publish it
    cancel: CancelToken = field(default_factory=CancelToken)
    dropped_at: float | None = None  # early work cancelled while in flight: when


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
        keep_warm_s: float = KEEP_WARM_S,
        early_cancel_limit_s: float = EARLY_CANCEL_LIMIT_S,
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
        self._history: deque[tuple[int, ContextLine]] = deque()
        self._history_size = max(history_size, 1)
        self._history_enabled = history_size > 0
        self._history_timeout_s = history_timeout_s
        self._clock = clock
        self._last_submit: float | None = None
        self._max_backlog = max(max_backlog, 1)
        self._keep_warm_s = keep_warm_s

        self._cond = threading.Condition()
        self._pending: deque[_Job] = deque()
        self._in_flight: _Job | None = None
        self._prepared: tuple[_Job, float] | None = None  # finished early, unclaimed; when
        self._early_enabled = True
        self._early_cancel_limit_s = early_cancel_limit_s
        self._closed = False
        self._worker = threading.Thread(target=self._run, name="translate-gpu", daemon=True)
        self._worker.start()

    def submit(self, line_id: int, text: str) -> None:
        """Queue ``text`` for ``line_id``; reuse a line_id for an upgraded reading."""
        with self._cond:
            now = self._clock()
            self._forget_old_history(now)
            self._last_submit = now
            flying = self._in_flight
            if flying is not None and flying.line_id is None and flying.text == text:
                flying.line_id = line_id  # started early: it is this line's translation now
                return
            job = _Job(line_id, text)
            if self._prepared is not None:
                early, finished = self._prepared
                if early.text == text and now - finished <= PREPARED_TTL_S:
                    job.result = early.result
            self._drop_early()
            # An upgrade supersedes its own line's older reading, wherever that one is.
            self._pending = deque(j for j in self._pending if j.line_id != line_id)
            if flying is not None and flying.line_id == line_id:
                flying.cancel.cancel()
            self._pending.append(job)
            while len(self._pending) > self._max_backlog:
                dropped = self._pending.popleft()
                log.info("Translation backlog full, skipping %r", dropped.text)
            self._cond.notify()

    def pending(self, line_id: int) -> bool:
        """A translation of ``line_id`` is waiting or in flight."""
        with self._cond:
            flying = self._in_flight
            return (flying is not None and flying.line_id == line_id) or any(
                job.line_id == line_id for job in self._pending
            )

    def withdraw(self, line_ids: Iterable[int]) -> None:
        """Drop the waiting (not the in-flight) translations of ``line_ids``: more important
        lines came (named dialogue over background voices)."""
        ids = set(line_ids)
        if not ids:
            return
        with self._cond:
            self._pending = deque(j for j in self._pending if j.line_id not in ids)

    def prepare(self, text: str | None) -> None:
        """Start translating ``text`` before its line is confirmed, if the lane is idle; a
        ``submit`` of the same text takes the translation over. ``None`` (or another text) drops
        an early translation no line has claimed."""
        with self._cond:
            if self._closed:
                return
            flying = self._in_flight
            if text is not None and (
                (flying is not None and flying.line_id is None and flying.text == text)
                or (self._prepared is not None and self._prepared[0].text == text)
                or any(j.line_id is None and j.text == text for j in self._pending)
            ):
                return  # already on its way
            self._drop_early()
            if text is None or not self._early_enabled:
                return
            if self._in_flight is not None or self._pending:
                return  # confirmed lines come first: no early start behind them
            self._forget_old_history(self._clock())
            self._pending.append(_Job(None, text))
            self._cond.notify()

    def _drop_early(self) -> None:
        """Forget early work no line has claimed (called with the lock held)."""
        self._prepared = None
        self._pending = deque(j for j in self._pending if j.line_id is not None)
        if self._in_flight is not None and self._in_flight.line_id is None:
            self._in_flight.dropped_at = time.monotonic()
            self._in_flight.cancel.cancel()
            self._in_flight = None  # the worker drops its result

    def _forget_old_history(self, now: float) -> None:
        if self._last_submit is not None and now - self._last_submit > self._history_timeout_s:
            self._history.clear()  # a long silence: most likely another scene

    def clear_history(self) -> None:
        """Forget the dialogue context (profile switch, scene change)."""
        with self._cond:
            self._history.clear()

    def close(self, timeout: float = 2.0) -> None:
        with self._cond:
            self._closed = True
            self._pending.clear()
            if self._in_flight is not None:
                self._in_flight.cancel.cancel()
            self._cond.notify()
        self._worker.join(timeout)

    def _run(self) -> None:
        while True:
            with self._cond:
                idle = False
                while not self._pending and not self._closed and not idle:
                    idle = not self._cond.wait(self._keep_warm_s)
                if self._closed:
                    return
                if not self._pending:
                    job = None
                else:
                    job = self._pending.popleft()
                    self._in_flight = job
                    request = self._request(job)
            if job is None:
                self._keep_warm()
                continue
            result, error = job.result, None
            try:
                if result is None:
                    result = self._translate(request, job.cancel)
            except TranslationCancelledError:
                pass
            except TranslationError as exc:
                error = exc
            except Exception as exc:  # a reply nobody expected must not end the lane for good
                log.exception("Translating %r failed unexpectedly", job.text)
                error = TranslationError(f"{type(exc).__name__}: {exc}")
            with self._cond:
                if self._in_flight is job:
                    self._in_flight = None
                if job.dropped_at is not None and self._early_enabled:
                    busy = time.monotonic() - job.dropped_at
                    if busy > self._early_cancel_limit_s:
                        self._early_enabled = False
                        log.info("Early starts off: a dropped request took %.0f ms", busy * 1000)
                if job.cancel.cancelled or (result is None and error is None):
                    continue  # superseded by an upgrade, or early work that was dropped
                line_id = job.line_id
                if line_id is None:  # nobody claimed it yet: keep the result for a moment
                    if result is not None:
                        job.result = result
                        self._prepared = (job, self._clock())
                    continue
            if error is not None:
                log.warning("Translation failed for %r: %s", job.text, error)
                self._emit(LineFailed(line_id, job.text, error))
                continue
            if not result.cached:
                self._cache.put(self._gpu.model, self._cache_scope, job.text, result.text)
            self._publish(job, result)

    def _keep_warm(self) -> None:
        """Idle for ``keep_warm_s``: renew the model's keep-alive (a request without a prompt),
        so a long stretch without subtitles never makes the next line wait for a reload."""
        load = getattr(self._gpu, "load", None)
        if load is None:
            return
        try:
            load()
        except TranslationError as exc:
            log.info("Keeping the model loaded failed: %s", exc)

    def _translate(self, request: TranslationRequest, cancel: CancelToken) -> TranslationResult:
        cached = self._cache.get(self._gpu.model, self._cache_scope, request.text)
        if cached is not None:
            return TranslationResult(cached, self._gpu.model, self._gpu.lane, 0.0, cached=True)
        return self._gpu.translate(request, cancel)  # cached once a line owns the result

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
                if len(self._history) > 2 * self._history_size - 1:
                    while len(self._history) > self._history_size:
                        self._history.popleft()  # one cut, then the prompt only grows again
        self._emit(LineTranslated(job.line_id, job.text, result))

    def _emit(self, event: TranslationEvent) -> None:
        try:
            self._on_event(event)
        except Exception:
            log.exception("Translation event handler failed")
