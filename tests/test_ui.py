from pathlib import Path

import pytest
from pytestqt.qtbot import QtBot

from livesubs.core.config import AppConfig
from livesubs.core.geometry import NormRect, Rect
from livesubs.core.messages import (
    Command,
    EngineError,
    EngineState,
    ErrorKind,
    Event,
    FrameInfo,
    SetRegion,
    StartPipeline,
    StateChanged,
    StopPipeline,
    SubtitleCleared,
    SubtitleRead,
    SubtitleTranslated,
)
from livesubs.core.profiles import ProfileStore
from livesubs.ui.main_window import PROFILE_ID, MainWindow, overlay_area
from livesubs.ui.overlay import Overlay, wrap
from livesubs.ui.screens import ScreenMap, to_logical, to_physical

# The developer's desk: 1080p at 100 % and a 1440p laptop panel at 150 %, up and to the right.
DESK = [
    ScreenMap(Rect(0, 0, 1920, 1080), 1.0),
    ScreenMap(Rect(1920, -164, 2560, 1440), 1.5),
]


# --- screens ------------------------------------------------------------------------------------


def test_physical_to_logical_per_monitor() -> None:
    assert to_logical(Rect(100, 900, 800, 100), DESK) == Rect(100, 900, 800, 100)
    # On the 150 % panel: offsets from its corner and sizes shrink, the corner itself does not.
    assert to_logical(Rect(1920 + 300, -164 + 600, 1500, 150), DESK) == Rect(2120, 236, 1000, 100)
    assert ScreenMap(Rect(1920, -164, 2560, 1440), 1.5).logical == Rect(1920, -164, 1707, 960)


def test_logical_round_trip() -> None:
    for rect in (Rect(10, 20, 300, 40), Rect(2220, 436, 1500, 150)):
        assert to_physical(to_logical(rect, DESK), DESK) == rect


def test_rect_off_every_screen_uses_the_nearest() -> None:
    assert to_logical(Rect(5000, 100, 30, 30), DESK).x > 1920


# --- overlay ------------------------------------------------------------------------------------


def test_overlay_goes_above_the_region_or_below_when_no_room() -> None:
    screen = Rect(0, 0, 1920, 1080)
    assert overlay_area(Rect(200, 900, 1500, 120), screen) == Rect(200, 750, 1500, 144)
    assert overlay_area(Rect(200, 20, 1500, 60), screen) == Rect(200, 86, 1500, 90)


def test_wrap() -> None:
    def width(s: str) -> float:
        return len(s) * 10.0

    assert wrap("Arabaya bin hemen", width, 120) == ["Arabaya bin", "hemen"]
    assert wrap("Supercalifragilistic word", width, 50) == ["Supercalifragilistic", "word"]
    assert wrap("", width, 100) == []


def test_overlay_paints_text(qtbot: QtBot) -> None:
    overlay = Overlay()
    qtbot.addWidget(overlay)
    overlay.place(Rect(100, 100, 800, 120))
    overlay.show_text("Kıçını arabaya sok kanka. Aynasızlar geliyor!", "Get in the car!")
    assert overlay.isVisible()
    assert overlay.text.startswith("Kıçını")
    assert not overlay.grab().isNull()  # paints without errors
    overlay.clear()
    assert overlay.text == ""


def test_long_text_shrinks_to_fit(qtbot: QtBot) -> None:
    overlay = Overlay()
    qtbot.addWidget(overlay)
    overlay.place(Rect(0, 0, 400, 100))
    overlay.show_text("çok " * 60)
    font, lines = overlay._layout(380, 80)
    assert len(lines) <= overlay.settings.max_lines
    assert font.pointSizeF() < overlay.settings.font_size_pt


# --- main window --------------------------------------------------------------------------------


class FakeEngine:
    def __init__(self) -> None:
        self.sent: list[Command] = []
        self.queue: list[Event] = []
        self.started = self.stopped = False

    def start(self) -> None:
        self.started = True

    def send(self, command: Command) -> None:
        self.sent.append(command)

    def poll(self, max_events: int = 100) -> list[Event]:
        events, self.queue = self.queue, []
        return events

    def shutdown(self, timeout: float = 5.0) -> None:
        self.stopped = True


@pytest.fixture
def window(qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MainWindow:
    monkeypatch.setenv("LIVESUBS_HOME", str(tmp_path))
    monkeypatch.setattr("livesubs.ui.main_window.list_models", lambda url: None)
    win = MainWindow(FakeEngine(), config=AppConfig(), store=ProfileStore(tmp_path / "profiles"))
    qtbot.addWidget(win)
    return win


def engine_of(window: MainWindow) -> FakeEngine:
    return window._engine  # type: ignore[return-value]


def test_start_needs_a_region(window: MainWindow) -> None:
    assert engine_of(window).started
    window.toggle()
    assert engine_of(window).sent == []
    assert window.statusBar().currentMessage()


def test_start_sends_the_profile_and_saves_it(window: MainWindow, tmp_path: Path) -> None:
    window.set_region(NormRect(x=0.1, y=0.8, width=0.8, height=0.15))
    window.toggle()
    command = engine_of(window).sent[-1]
    assert isinstance(command, StartPipeline)
    assert command.profile.region == NormRect(x=0.1, y=0.8, width=0.8, height=0.15)
    assert command.profile.source is not None
    assert command.profile_id == PROFILE_ID
    assert ProfileStore(tmp_path / "profiles").load(PROFILE_ID).region == command.profile.region


def test_events_drive_the_feed_and_overlay(window: MainWindow, qtbot: QtBot) -> None:
    engine = engine_of(window)
    window.set_region(NormRect(x=0.1, y=0.8, width=0.8, height=0.15))
    engine.queue = [
        StateChanged(EngineState.STARTING),
        FrameInfo(1920, 1080, (0, 0, 1920, 1080)),
        StateChanged(EngineState.WARMING_UP, "gemma4:12b-it-qat"),
        StateChanged(EngineState.RUNNING),
        SubtitleRead(1, "Get in the car.", 12.0),
    ]
    window._poll()
    assert window.start_button.text() == "Durdur"
    assert window.feed.rowCount() == 1
    assert window.feed.item(0, 1).text() == "Get in the car."

    engine.queue = [
        SubtitleTranslated(1, "Get in the car.", "Arabaya bin.", "m", "gpu", 12.0, 640.0, False)
    ]
    window._poll()
    assert window.feed.item(0, 2).text() == "Arabaya bin."
    assert window.feed.item(0, 5).text() == "640"
    assert window._overlay.text == "Arabaya bin."

    # A new line hides the old translation until its own arrives.
    engine.queue = [SubtitleRead(2, "Who are you?", 10.0)]
    window._poll()
    assert window.feed.rowCount() == 2
    assert window._overlay.text == ""

    # The line disappears: the overlay clears after the configured delay.
    window._overlay.show_text("Sen kimsin?")
    engine.queue = [SubtitleCleared()]
    window._poll()
    assert window._overlay.text == "Sen kimsin?"
    qtbot.waitUntil(lambda: window._overlay.text == "", timeout=3000)


def test_errors_and_stop(window: MainWindow) -> None:
    engine = engine_of(window)
    engine.queue = [
        StateChanged(EngineState.RUNNING),
        EngineError(ErrorKind.OLLAMA_UNAVAILABLE, "refused"),
    ]
    window._poll()
    assert "Ollama" in window.statusBar().currentMessage()
    window.toggle()
    assert isinstance(engine.sent[-1], StopPipeline)
    engine.queue = [StateChanged(EngineState.STOPPED)]
    window._poll()
    assert window.start_button.text() == "Başlat"


def test_region_change_while_running_reaches_the_engine(window: MainWindow) -> None:
    engine = engine_of(window)
    engine.queue = [StateChanged(EngineState.RUNNING)]
    window._poll()
    window.set_region(NormRect(x=0.2, y=0.7, width=0.6, height=0.2))
    assert isinstance(engine.sent[-1], SetRegion)


def test_close_shuts_the_engine_down(window: MainWindow) -> None:
    window.close()
    assert engine_of(window).stopped
