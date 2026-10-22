from pathlib import Path

import numpy as np
import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QGuiApplication, QKeySequence
from PySide6.QtWidgets import QMessageBox
from pytestqt.qtbot import QtBot

from livesubs.core import paths
from livesubs.core.config import AppConfig, load_config
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
    VisionSettings,
    WindowSource,
)
from livesubs.engine.translate.cache import TranslationCache
from livesubs.i18n import t
from livesubs.platform import hotkeys, win32
from livesubs.platform.win32 import MonitorInfo, WindowInfo
from livesubs.ui.inspector import Inspector
from livesubs.ui.main_window import DEFAULT_PROFILE_ID, MainWindow
from livesubs.ui.overlay import Overlay, wrap
from livesubs.ui.pick import resolve
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
    monkeypatch.setattr("livesubs.ui.main_window.cpu_share", lambda url, model: None)
    win = MainWindow(FakeEngine(), config=AppConfig(), store=ProfileStore(tmp_path / "profiles"))
    qtbot.addWidget(win)
    # A LiveSubs running on this PC holds F8-F10; its "hotkeys taken" notice is not under test.
    win.statusBar().clearMessage()
    return win


def engine_of(window: MainWindow) -> FakeEngine:
    return window._engine  # type: ignore[return-value]


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
    moved = expected.translated(0, -to_logical(Rect(0, 0, 1, 108), current_screens()).height)
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
    window.clear_cache()
    cache = TranslationCache(paths.translation_cache_file())
    assert cache.get("m", "p", "Hi.") is None
    cache.close()


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

    tab._add_term()
    tab.glossary.item(0, 0).setText("Vice City")
    assert profile.translation.glossary == {}  # needs both columns
    tab.glossary.item(0, 1).setText("Vice City")
    assert profile.translation.glossary == {"Vice City": "Vice City"}
    tab.glossary.selectRow(0)
    tab._remove_terms()
    assert profile.translation.glossary == {}


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
    try:
        combo.setCurrentIndex(combo.findData("dark"))
        assert window._config.theme == "dark"
        assert window.header._theme.name == "dark"
        assert window.start_button.graphicsEffect() is None  # no glow in the plain theme
        assert app.palette().window().color().name() == "#17181c"
    finally:
        combo.setCurrentIndex(combo.findData("vice"))
    assert window.start_button.graphicsEffect() is not None


def test_start_button_shows_the_running_state(window: MainWindow) -> None:
    window.handle(StateChanged(EngineState.RUNNING, "m"))
    assert window.start_button.property("running") is True
    assert window.start_button.text() == t("stop")
    window.handle(StateChanged(EngineState.STOPPED, ""))
    assert window.start_button.property("running") is False
