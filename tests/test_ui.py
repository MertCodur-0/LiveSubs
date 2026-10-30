from pathlib import Path

import numpy as np
import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QGuiApplication, QKeySequence
from PySide6.QtWidgets import QApplication, QLabel, QMessageBox
from pytestqt.qtbot import QtBot

from livesubs.core import paths
from livesubs.core.config import AppConfig, PreviewSettings, load_config
from livesubs.core.geometry import NormRect, Rect
from livesubs.core.messages import (
    Command,
    DebugFrame,
    EngineError,
    EngineState,
    ErrorKind,
    Event,
    FrameInfo,
    SetDebug,
    SetPaused,
    SetRegion,
    SetVision,
    StartPipeline,
    StateChanged,
    StopPipeline,
    SubtitleCleared,
    SubtitleRead,
    SubtitleTranslated,
)
from livesubs.core.profiles import (
    MonitorSource,
    OverlayDisplay,
    OverlayPlacement,
    Profile,
    ProfileStore,
    VideoDeviceSource,
    VisionSettings,
    WindowSource,
)
from livesubs.device.messages import (
    CloseDevice,
    DeviceErrorKind,
    DeviceFailed,
    DeviceOpened,
    OpenDevice,
    PreviewChanged,
    ShowPreview,
    SignalChanged,
)
from livesubs.engine.capture.shared_frames import SharedFrameWriter
from livesubs.engine.translate.cache import TranslationCache
from livesubs.i18n import t
from livesubs.platform import hotkeys, win32
from livesubs.platform.video_devices import VideoDevice
from livesubs.platform.win32 import MonitorInfo, WindowInfo
from livesubs.ui import theme as themes
from livesubs.ui.frame_region import FrameRegionDialog
from livesubs.ui.inspector import Inspector
from livesubs.ui.main_window import DEFAULT_PROFILE_ID, MainWindow
from livesubs.ui.overlay import Overlay, wrap
from livesubs.ui.pick import resolve, resolve_card
from livesubs.ui.placement import GAP_PX, custom_geometry, monitor_at, overlay_geometry
from livesubs.ui.region_select import RegionPicker
from livesubs.ui.screens import ScreenMap, current_screens, from_qrect, to_logical, to_physical
from livesubs.ui.tabs.profiles import ProfilesTab
from livesubs.ui.tabs.settings import SettingsTab, hotkey_text

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


SCREEN = Rect(0, 0, 1920, 1080)
SUBTITLE = Rect(200, 900, 1500, 120)


@pytest.mark.parametrize(
    ("placement", "expected"),
    [
        (OverlayPlacement.ABOVE, Rect(200, 900 - 100 - GAP_PX, 1500, 100)),
        (OverlayPlacement.BELOW, Rect(200, 900 - 100 - GAP_PX, 1500, 100)),  # no room: flips up
        (OverlayPlacement.OVER, Rect(200, 900, 1500, 120)),  # at least as tall as the subtitle
    ],
)
def test_overlay_geometry(placement: OverlayPlacement, expected: Rect) -> None:
    assert overlay_geometry(placement, SUBTITLE, SCREEN, 100) == expected


def test_overlay_flips_when_there_is_no_room() -> None:
    top = Rect(200, 20, 1500, 60)
    assert overlay_geometry(OverlayPlacement.ABOVE, top, SCREEN, 90).y == 80 + GAP_PX
    mid = Rect(200, 500, 1500, 60)
    assert overlay_geometry(OverlayPlacement.BELOW, mid, SCREEN, 90).y == 560 + GAP_PX


def test_custom_geometry_and_monitor_lookup() -> None:
    laptop = MONITORS[1]
    rect = NormRect(x=0.25, y=0.5, width=0.5, height=0.1)
    assert custom_geometry(rect, laptop.rect) == Rect(1920 + 640, -164 + 720, 1280, 144)
    assert monitor_at(MONITORS, 2500, 0) is laptop
    assert monitor_at(MONITORS, -500, 500) is MONITORS[0]  # off-screen: the nearest


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
    font, lines = overlay._layout(overlay.text, 380, 80)
    assert len(lines) <= overlay.settings.max_lines
    assert font.pointSizeF() < overlay.settings.font_size_pt


# --- main window --------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def no_video_devices(monkeypatch: pytest.MonkeyPatch) -> None:
    """The source list must not depend on the webcams and cards of the PC running the tests."""
    monkeypatch.setattr("livesubs.ui.main_window.list_video_devices", lambda: [])
    monkeypatch.setattr("livesubs.ui.tabs.settings.list_video_devices", lambda: [])


class FakeCardHost:
    """Stands in for the device host process."""

    def __init__(self) -> None:
        self.sent: list = []
        self.events: list = []
        self.started = self.alive = False
        self.pid: int | None = None
        self.restarts = 0

    def start(self) -> None:
        self.started = self.alive = True
        self.pid = 4242

    def restart(self) -> None:
        self.restarts += 1
        self.alive = True

    def send(self, command: object) -> None:
        self.sent.append(command)

    def poll(self, max_events: int = 100) -> list:
        events, self.events = self.events, []
        return events

    def shutdown(self, timeout: float = 5.0) -> None:
        self.alive = False


class FakeEngine:
    def __init__(self) -> None:
        self.sent: list[Command] = []
        self.queue: list[Event] = []
        self.started = self.stopped = False
        self.alive = True
        self.restarts = 0

    def start(self) -> None:
        self.started = True

    def restart(self) -> None:
        self.restarts += 1
        self.alive = True

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
    monkeypatch.setattr("livesubs.ui.main_window.cpu_share", lambda url, model: None)
    win = MainWindow(
        FakeEngine(),
        config=AppConfig(),
        store=ProfileStore(tmp_path / "profiles"),
        card_host=FakeCardHost(),
    )
    qtbot.addWidget(win)
    # A LiveSubs running on this PC holds F8-F10; its "hotkeys taken" notice is not under test.
    win.statusBar().clearMessage()
    return win


def engine_of(window: MainWindow) -> FakeEngine:
    return window._engine  # type: ignore[return-value]


def card_host_of(window: MainWindow) -> FakeCardHost:
    return window._card._host  # type: ignore[return-value]


def test_start_needs_a_region(window: MainWindow) -> None:
    assert engine_of(window).started
    window.toggle()
    assert engine_of(window).sent == []
    assert window.statusBar().currentMessage()


def test_no_source_is_preselected(window: MainWindow) -> None:
    """Regression: the front-most window (the Explorer window the exe was started from) used
    to be preselected, so the user's region silently applied to the wrong window."""
    assert window.selected_source() is None
    assert window.source_combo.currentIndex() == -1


def test_start_sends_the_profile_and_saves_it(window: MainWindow, tmp_path: Path) -> None:
    window.source_combo.setCurrentIndex(window.source_combo.count() - 1)  # a monitor
    window.set_region(NormRect(x=0.1, y=0.8, width=0.8, height=0.15))
    window.toggle()
    command = engine_of(window).sent[-1]
    assert isinstance(command, StartPipeline)
    assert command.profile.region == NormRect(x=0.1, y=0.8, width=0.8, height=0.15)
    assert command.profile.source is not None
    assert command.profile_id == DEFAULT_PROFILE_ID
    assert (
        ProfileStore(tmp_path / "profiles").load(DEFAULT_PROFILE_ID).region
        == command.profile.region
    )


def test_a_dead_engine_is_replaced(window: MainWindow) -> None:
    """Regression: windows-capture crashed the engine on Start; the UI waited in STARTING
    forever and Start/Stop went to a dead process, so the app looked frozen."""
    engine = engine_of(window)
    engine.queue = [StateChanged(EngineState.STARTING)]
    window._poll()
    assert window.start_button.text() == "Durdur"

    window._check_engine()
    assert engine.restarts == 0

    engine.alive = False
    window._check_engine()
    assert engine.restarts == 1
    assert window.start_button.text() == "Başlat"
    assert "yeniden başlatıldı" in window.statusBar().currentMessage()


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

    # A new line keeps the old translation up until its own is ready.
    engine.queue = [SubtitleRead(2, "Who are you?", 10.0)]
    window._poll()
    assert window.feed.rowCount() == 2
    assert window._overlay.text == "Arabaya bin."
    engine.queue = [SubtitleTranslated(2, "Who are you?", "Sen kimsin?", "m", "gpu", 9, 500, False)]
    window._poll()
    assert window._overlay.text == "Sen kimsin?"


def test_lines_after_a_restart_go_on_top_of_the_feed(window: MainWindow) -> None:
    """The engine numbers lines from 1 again on every Start: a new line 1 once overwrote the old
    line 1's row further down, so the newest translation was not at the top."""
    engine = engine_of(window)
    window.set_region(NormRect(x=0.1, y=0.8, width=0.8, height=0.15))
    engine.queue = [
        StateChanged(EngineState.RUNNING),
        SubtitleRead(1, "Get in.", 9.0),
        SubtitleTranslated(1, "Get in.", "Bin.", "m", "gpu", 9, 500, False),
        SubtitleRead(2, "Drive.", 9.0),
        SubtitleTranslated(2, "Drive.", "Sür.", "m", "gpu", 9, 500, False),
    ]
    window._poll()
    window._send_start()
    engine.queue = [
        SubtitleRead(1, "Who are you?", 9.0),
        SubtitleTranslated(1, "Who are you?", "Sen kimsin?", "m", "gpu", 9, 500, False),
    ]
    window._poll()
    assert [window.feed.item(row, 2).text() for row in range(3)] == ["Sen kimsin?", "Sür.", "Bin."]

    # A repeat of line 1 in the new session never brings back the old session's line 1.
    window._overlay.clear()
    engine.queue = [SubtitleRead(2, "Hey.", 9.0), SubtitleRead(2, "Hey.", 9.0, repeat=True)]
    window._poll()
    assert window._overlay.text == ""


def test_translation_outlives_its_subtitle_by_the_clear_delay(
    window: MainWindow, qtbot: QtBot
) -> None:
    engine = engine_of(window)
    assert window._profile.overlay.clear_delay_ms == 2000  # default: time to finish reading
    window._profile.overlay.clear_delay_ms = 300  # keep the test fast
    window._overlay.show_text("Sen kimsin?")

    # The subtitle disappears, but a new one comes before the delay is over: nothing is cleared.
    engine.queue = [SubtitleCleared(), SubtitleRead(3, "Get out.", 9.0)]
    window._poll()
    qtbot.wait(500)
    assert window._overlay.text == "Sen kimsin?"

    # It disappears and nothing follows: cleared once the delay has passed.
    engine.queue = [SubtitleCleared()]
    window._poll()
    assert window._overlay.text == "Sen kimsin?"
    qtbot.waitUntil(lambda: window._overlay.text == "", timeout=2000)


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


# --- picking a region anywhere on the desktop ---------------------------------------------------

MONITORS = [
    MonitorInfo(1, r"\\.\DISPLAY1", Rect(0, 0, 1920, 1080), Rect(0, 0, 1920, 1032), True, 96),
    MonitorInfo(
        2, r"\\.\DISPLAY2", Rect(1920, -164, 2560, 1440), Rect(1920, -164, 2560, 1392), False, 144
    ),
]


def _window(title: str, exe: str, area: Rect, minimized: bool = False) -> tuple[WindowInfo, Rect]:
    return WindowInfo(hash(title), title, 1, exe, area, minimized), area


def test_pick_uses_the_window_under_the_selection() -> None:
    browser = _window("GTA trailer - YouTube", "chrome.exe", Rect(0, 0, 1920, 1080))
    explorer = _window("LiveSubs - Dosya Gezgini", "explorer.exe", Rect(1950, 0, 900, 600))
    pick = resolve(Rect(300, 930, 1320, 120), [explorer, browser], MONITORS)
    assert pick is not None
    assert pick.source == WindowSource(exe_name="chrome.exe", title="GTA trailer - YouTube")
    assert pick.region.to_pixels(1920, 1080) == Rect(300, 930, 1320, 120)
    assert pick.label == "GTA trailer - YouTube"


def test_pick_prefers_the_front_window_and_normalizes_to_it() -> None:
    small = _window("Remote Play", "RemotePlay.exe", Rect(100, 100, 1280, 720))
    behind = _window("Desktop app", "app.exe", Rect(0, 0, 1920, 1080))
    pick = resolve(Rect(100 + 128, 100 + 612, 1024, 72), [small, behind], MONITORS)
    assert pick.source.exe_name == "RemotePlay.exe"
    assert pick.region == NormRect(x=0.1, y=0.85, width=0.8, height=0.1)


def test_pick_skips_minimized_windows_and_the_desktop() -> None:
    minimized = _window("Game", "game.exe", Rect(0, 0, 1920, 1080), minimized=True)
    desktop = _window("Program Manager", "explorer.exe", Rect(0, 0, 4480, 1440))
    pick = resolve(Rect(2200, 1000, 800, 100), [minimized, desktop], MONITORS)
    assert pick.source == MonitorSource(device_name=r"\\.\DISPLAY2")
    assert pick.area == MONITORS[1].rect
    assert pick.label == "DISPLAY2"


def test_pick_outside_every_monitor() -> None:
    assert resolve(Rect(9000, 9000, 50, 50), [], MONITORS) is None


def test_picker_covers_every_screen(qtbot: QtBot) -> None:
    picker = RegionPicker()
    picker.show()
    layers = picker._layers
    assert len(layers) == len(QGuiApplication.screens())
    for layer in layers:
        qtbot.addWidget(layer)
        assert layer.geometry() == layer.screen().geometry()
    picker.cancel()
    assert picker._layers == []


# --- appearance and calibration editing ----------------------------------------------------------


def test_appearance_edits_apply_to_the_profile(window: MainWindow, qtbot: QtBot) -> None:
    tab = window.appearance
    settings = window._profile.overlay
    with qtbot.waitSignal(tab.changed):
        tab.placement.setCurrentIndex(1)
    assert settings.placement is OverlayPlacement.OVER
    tab.background.setChecked(False)
    assert settings.background is False
    assert not tab.opacity.isEnabled()
    tab.size.setValue(40)
    tab.clear_delay.setValue(2.5)
    tab.show_original.setChecked(True)
    assert (settings.font_size_pt, settings.clear_delay_ms, settings.show_original) == (
        40,
        2500,
        True,
    )

    # Saved shortly after (debounced).
    def saved() -> None:
        assert window._store.ids() == [DEFAULT_PROFILE_ID]
        assert window._store.load(DEFAULT_PROFILE_ID).overlay.font_size_pt == 40

    qtbot.waitUntil(saved, timeout=2000)


def test_preview_shows_a_sample_and_hides_it_again(window: MainWindow) -> None:
    window.appearance.preview.setChecked(True)
    assert window._overlay.isVisible()
    assert window._overlay.text
    window.appearance.preview.setChecked(False)
    assert window._overlay.text == ""
    assert not window._overlay.isVisible()


def test_moving_the_overlay_makes_its_place_custom(window: MainWindow) -> None:
    primary = next(m for m in win32.list_monitors() if m.is_primary)
    logical = to_logical(
        Rect(primary.rect.x + 100, primary.rect.y + 200, 800, 120), current_screens()
    )
    window._on_overlay_edited(logical)
    s = window._profile.overlay
    assert s.placement is OverlayPlacement.CUSTOM
    assert s.monitor_device == primary.device_name
    assert s.rect.to_pixels(primary.rect.width, primary.rect.height) == Rect(100, 200, 800, 120)
    assert window.appearance.placement.currentData() == OverlayPlacement.CUSTOM


def test_overlay_edit_mode_drag_moves_it(qtbot: QtBot) -> None:
    overlay = Overlay()
    qtbot.addWidget(overlay)
    overlay.place(Rect(100, 100, 600, 120))
    overlay.set_editable(True)
    assert overlay.isVisible() and overlay.editable
    start = overlay.geometry()
    with qtbot.waitSignal(overlay.edited) as moved:
        qtbot.mousePress(overlay, Qt.MouseButton.LeftButton, pos=QPoint(300, 60))
        qtbot.mouseMove(overlay, QPoint(340, 90))
        qtbot.mouseRelease(overlay, Qt.MouseButton.LeftButton, pos=QPoint(340, 90))
    assert overlay.geometry().size() == start.size()
    assert moved.args[0] == from_qrect(overlay.geometry())
    overlay.set_editable(False)
    assert not overlay.editable


# --- calibration mode, pause, hide ---------------------------------------------------------------


def _with_monitor_source(window: MainWindow) -> win32.MonitorInfo:
    primary = next(m for m in win32.list_monitors() if m.is_primary)
    window._profile.source = MonitorSource(device_name=primary.device_name)
    window.refresh_sources()
    window.set_region(NormRect(x=0.1, y=0.8, width=0.8, height=0.15))
    return primary


def test_calibration_shows_editable_overlay_and_region(window: MainWindow) -> None:
    primary = _with_monitor_source(window)
    window.set_calibration(True)
    assert window._overlay.editable and window._overlay.isVisible()
    assert window._region_frame.isVisible()
    expected = to_logical(
        NormRect(x=0.1, y=0.8, width=0.8, height=0.15)
        .to_pixels(primary.rect.width, primary.rect.height)
        .translated(primary.rect.x, primary.rect.y),
        current_screens(),
    )
    assert from_qrect(window._region_frame.geometry()) == expected

    # Dragging the frame changes the region (normalized to the source).
    tenth = Rect(0, 0, 1, primary.rect.height // 10)  # CI's screen is not 1080 px tall
    moved = expected.translated(0, -to_logical(tenth, current_screens()).height)
    window._on_region_edited(moved)
    assert window._profile.region.y == pytest.approx(0.7, abs=0.01)

    window.set_calibration(False)
    assert not window._overlay.editable
    assert not window._region_frame.isVisible()
    assert not window._overlay.isVisible()  # not running: nothing to show


def test_pause_hotkey_and_paused_state(window: MainWindow) -> None:
    engine = engine_of(window)
    engine.queue = [StateChanged(EngineState.RUNNING)]
    window._poll()
    window._overlay.show_text("Arabaya bin.")
    window.on_hotkey("pause")
    assert engine.sent[-1] == SetPaused(True)
    engine.queue = [StateChanged(EngineState.PAUSED)]
    window._poll()
    assert window._overlay.text == ""
    assert "Duraklatıldı" in window.state_label.text()
    window.on_hotkey("pause")
    assert engine.sent[-1] == SetPaused(False)


def test_toggle_overlay_keeps_text_hidden(window: MainWindow) -> None:
    window._overlay.show_text("Arabaya bin.")
    window.on_hotkey("toggle_overlay")
    assert not window._overlay.isVisible()
    window._overlay.show_text("Sen kimsin?")  # a new translation while hidden
    assert not window._overlay.isVisible()
    window.on_hotkey("toggle_overlay")
    assert window._overlay.isVisible()
    assert window._overlay.text == "Sen kimsin?"


# --- Inspector ------------------------------------------------------------------------------------


def _debug_frame(**changes: object) -> DebugFrame:
    values = {
        "region": np.full((40, 300, 3), 30, np.uint8),
        "ocr_input": np.full((80, 600), 255, np.uint8),
        "text_fraction": 0.05,
        "change": "same",
        "last_read": "Get in the car.",
        "rejected": "",
    }
    return DebugFrame(**{**values, **changes})


def test_inspector_shows_what_the_pipeline_sees(qtbot: QtBot) -> None:
    inspector = Inspector(VisionSettings())
    qtbot.addWidget(inspector)
    inspector.show()
    inspector.show_frame(_debug_frame())
    assert not inspector.region_image.pixmap().isNull()
    assert not inspector.ocr_image.pixmap().isNull()
    assert "5.0%" in inspector.stats.text() and "aynı satır" in inspector.stats.text()
    assert "Get in the car." in inspector.last_read.text() and "geçti" in inspector.last_read.text()
    inspector.show_frame(_debug_frame(last_read="||||", rejected="repeated_junk"))
    assert "elendi" in inspector.last_read.text()


def test_inspector_edits_vision_settings(qtbot: QtBot) -> None:
    vision = VisionSettings(text_colors=["#FFFFFF"])
    inspector = Inspector(vision)
    qtbot.addWidget(inspector)
    with qtbot.waitSignal(inspector.vision_changed):
        inspector.tolerance.slider.setValue(90)
    assert vision.color_tolerance == 90
    inspector.yellow.setChecked(True)
    assert vision.text_colors == ["#FFFFFF", "#FFFF00"]
    inspector.white.setChecked(False)
    inspector.yellow.setChecked(False)  # never zero colors: white comes back
    assert inspector.white.isChecked()
    assert vision.text_colors == ["#FFFFFF"]
    inspector.contrast.setChecked(False)
    assert vision.local_contrast is False


def test_calibration_drives_debug_frames_and_vision(window: MainWindow) -> None:
    engine = engine_of(window)
    _with_monitor_source(window)
    engine.queue = [StateChanged(EngineState.RUNNING)]
    window._poll()
    window.set_calibration(True)
    assert engine.sent[-1] == SetDebug(True)
    assert window._inspector.isVisible()
    engine.queue = [_debug_frame()]
    window._poll()
    assert "Get in the car." in window._inspector.last_read.text()

    window._inspector.tolerance.slider.setValue(100)
    assert isinstance(engine.sent[-1], SetVision)
    assert engine.sent[-1].vision.color_tolerance == 100
    window.set_calibration(False)
    assert SetDebug(False) in engine.sent
    assert not window._inspector.isVisible()


# --- setup --------------------------------------------------------------------------------------


def test_model_from_setup_becomes_the_translation_model(window: MainWindow) -> None:
    window.setup.model_selected.emit("gemma4:26b-a4b-it-qat")
    assert window._config.ollama.gpu_model == "gemma4:26b-a4b-it-qat"
    assert load_config().ollama.gpu_model == "gemma4:26b-a4b-it-qat"


def test_imported_settings_are_not_overwritten(
    window: MainWindow, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "livesubs.ui.main_window.QMessageBox.question",
        lambda *args: QMessageBox.StandardButton.No,
    )
    window.setup.settings_imported.emit()
    window.set_region(NormRect(x=0.1, y=0.8, width=0.8, height=0.1))
    assert not paths.config_file().exists()


def test_missing_ocr_language_opens_setup(window: MainWindow) -> None:
    window.handle(EngineError(ErrorKind.OCR_UNAVAILABLE, "no English", fatal=True))
    assert window.setup.isVisible()


# --- models tab ---------------------------------------------------------------------------------


def test_models_tab_follows_the_engine_and_the_model_choice(window: MainWindow) -> None:
    window.handle(StateChanged(EngineState.RUNNING))
    assert window.models._busy
    assert not window.models.compare_button.isEnabled()
    window.handle(StateChanged(EngineState.STOPPED))
    assert not window.models._busy

    window.model_combo.addItems(["gemma4:12b-it-qat", "qwen3:8b"])
    window.model_combo.setCurrentIndex(1)
    window.model_combo.activated.emit(1)
    assert window._config.ollama.gpu_model == "qwen3:8b"
    assert load_config().ollama.gpu_model == "qwen3:8b"

    window.models.model_selected.emit("gemma4:12b-it-qat")
    assert window._config.ollama.gpu_model == "gemma4:12b-it-qat"


def test_model_spilling_to_the_cpu_is_reported_once_per_start(window: MainWindow) -> None:
    window.source_combo.setCurrentIndex(window.source_combo.count() - 1)
    window.set_region(NormRect(x=0.1, y=0.8, width=0.8, height=0.15))
    window.toggle()
    window.handle(StateChanged(EngineState.RUNNING))
    window._on_spill("gemma4:12b-it-qat", 0.28)
    assert "%72" in window.statusBar().currentMessage()
    window.statusBar().clearMessage()
    window._on_spill("gemma4:12b-it-qat", 0.28)
    assert window.statusBar().currentMessage() == ""

    window.handle(StateChanged(EngineState.STOPPED))
    window.toggle()  # a new start: worth one more warning
    window.handle(StateChanged(EngineState.RUNNING))
    window._on_spill("gemma4:12b-it-qat", 0.28)
    assert "%72" in window.statusBar().currentMessage()


# --- settings tab, tray, warnings ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("typed", "stored"),
    [("F8", "F8"), ("Ctrl+Shift+P", "Ctrl+Shift+P"), ("Meta+F5", "Win+F5"), ("PgUp", "PgUp")],
)
def test_typed_hotkeys_are_stored_in_parser_form(typed: str, stored: str) -> None:
    text = hotkey_text(QKeySequence(typed))
    assert text == stored
    hotkeys.parse(text)  # Windows can register it


def settings_tab(qtbot: QtBot) -> SettingsTab:
    tab = SettingsTab(AppConfig(), Profile(name="p"))
    qtbot.addWidget(tab)
    return tab


def test_settings_edit_the_config(qtbot: QtBot) -> None:
    tab = settings_tab(qtbot)
    with qtbot.waitSignal(tab.config_changed):
        tab.context_lines.setValue(5)
    assert tab.config.ollama.context_lines == 5
    tab.keep_alive.setCurrentIndex(tab.keep_alive.findData("-1"))
    assert tab.config.ollama.keep_alive == "-1"
    with qtbot.waitSignal(tab.profile_changed):
        tab.fps.setValue(15)
    assert tab.profile.capture.fps == 15


def test_ollama_url_must_be_http(qtbot: QtBot) -> None:
    tab = settings_tab(qtbot)
    tab.ollama_url.setText("bilgisayar:11434")
    tab.ollama_url.editingFinished.emit()
    assert tab.config.ollama.url == "http://127.0.0.1:11434"
    assert tab.ollama_url.text() == "http://127.0.0.1:11434"
    tab.ollama_url.setText("http://192.168.1.20:11434/")
    tab.ollama_url.editingFinished.emit()
    assert tab.config.ollama.url == "http://192.168.1.20:11434"


def test_hotkey_change_is_validated(qtbot: QtBot) -> None:
    tab = settings_tab(qtbot)
    edit = tab.hotkey_edits["pause"]
    edit.setKeySequence(QKeySequence("F8"))  # already the calibration key
    edit.editingFinished.emit()
    assert tab.config.hotkeys.pause == "F10"
    assert edit.keySequence().toString() == "F10"
    with qtbot.waitSignal(tab.hotkeys_changed):
        edit.setKeySequence(QKeySequence("Ctrl+F11"))
        edit.editingFinished.emit()
    assert tab.config.hotkeys.pause == "Ctrl+F11"
    tab.reset_hotkeys()
    assert tab.config.hotkeys.pause == "F10"


def test_settings_are_saved(window: MainWindow, qtbot: QtBot) -> None:
    window.settings.context_lines.setValue(6)
    window.settings.fps.setValue(12)
    qtbot.waitUntil(
        lambda: paths.config_file().exists() and load_config().ollama.context_lines == 6
    )
    assert window._store.load(DEFAULT_PROFILE_ID).capture.fps == 12


def test_recording_mode_lets_capture_see_the_overlay(window: MainWindow) -> None:
    assert not window._overlay.capture_visible
    window.settings.recording.setChecked(True)
    assert window._overlay.capture_visible
    assert window._config.recording_mode


def running_with_region(window: MainWindow) -> None:
    window.set_region(NormRect(x=0.1, y=0.8, width=0.8, height=0.15))
    engine_of(window).queue = [
        StateChanged(EngineState.RUNNING),
        FrameInfo(1920, 1080, (0, 0, 1920, 1080)),
    ]
    window._poll()


def test_recording_mode_warns_when_the_overlay_covers_the_subtitle(window: MainWindow) -> None:
    running_with_region(window)
    window._profile.overlay.placement = OverlayPlacement.OVER
    window._place_overlay()
    assert not window.statusBar().currentMessage()  # hidden from capture: no problem
    window.settings.recording.setChecked(True)
    assert "OCR çeviriyi de okuyabilir" in window.statusBar().currentMessage()
    window.statusBar().clearMessage()
    window._profile.overlay.placement = OverlayPlacement.ABOVE
    window._place_overlay()
    assert not window._warned_overlap


def test_exclusive_fullscreen_is_reported_once(
    window: MainWindow, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(win32, "is_exclusive_fullscreen_running", lambda: True)
    window._check_fullscreen()
    assert not window.statusBar().currentMessage()  # not running: nothing to warn about
    running_with_region(window)
    window._check_fullscreen()
    assert "Kenarlıksız" in window.statusBar().currentMessage()
    window.statusBar().clearMessage()
    window._check_fullscreen()
    assert not window.statusBar().currentMessage()


def test_cache_is_cleared_only_while_stopped(window: MainWindow) -> None:
    cache = TranslationCache(paths.translation_cache_file())
    cache.put("m", "p", "Hi.", "Selam.")
    cache.close()
    running_with_region(window)
    window.clear_cache()
    assert window.settings.upkeep_status.text() == "Önce çeviriyi durdurun."
    window.handle(StateChanged(EngineState.STOPPED))
    window.settings.clear_cache.click()  # through the button, as the user does
    assert window.settings.upkeep_status.text() == "Önbellek temizlendi."
    cache = TranslationCache(paths.translation_cache_file())
    assert cache.get("m", "p", "Hi.") is None
    cache.close()


def test_request_buttons_emit_their_signals(window: MainWindow, qtbot: QtBot) -> None:
    """`clicked=some_signal` in a PySide constructor raises when clicked ("not callable"): these
    buttons once did nothing at all."""
    buttons = [
        (window.models.start_button, window.models.start_ollama_requested),
        (window.models.setup_button, window.models.setup_requested),
        (window.settings.clear_cache, window.settings.clear_cache_requested),
        (window.settings.open_logs, window.settings.open_logs_requested),
    ]
    for button, signal in buttons:
        signal.disconnect()  # the window's slots would start Ollama, open dialogs
        with qtbot.waitSignal(signal, timeout=500):
            button.click()


def test_tray_follows_the_engine_state(window: MainWindow) -> None:
    assert window.tray.run_action.text() == "Başlat"
    running_with_region(window)
    assert window.tray.run_action.text() == "Durdur"


def test_closing_from_the_tray_quits_the_app(
    window: MainWindow, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: a window hidden in the tray closed without ending the app."""
    quits: list[bool] = []
    monkeypatch.setattr("livesubs.ui.main_window.QCoreApplication.quit", lambda: quits.append(True))
    window.hide()
    window.tray.quit.emit()
    assert quits == [True]
    assert engine_of(window).stopped


# --- profiles -----------------------------------------------------------------------------------


def make_window(qtbot: QtBot, tmp_path: Path, config: AppConfig | None = None) -> MainWindow:
    win = MainWindow(
        FakeEngine(), config=config or AppConfig(), store=ProfileStore(tmp_path / "profiles")
    )
    qtbot.addWidget(win)
    return win


def test_startup_uses_the_last_profile(
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LIVESUBS_HOME", str(tmp_path))
    monkeypatch.setattr("livesubs.ui.main_window.list_models", lambda url: None)
    store = ProfileStore(tmp_path / "profiles")
    store.save(Profile(name="GTA"), "gta")
    store.save(Profile(name="Mafia"), "mafia")
    assert make_window(qtbot, tmp_path, AppConfig(last_profile="mafia"))._profile.name == "Mafia"
    assert make_window(qtbot, tmp_path, AppConfig(last_profile="gone"))._profile.name == "GTA"


def test_new_profile_becomes_active_everywhere(window: MainWindow) -> None:
    window._profile.overlay.font_size_pt = 40
    window.new_profile("Vice City Gece")
    assert window._profile_id == "vice-city-gece"
    assert window._profile.overlay.font_size_pt == 26  # a fresh profile
    assert window._overlay.settings is window._profile.overlay
    assert window.appearance.settings is window._profile.overlay
    assert window._inspector.vision is window._profile.vision
    assert window.settings.profile is window._profile
    assert window._store.ids() == [DEFAULT_PROFILE_ID, "vice-city-gece"]
    assert [window.profile_combo.itemText(i) for i in range(2)] == ["Varsayılan", "Vice City Gece"]
    assert window.profile_combo.currentData() == "vice-city-gece"
    assert window._config.last_profile == "vice-city-gece"

    window.switch_profile(DEFAULT_PROFILE_ID)
    assert window._profile.overlay.font_size_pt == 40
    assert window.appearance.size.value() == 40


def test_switching_while_running_follows_the_new_profile(window: MainWindow) -> None:
    running_with_region(window)
    window.source_combo.setCurrentIndex(window.source_combo.count() - 1)  # a monitor
    window._profile.source = window.selected_source().spec
    window.copy_profile("Kopya")  # same source and region: restarts with the copy
    command = engine_of(window).sent[-1]
    assert isinstance(command, StartPipeline)
    assert command.profile_id == "kopya"
    assert command.profile.name == "Kopya"
    window.new_profile("Boş")  # no source or region yet: stops
    assert isinstance(engine_of(window).sent[-1], StopPipeline)


def test_rename_keeps_the_file_and_delete_moves_on(
    window: MainWindow, monkeypatch: pytest.MonkeyPatch
) -> None:
    window.delete_profile()
    assert window.statusBar().currentMessage() == "Son profil silinemez."
    window.new_profile("Mafia")
    window.rename_profile("Mafia II")
    assert window._store.load("mafia").name == "Mafia II"
    assert window.profile_combo.currentText() == "Mafia II"
    window.delete_profile()
    assert window._store.ids() == [DEFAULT_PROFILE_ID]
    assert window._profile_id == DEFAULT_PROFILE_ID


def test_profiles_tab_edits_the_translation_hints(qtbot: QtBot) -> None:
    profile = Profile(name="GTA", source=WindowSource(exe_name="RemotePlay.exe"))
    tab = ProfilesTab(profile, monitors=lambda: [])
    qtbot.addWidget(tab)
    with qtbot.waitSignal(tab.changed):
        tab.context.setPlainText("Gritty 1980s crime story.")
    assert profile.translation.context == "Gritty 1980s crime story."

    tab.glossary.add_row()
    tab.glossary.table.item(0, 0).setText("Vice City")
    assert profile.translation.glossary == {}  # needs both columns
    tab.glossary.table.item(0, 1).setText("Vice City")
    assert profile.translation.glossary == {"Vice City": "Vice City"}
    tab.glossary.table.selectRow(0)
    tab.glossary.remove_rows()
    assert profile.translation.glossary == {}

    tab.examples.add_row()
    tab.examples.table.item(0, 0).setText("Copy.")
    tab.examples.table.item(0, 1).setText("Anlaşıldı.")
    assert profile.translation.examples == {"Copy.": "Anlaşıldı."}
    assert profile.translation.glossary == {}


def test_profiles_tab_imports_a_translation_pack(
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(QMessageBox, "information", lambda *args: None)
    profile = Profile(name="GTA VI")
    profile.translation.glossary = {"old": "eski"}
    tab = ProfilesTab(profile, monitors=lambda: [])
    qtbot.addWidget(tab)
    pack = tmp_path / "gta6.json"
    pack.write_text(
        '{"game_context": "Set in Leonida.", "glossary": [{"en": "Lucia", "tr": "Lucia"}],'
        ' "examples": [{"en": "Copy.", "tr": "Anlaşıldı."}], "test_lines": [{"en": "Hi."}]}',
        encoding="utf-8",
    )
    with qtbot.waitSignal(tab.changed):
        assert tab.import_pack(pack)
    assert profile.translation.context == "Set in Leonida."
    assert profile.translation.glossary == {"Lucia": "Lucia"}  # replaced, not merged
    assert profile.translation.examples == {"Copy.": "Anlaşıldı."}
    assert tab.context.toPlainText() == "Set in Leonida."
    assert tab.examples.table.rowCount() == 1

    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: warnings.append(args[2]))
    broken = tmp_path / "broken.json"
    broken.write_text("{nope", encoding="utf-8")
    assert not tab.import_pack(broken)
    assert warnings and profile.translation.context == "Set in Leonida."


def test_profiles_tab_game_location_and_vram_reserve(qtbot: QtBot) -> None:
    profile = Profile(name="PS5", source=WindowSource(exe_name="RemotePlay.exe"))
    tab = ProfilesTab(profile, monitors=lambda: [])
    qtbot.addWidget(tab)
    assert "başka cihazda" in tab.location.itemText(0)  # guessed from the streaming client
    assert tab.reserve.value() == 0.5
    tab.location.setCurrentIndex(1)  # this PC
    assert profile.vram.game_on_this_pc is True
    assert tab.reserve.value() == 2.0  # the automatic reserve follows
    tab.reserve_auto.setChecked(False)
    tab.reserve.setValue(3.5)
    assert profile.game_vram_reserve_gb() == 3.5
    tab.reserve_auto.setChecked(True)
    assert profile.vram.game_reserve_gb is None


def test_translation_can_go_to_another_monitor(window: MainWindow) -> None:
    """Game on the TV, translation on the PC screen next to it."""
    monitor = win32.list_monitors()[-1]
    running_with_region(window)
    index = window.profiles.display.findData(monitor.device_name)
    window.profiles.display.setCurrentIndex(index)
    assert window._profile.overlay.display == OverlayDisplay.MONITOR
    assert window._profile.overlay.monitor_device == monitor.device_name
    assert not window.appearance.placement.isEnabled()
    physical = to_physical(from_qrect(window._overlay.geometry()), current_screens())
    assert monitor.rect.intersects(physical)
    window.profiles.display.setCurrentIndex(0)
    assert window._profile.overlay.display == OverlayDisplay.AUTO


def test_theme_switch_from_settings(window: MainWindow) -> None:
    app = QGuiApplication.instance()
    combo = window.settings.theme
    assert [combo.itemData(i) for i in range(combo.count())] == list(themes.CHOICES)
    assert combo.currentData() == "apple"  # the default
    try:
        combo.setCurrentIndex(combo.findData("apple_light"))
        assert window._config.theme == "apple_light"
        assert app.palette().window().color().name() == "#f5f5f7"
        assert window.start_button.graphicsEffect() is None  # no glow
        combo.setCurrentIndex(combo.findData("vice"))
        assert window.header._theme.name == "vice"
        assert window.start_button.graphicsEffect() is not None  # Vice glows
        combo.setCurrentIndex(combo.findData("dark"))
        assert app.palette().window().color().name() == "#17181c"
    finally:
        combo.setCurrentIndex(combo.findData("apple"))
    assert app.palette().window().color().name() == "#1c1c1e"
    assert window.start_button.graphicsEffect() is None


def test_system_theme_follows_windows_live(
    window: MainWindow, monkeypatch: pytest.MonkeyPatch
) -> None:
    combo = window.settings.theme
    combo.setCurrentIndex(combo.findData("system"))
    for scheme, expected in (("light", "apple_light"), ("dark", "apple"), ("light", "apple_light")):
        monkeypatch.setattr(themes, "system_scheme", lambda s=scheme: s)
        window._on_color_scheme()  # what Qt's colorSchemeChanged calls
        assert window._theme.name == expected
    combo.setCurrentIndex(combo.findData("vice"))
    window._on_color_scheme()  # a chosen theme ignores Windows
    assert window._theme.name == "vice"
    combo.setCurrentIndex(combo.findData("apple"))


def test_sidebar_switches_pages(window: MainWindow) -> None:
    titles = [button.text() for button in window.nav_buttons]
    keys = ("tab.live", "tab.appearance", "tab.profiles", "tab.models", "tab.settings")
    assert titles == [t(k) for k in keys]
    assert window.pages.count() == 5
    for index, button in enumerate(window.nav_buttons):
        assert not button.icon().isNull()
        button.click()
        assert window.pages.currentIndex() == index
        assert button.isChecked()
        assert sum(b.isChecked() for b in window.nav_buttons) == 1
        title = window.pages.currentWidget().findChild(QLabel, "pageTitle")
        assert title is not None and title.text() == titles[index]
    window.show_page(0)
    assert window.nav_buttons[0].isChecked()


def test_sidebar_status(window: MainWindow) -> None:
    window._on_models(None)  # the fixture's Ollama check finds nothing either
    assert window.status_text.text() == t("sidebar.ollama_off")
    assert window.status_dot.property("state") == "warn"
    window._on_models([])
    assert window.status_text.text() == t("sidebar.stopped")
    engine_of(window).queue.append(StateChanged(EngineState.RUNNING))
    window._poll()
    assert window.status_text.text() == t("sidebar.running")
    assert window.status_dot.property("state") == "ok"
    engine_of(window).queue.append(StateChanged(EngineState.PAUSED))
    window._poll()
    assert window.status_text.text() == t("sidebar.paused")


def test_start_button_shows_the_running_state(window: MainWindow) -> None:
    window.handle(StateChanged(EngineState.RUNNING, "m"))
    assert window.start_button.property("running") is True
    assert window.start_button.text() == t("stop")
    window.handle(StateChanged(EngineState.STOPPED, ""))
    assert window.start_button.property("running") is False


class FakeWebServer:
    """Records what the phone page would get; never opens a port."""

    def __init__(self, port: int) -> None:
        self.port = port
        self.calls: list[tuple] = []
        self.running = False
        self.url = f"http://192.168.1.20:{port}/"
        self.client_count = 0

    def start(self) -> None:
        self.running = True

    def stop(self) -> None:
        self.running = False

    def show(self, line_id: int, text: str) -> None:
        self.calls.append(("show", line_id, text))

    def clear(self) -> None:
        self.calls.append(("clear",))

    def new_session(self) -> None:
        self.calls.append(("new_session",))


@pytest.fixture
def web(monkeypatch: pytest.MonkeyPatch) -> list[FakeWebServer]:
    servers: list[FakeWebServer] = []

    def make(port: int) -> FakeWebServer:
        servers.append(FakeWebServer(port))
        return servers[-1]

    monkeypatch.setattr("livesubs.ui.main_window.WebServer", make)
    return servers


def test_web_output_starts_and_follows_the_port(
    window: MainWindow, web: list[FakeWebServer]
) -> None:
    assert not web  # off by default
    window.settings.web_enabled.setChecked(True)
    assert web[-1].running and web[-1].port == 8765
    assert "192.168.1.20:8765" in window.settings.web_status.text()
    assert window.settings.web_qr.isVisibleTo(window.settings)
    window.settings.web_port.setValue(9000)
    window.settings.web_port.editingFinished.emit()
    assert not web[0].running
    assert web[-1].running and web[-1].port == 9000
    window.settings.web_enabled.setChecked(False)
    assert not web[-1].running
    assert window.settings.web_status.text() == t("settings.web_off")
    assert window._config.web.port == 9000


def test_translations_go_to_the_phone(window: MainWindow, web: list[FakeWebServer]) -> None:
    window.settings.web_enabled.setChecked(True)
    running_with_region(window)
    engine = engine_of(window)
    engine.queue = [
        SubtitleRead(1, "Get in.", 9.0),
        SubtitleTranslated(1, "Get in.", "Bin.", "m", "gpu", 9, 500, False),
    ]
    window._poll()
    window._profile.overlay.clear_delay_ms = 0
    engine.queue = [SubtitleCleared()]
    window._poll()
    QApplication.processEvents()
    assert web[-1].calls[-2:] == [("show", 1, "Bin."), ("clear",)]


def test_phone_only_hides_the_pc_overlay(window: MainWindow, web: list[FakeWebServer]) -> None:
    running_with_region(window)
    window.settings.web_only.setChecked(True)
    assert window._profile.overlay.display == OverlayDisplay.WEB_ONLY
    assert window._config.web.enabled and web[-1].running  # the phone is the only output now
    assert window.profiles.display.currentData() == OverlayDisplay.WEB_ONLY
    engine_of(window).queue = [SubtitleTranslated(1, "Get in.", "Bin.", "m", "gpu", 9, 500, False)]
    window._poll()
    assert not window._overlay.isVisible()
    assert web[-1].calls[-1] == ("show", 1, "Bin.")
    window.toggle_overlay()  # F9 twice: still hidden
    window.toggle_overlay()
    assert not window._overlay.isVisible()

    window.settings.web_only.setChecked(False)
    assert window._profile.overlay.display == OverlayDisplay.AUTO
    assert window._overlay.isVisible()


def test_phone_only_from_the_profiles_tab(window: MainWindow, web: list[FakeWebServer]) -> None:
    window.profiles.display.setCurrentIndex(window.profiles.display.count() - 1)
    assert window._profile.overlay.display == OverlayDisplay.WEB_ONLY
    assert window.settings.web_only.isChecked()
    assert window._overlay.disabled


# --- capture card -------------------------------------------------------------------------------

CARD_NAME = "Test Card"
CARD = VideoDeviceSource(device_name=CARD_NAME)


@pytest.fixture
def card_window(window: MainWindow, monkeypatch: pytest.MonkeyPatch) -> MainWindow:
    devices = [VideoDevice(0, CARD_NAME, "usb-test")]
    monkeypatch.setattr("livesubs.ui.main_window.list_video_devices", lambda: devices)
    monkeypatch.setattr("livesubs.ui.tabs.settings.list_video_devices", lambda: devices)
    window.refresh_sources()
    window.settings.refresh_preview_choices()
    return window


def select_card(window: MainWindow) -> None:
    labels = [window.source_combo.itemText(i) for i in range(window.source_combo.count())]
    window.source_combo.setCurrentIndex(labels.index(t("source.card", name=CARD_NAME)))
    window.set_region(NormRect(x=0.1, y=0.8, width=0.8, height=0.15))


def card_opens(window: MainWindow, frames: str) -> None:
    card_host_of(window).events.append(DeviceOpened(CARD_NAME, frames, 1920, 1080))
    window._card.poll()


def starts_of(window: MainWindow) -> list[StartPipeline]:
    return [c for c in engine_of(window).sent if isinstance(c, StartPipeline)]


def test_cards_are_listed_as_sources(card_window: MainWindow) -> None:
    assert t("source.card", name=CARD_NAME) in [
        card_window.source_combo.itemText(i) for i in range(card_window.source_combo.count())
    ]
    assert not card_host_of(card_window).started  # nothing wanted: no process yet


def test_start_on_a_card_waits_until_the_host_opened_it(card_window: MainWindow) -> None:
    select_card(card_window)
    card_window.toggle()
    host = card_host_of(card_window)
    assert host.started and host.sent == [OpenDevice(CARD)]
    assert starts_of(card_window) == []
    assert card_window.state_label.text() == t("state.card_opening")
    assert card_window.start_button.text() == t("stop")

    card_opens(card_window, "frames-1")
    (start,) = starts_of(card_window)
    assert start.shared_frames == "frames-1"
    assert start.profile.source == CARD


def test_stop_while_waiting_releases_the_card(card_window: MainWindow) -> None:
    select_card(card_window)
    card_window.toggle()
    card_window.toggle()
    assert card_host_of(card_window).sent == [OpenDevice(CARD), CloseDevice()]
    assert card_window.start_button.text() == t("start")
    card_opens(card_window, "frames-1")  # arrives late: no start any more
    assert starts_of(card_window) == []


def test_a_lost_card_resumes_when_it_is_back(card_window: MainWindow) -> None:
    select_card(card_window)
    card_window.toggle()
    card_opens(card_window, "frames-1")
    engine = engine_of(card_window)
    engine.queue = [StateChanged(EngineState.RUNNING)]
    card_window._poll()

    # The card was unplugged: the pipeline stops with "source lost"; the card stays wanted.
    engine.queue = [
        EngineError(ErrorKind.SOURCE_LOST, "closed", fatal=True),
        StateChanged(EngineState.STOPPED),
    ]
    card_window._poll()
    assert CloseDevice() not in card_host_of(card_window).sent
    assert card_window.state_label.text() == t("state.card_opening")
    card_opens(card_window, "frames-2")
    assert [s.shared_frames for s in starts_of(card_window)] == ["frames-1", "frames-2"]


def test_a_replaced_pipeline_keeps_the_card(card_window: MainWindow) -> None:
    """STOPPED also comes from the pipeline a new start replaces: never close the card then."""
    select_card(card_window)
    card_window.toggle()
    card_opens(card_window, "frames-1")
    engine_of(card_window).queue = [
        StateChanged(EngineState.RUNNING),
        StateChanged(EngineState.STOPPED),
        StateChanged(EngineState.STARTING),
    ]
    card_window._poll()
    assert CloseDevice() not in card_host_of(card_window).sent


def test_card_problems_are_explained(card_window: MainWindow) -> None:
    card_host_of(card_window).events = [
        DeviceFailed(CARD_NAME, DeviceErrorKind.BUSY, "no frames"),
    ]
    card_window._card.poll()
    assert card_window.statusBar().currentMessage() == t("card.busy", name=CARD_NAME)
    card_host_of(card_window).events = [SignalChanged(black=True)]
    card_window._card.poll()
    assert card_window.statusBar().currentMessage() == t("card.black")


def test_preview_opens_the_card_on_the_chosen_monitor(card_window: MainWindow) -> None:
    host = card_host_of(card_window)
    card_window.settings.preview_device.setCurrentIndex(1)
    card_window.settings.preview_device.activated.emit(1)
    card_window.settings.preview_enabled.setChecked(True)
    primary = next(m for m in win32.list_monitors() if m.is_primary)
    assert host.sent == [OpenDevice(CARD), ShowPreview(primary.device_name)]
    assert card_window._config.preview == PreviewSettings(enabled=True, device=CARD_NAME)

    # Esc on the preview: off, remembered, and the card is released (nothing else wants it).
    host.events = [PreviewChanged(None, by_user=True)]
    card_window._card.poll()
    assert not card_window._config.preview.enabled
    assert not card_window.settings.preview_enabled.isChecked()
    assert host.sent[-1] == CloseDevice()


def test_preview_shows_the_profile_card_by_default(card_window: MainWindow) -> None:
    select_card(card_window)
    card_window._profile.source = CARD
    card_window._apply_card()
    card_window.toggle_card_preview()  # from the tray
    assert card_host_of(card_window).sent[0] == OpenDevice(CARD)


def test_a_dead_device_host_is_replaced(card_window: MainWindow) -> None:
    select_card(card_window)
    card_window.toggle()
    host = card_host_of(card_window)
    host.alive = False
    card_window._card._watch()
    assert host.restarts == 1
    assert host.sent == [OpenDevice(CARD), OpenDevice(CARD)]  # told again what is wanted


def test_a_mark_on_the_preview_selects_the_card() -> None:
    preview = Rect(0, 0, 1920, 1080)
    pick = resolve_card(Rect(192, 864, 1536, 162), preview, CARD_NAME)
    assert pick is not None
    assert pick.source == CARD
    assert pick.region == NormRect(x=0.1, y=0.8, width=0.8, height=0.15)
    assert resolve_card(Rect(2500, 100, 100, 50), preview, CARD_NAME) is None  # elsewhere
    assert resolve_card(Rect(192, 864, 1536, 162), None, CARD_NAME) is None  # no preview


def test_region_on_a_card_frame(card_window: MainWindow, monkeypatch: pytest.MonkeyPatch) -> None:
    """No preview on screen: the region is picked on a still frame of the card."""
    select_card(card_window)
    picked = NormRect(x=0.2, y=0.7, width=0.6, height=0.2)
    monkeypatch.setattr(FrameRegionDialog, "exec", lambda self: 1)
    monkeypatch.setattr(FrameRegionDialog, "region", lambda self: picked)

    card_window.select_region()  # the card is not open: opened for a frame first
    assert card_host_of(card_window).sent == [OpenDevice(CARD)]
    writer = SharedFrameWriter(64, 36)
    try:
        writer.write(np.zeros((36, 64, 3), np.uint8), 1.0)
        card_opens(card_window, writer.name)
    finally:
        writer.close()
    assert card_window._profile.region == picked
    assert card_window._profile.source == CARD
    assert card_host_of(card_window).sent[-1] == CloseDevice()  # only opened for the frame


def test_frame_region_dialog_drag(qtbot: QtBot) -> None:
    dialog = FrameRegionDialog(np.zeros((1080, 1920, 3), np.uint8), None)
    qtbot.addWidget(dialog)
    view = dialog.view
    assert dialog.region() is None
    w, h = view.width(), view.height()
    start, end = QPoint(w // 10, h * 8 // 10), QPoint(w * 9 // 10, h * 95 // 100)
    qtbot.mousePress(view, Qt.MouseButton.LeftButton, pos=start)
    qtbot.mouseMove(view, end)
    qtbot.mouseRelease(view, Qt.MouseButton.LeftButton, pos=end)
    region = dialog.region()
    assert region is not None
    assert region.x == pytest.approx(0.1, abs=0.01)
    assert region.width == pytest.approx(0.8, abs=0.01)


def test_start_on_another_source_updates_the_region_label(card_window: MainWindow) -> None:
    card_window._profile.source = WindowSource(exe_name="opera.exe", title="YouTube - Opera")
    select_card(card_window)
    card_window.toggle()
    assert CARD_NAME in card_window.region_label.text()
