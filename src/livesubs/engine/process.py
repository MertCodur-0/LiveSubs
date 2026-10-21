"""The engine process: runs pipelines on command and reports events.

Capture, OCR and translation live in their own process so the UI's event loop and the GIL can
never slow them down. The UI talks to it only through two multiprocessing queues.
"""

from __future__ import annotations

import logging
import logging.handlers
import multiprocessing
import queue
from multiprocessing.queues import Queue
from pathlib import Path

from livesubs.core import paths
from livesubs.core.messages import (
    Command,
    EngineError,
    EngineState,
    ErrorKind,
    Event,
    SetRegion,
    Shutdown,
    StartPipeline,
    StateChanged,
    StopPipeline,
)
from livesubs.engine.pipeline import Pipeline

log = logging.getLogger(__name__)


def _setup_logging() -> None:
    log_dir = paths.log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        log_dir / "engine.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)


def engine_main(commands: Queue, events: Queue) -> None:
    """Entry point of the engine process."""
    _setup_logging()
    pipeline: Pipeline | None = None
    events.put(StateChanged(EngineState.IDLE))
    while True:
        command: Command = commands.get()
        try:
            match command:
                case StartPipeline():
                    if pipeline is not None:
                        pipeline.stop()
                    pipeline = Pipeline(command, events.put, cache_path=_cache_path())
                    pipeline.start()
                case StopPipeline():
                    if pipeline is not None:
                        pipeline.stop()
                        pipeline = None
                case SetRegion(region=region):
                    if pipeline is not None:
                        pipeline.set_region(region)
                case Shutdown():
                    if pipeline is not None:
                        pipeline.stop()
                    return
        except Exception as exc:
            log.exception("Engine command failed: %r", command)
            events.put(EngineError(ErrorKind.INTERNAL, f"{type(exc).__name__}: {exc}"))


def _cache_path() -> Path:
    # Resolved when a pipeline starts, so LIVESUBS_HOME set by tests applies.
    return paths.cache_dir() / "translations.sqlite3"


class EngineProcess:
    """UI-side handle: starts the engine process and exchanges messages with it."""

    def __init__(self) -> None:
        context = multiprocessing.get_context("spawn")
        self._commands: Queue = context.Queue()
        self._events: Queue = context.Queue()
        self._process = context.Process(
            target=engine_main,
            args=(self._commands, self._events),
            name="livesubs-engine",
            daemon=True,
        )

    def start(self) -> None:
        self._process.start()

    @property
    def alive(self) -> bool:
        return self._process.is_alive()

    def send(self, command: Command) -> None:
        self._commands.put(command)

    def poll(self, max_events: int = 100) -> list[Event]:
        """Events received so far, without blocking."""
        received: list[Event] = []
        while len(received) < max_events:
            try:
                received.append(self._events.get_nowait())
            except queue.Empty:
                break
        return received

    def wait_event(self, timeout: float) -> Event | None:
        try:
            return self._events.get(timeout=timeout)
        except queue.Empty:
            return None

    def shutdown(self, timeout: float = 5.0) -> None:
        if self._process.is_alive():
            self.send(Shutdown())
            self._process.join(timeout)
        if self._process.is_alive():
            self._process.terminate()
            self._process.join(1.0)
