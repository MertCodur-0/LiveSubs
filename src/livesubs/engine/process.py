"""The engine process: runs pipelines on command and reports events.

Capture, OCR and translation live in their own process so the UI's event loop and the GIL can
never slow them down. The UI talks to it only through two multiprocessing queues.
"""

from __future__ import annotations

import logging
import os
from multiprocessing.queues import Queue

from livesubs.core import paths
from livesubs.core.child_process import ChildProcess, exit_with_parent
from livesubs.core.logs import setup_file_logging
from livesubs.core.messages import (
    Command,
    EngineError,
    EngineState,
    ErrorKind,
    Event,
    SetDebug,
    SetPaused,
    SetRegion,
    SetVision,
    Shutdown,
    StartPipeline,
    StateChanged,
    StopPipeline,
)
from livesubs.engine.pipeline import Pipeline

log = logging.getLogger(__name__)


def engine_main(commands: Queue, events: Queue) -> None:
    """Entry point of the engine process."""
    setup_file_logging("engine.log")
    exit_with_parent()
    log.info("Engine started (pid %d)", os.getpid())
    pipeline: Pipeline | None = None
    events.put(StateChanged(EngineState.IDLE))
    while True:
        command: Command = commands.get()
        log.info("Command: %s", type(command).__name__)
        try:
            match command:
                case StartPipeline():
                    if pipeline is not None:
                        pipeline.stop()
                    # Resolved per start, so LIVESUBS_HOME set by tests applies.
                    cache_path = paths.translation_cache_file()
                    pipeline = Pipeline(command, events.put, cache_path=cache_path)
                    pipeline.start()
                case StopPipeline():
                    if pipeline is not None:
                        pipeline.stop()
                        pipeline = None
                case SetRegion(region=region):
                    if pipeline is not None:
                        pipeline.set_region(region)
                case SetPaused(paused=paused):
                    if pipeline is not None:
                        pipeline.set_paused(paused)
                case SetDebug(enabled=enabled):
                    if pipeline is not None:
                        pipeline.set_debug(enabled)
                case SetVision(vision=vision):
                    if pipeline is not None:
                        pipeline.set_vision(vision)
                case Shutdown():
                    if pipeline is not None:
                        pipeline.stop()
                    return
        except Exception as exc:
            log.exception("Engine command failed: %r", command)
            events.put(EngineError(ErrorKind.INTERNAL, f"{type(exc).__name__}: {exc}"))


class EngineProcess(ChildProcess[Command, Event]):
    """UI-side handle: starts the engine process and exchanges messages with it."""

    def __init__(self) -> None:
        super().__init__(engine_main, "livesubs-engine")
