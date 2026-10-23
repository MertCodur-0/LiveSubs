"""The real engine process: spawn, run a monitor pipeline without Ollama, stop, shut down."""

import time
from pathlib import Path

import pytest

from livesubs.core import paths
from livesubs.core.config import OllamaSettings
from livesubs.core.messages import (
    EngineError,
    EngineState,
    ErrorKind,
    Event,
    FrameInfo,
    StartPipeline,
    StateChanged,
    StopPipeline,
)
from livesubs.core.profiles import MonitorSource, Profile
from livesubs.engine.ocr.language import english_ocr_language
from livesubs.engine.process import EngineProcess
from livesubs.platform import win32

pytestmark = [
    pytest.mark.capture,
    pytest.mark.ocr,
    pytest.mark.skipif(english_ocr_language() is None, reason="English OCR not installed"),
]


def collect_until(
    engine: EngineProcess, done: type, state: EngineState | None, timeout: float
) -> list[Event]:
    events: list[Event] = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        event = engine.wait_event(0.2)
        if event is None:
            continue
        events.append(event)
        if isinstance(event, done) and (state is None or event.state == state):
            return events
    pytest.fail(f"timed out; got {events}")


def test_engine_process_lifecycle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(paths.HOME_ENV_VAR, str(tmp_path))  # inherited by the spawned process
    monitor = next(m for m in win32.list_monitors() if m.is_primary)
    profile = Profile(name="desk", source=MonitorSource(device_name=monitor.device_name))
    nowhere = OllamaSettings(url="http://127.0.0.1:9")  # nothing listens on the discard port

    engine = EngineProcess()
    engine.start()
    try:
        events = collect_until(engine, StateChanged, EngineState.IDLE, 30)
        engine.send(StartPipeline(profile=profile, ollama=nowhere))
        events = collect_until(engine, StateChanged, EngineState.RUNNING, 30)

        info = next(e for e in events if isinstance(e, FrameInfo))
        assert (info.width, info.height) == (monitor.rect.width, monitor.rect.height)
        errors = [e for e in events if isinstance(e, EngineError)]
        assert [(e.kind, e.fatal) for e in errors] == [(ErrorKind.OLLAMA_UNAVAILABLE, False)]

        engine.send(StopPipeline())
        collect_until(engine, StateChanged, EngineState.STOPPED, 10)
    finally:
        engine.shutdown()
    assert not engine.alive
    assert (tmp_path / "logs" / "engine.log").is_file()


def test_restart_after_a_crash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A native crash ends the process without a word; ``restart`` gives a working engine."""
    monkeypatch.setenv(paths.HOME_ENV_VAR, str(tmp_path))
    engine = EngineProcess()
    engine.start()
    try:
        collect_until(engine, StateChanged, EngineState.IDLE, 30)
        engine._process.kill()  # what an access violation in windows-capture looks like
        engine._process.join(5)
        assert not engine.alive
        engine.restart()
        assert engine.alive
        collect_until(engine, StateChanged, EngineState.IDLE, 30)
    finally:
        engine.shutdown()
    assert not engine.alive
