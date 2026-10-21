"""Main window (minimal, step 9): source, region, model, Start/Stop, live feed, overlay.

Talks to the engine process only through messages; polls its events every 30 ms on the UI thread.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QCloseEvent, QFont, QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from livesubs import APP_NAME, __version__
from livesubs.core.config import AppConfig, load_config, save_config
from livesubs.core.geometry import NormRect, Rect
from livesubs.core.messages import (
    Command,
    EngineError,
    EngineState,
    Event,
    FrameInfo,
    SetPaused,
    SetRegion,
    StartPipeline,
    StateChanged,
    StopPipeline,
    SubtitleCleared,
    SubtitleRead,
    SubtitleTranslated,
)
from livesubs.core.profiles import (
    MonitorSource,
    OverlayPlacement,
    Profile,
    ProfileStore,
    Source,
    VisionSettings,
    WindowSource,
)
from livesubs.i18n import t
from livesubs.ollama.client import DEFAULT_URL, InstalledModel, list_models, start_ollama
from livesubs.platform import win32
from livesubs.ui import screens
from livesubs.ui.hotkeys import HotkeyManager
from livesubs.ui.overlay import Overlay
from livesubs.ui.pick import resolve
from livesubs.ui.placement import custom_geometry, monitor_at, overlay_geometry
from livesubs.ui.region_frame import RegionFrame
from livesubs.ui.region_select import RegionPicker
from livesubs.ui.tabs.appearance import AppearanceTab

PROFILE_ID = "varsayilan"
FEED_LIMIT = 200
OLLAMA_CHECK_MS = 5000
FOLLOW_MS = 300
ACTIVE_STATES = (
    EngineState.STARTING,
    EngineState.WARMING_UP,
    EngineState.RUNNING,
    EngineState.PAUSED,
)
# Where the text block sits inside the overlay: next to the subtitle it accompanies.
TEXT_ALIGN = {
    OverlayPlacement.ABOVE: Qt.AlignmentFlag.AlignBottom,
    OverlayPlacement.OVER: Qt.AlignmentFlag.AlignVCenter,
    OverlayPlacement.BELOW: Qt.AlignmentFlag.AlignTop,
    OverlayPlacement.CUSTOM: Qt.AlignmentFlag.AlignBottom,
}
# Games mostly use white subtitles, some speakers yellow.
DEFAULT_TEXT_COLORS = ["#FFFFFF", "#FFFF00"]


class Engine(Protocol):
    def start(self) -> None: ...
    def send(self, command: Command) -> None: ...
    def poll(self, max_events: int = 100) -> list[Event]: ...
    def shutdown(self, timeout: float = 5.0) -> None: ...


@dataclass(frozen=True)
class SourceOption:
    label: str
    spec: Source
    area: Callable[[], Rect | None]  # where the source's frame is on screen, physical pixels
    window: win32.WindowInfo | None = None


def visible_client_area(hwnd: int) -> Rect | None:
    """The part of a window's client area that is captured (what WGC frames show)."""
    bounds, client = win32.window_frame_bounds(hwnd), win32.client_rect(hwnd)
    return client.intersection(bounds) if bounds and client else None


def available_sources() -> list[SourceOption]:
    options = []
    for window in win32.list_windows(exclude_pid=os.getpid()):
        if window.minimized:
            continue

        def area(hwnd: int = window.hwnd) -> Rect | None:
            return visible_client_area(hwnd)

        label = t("source.window", title=window.title[:60]) + f"  [{window.exe_name}]"
        spec = WindowSource(exe_name=window.exe_name, title=window.title)
        options.append(SourceOption(label, spec, area, window))
    for monitor in win32.list_monitors():
        label = t(
            "source.monitor",
            name=monitor.device_name.removeprefix("\\\\.\\"),
            width=monitor.rect.width,
            height=monitor.rect.height,
        )
        options.append(
            SourceOption(
                label, MonitorSource(device_name=monitor.device_name), lambda r=monitor.rect: r
            )
        )
    return options


class _Bridge(QObject):
    """Carries results from worker threads to the UI thread."""

    models = Signal(object)  # list[InstalledModel] | None


class MainWindow(QMainWindow):
    def __init__(
        self, engine: Engine, *, config: AppConfig | None = None, store: ProfileStore | None = None
    ) -> None:
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} {__version__}")
        self.resize(980, 640)
        self._engine = engine
        self._config = config or load_config()
        self._store = store or ProfileStore()
        self._profile = self._load_profile()
        self._sources: list[SourceOption] = []
        self._state = EngineState.IDLE
        self._frame: FrameInfo | None = None
        self._rows: dict[int, int] = {}
        self._translations: dict[int, str] = {}
        self._clear_generation = 0
        self._reads = 0  # lines read since Start: tells "running" from "waiting for subtitles"
        self._overlay = Overlay(self._profile.overlay)
        self._overlay.edited.connect(self._on_overlay_edited)
        self._region_frame = RegionFrame()
        self._region_frame.edited.connect(self._on_region_edited)
        self._previewing = False
        self._calibrating = False
        self._paused = False
        self._bridge = _Bridge()
        self._bridge.models.connect(self._on_models)

        self._build()
        self.refresh_sources()
        self._update_region_label()
        self._hotkeys: HotkeyManager | None = None
        if QGuiApplication.platformName() == "windows":  # not offscreen (tests, CI)
            self._hotkeys = HotkeyManager(int(self.winId()))
            self._hotkeys.triggered.connect(self.on_hotkey)
            failed = self._hotkeys.register_all(self._config.hotkeys.model_dump())
            if failed:
                self.statusBar().showMessage(t("hotkey.failed", keys=", ".join(failed)), 15000)

        self._poll_timer = QTimer(self, interval=30, timeout=self._poll)
        self._poll_timer.start()
        # The overlay follows its source: windows move, a region can be edited.
        self._follow_timer = QTimer(self, interval=FOLLOW_MS, timeout=self._follow)
        self._follow_timer.start()
        self._save_timer = QTimer(self, interval=400, singleShot=True, timeout=self._save)
        self._ollama_timer = QTimer(self, interval=OLLAMA_CHECK_MS, timeout=self.check_ollama)
        self._ollama_timer.start()
        self.check_ollama()
        self._engine.start()

    # --- layout ----------------------------------------------------------------------------------

    def _build(self) -> None:
        live = QWidget()
        layout = QVBoxLayout(live)
        grid = QGridLayout()

        self.source_combo = QComboBox()
        self.source_combo.setPlaceholderText(t("source.none"))
        refresh = QPushButton(t("source.refresh"), clicked=self.refresh_sources)
        grid.addWidget(QLabel(t("source.label")), 0, 0)
        grid.addWidget(self.source_combo, 0, 1)
        grid.addWidget(refresh, 0, 2)

        self.region_label = QLabel()
        self.region_button = QPushButton(t("region.select"), clicked=self.select_region)
        grid.addWidget(QLabel(" "), 1, 0)
        grid.addWidget(self.region_label, 1, 1)
        grid.addWidget(self.region_button, 1, 2)

        self.model_combo = QComboBox()
        self.ollama_label = QLabel(t("ollama.checking"))
        self.ollama_button = QPushButton(t("ollama.start"), clicked=self._start_ollama)
        self.ollama_button.hide()
        grid.addWidget(QLabel(t("model.label")), 2, 0)
        grid.addWidget(self.model_combo, 2, 1)
        grid.addWidget(self.ollama_label, 2, 2)
        grid.addWidget(self.ollama_button, 3, 2)
        grid.setColumnStretch(1, 1)
        layout.addLayout(grid)

        self.start_button = QPushButton(t("start"), clicked=self.toggle)
        self.start_button.setMinimumHeight(48)
        font = QFont(self.start_button.font())
        font.setPointSize(14)
        font.setBold(True)
        self.start_button.setFont(font)
        self.calibrate_button = QPushButton(
            t("calibration.button"), checkable=True, clicked=self.set_calibration
        )
        self.calibrate_button.setMinimumHeight(48)
        buttons = QHBoxLayout()
        buttons.addWidget(self.start_button, 3)
        buttons.addWidget(self.calibrate_button, 1)
        layout.addLayout(buttons)

        self.state_label = QLabel(t("state.idle"))
        layout.addWidget(self.state_label)

        self.feed = QTableWidget(0, 6)
        self.feed.setHorizontalHeaderLabels(
            [
                t(k)
                for k in (
                    "feed.time",
                    "feed.english",
                    "feed.turkish",
                    "feed.model",
                    "feed.ocr_ms",
                    "feed.translate_ms",
                )
            ]
        )
        header = self.feed.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.feed.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.feed.setWordWrap(True)
        self.feed.verticalHeader().hide()
        layout.addWidget(self.feed, 1)

        self.appearance = AppearanceTab(self._profile.overlay)
        self.appearance.changed.connect(self._on_appearance_changed)
        self.appearance.preview_toggled.connect(self._on_preview)

        self.tabs = QTabWidget()
        self.tabs.addTab(live, t("tab.live"))
        self.tabs.addTab(self.appearance, t("tab.appearance"))
        self.setCentralWidget(self.tabs)

    # --- profile ---------------------------------------------------------------------------------

    def _load_profile(self) -> Profile:
        try:
            return self._store.load(PROFILE_ID)
        except (FileNotFoundError, ValueError):
            return Profile(
                name="Varsayılan", vision=VisionSettings(text_colors=DEFAULT_TEXT_COLORS)
            )

    def _save(self) -> None:
        self._store.save(self._profile, PROFILE_ID)
        self._config.last_profile = PROFILE_ID
        save_config(self._config)

    # --- source & region -------------------------------------------------------------------------

    def refresh_sources(self) -> None:
        self._sources = available_sources()
        self.source_combo.clear()
        for option in self._sources:
            self.source_combo.addItem(option.label)
        self.source_combo.setCurrentIndex(self._index_of(self._profile.source))

    def _index_of(self, spec: Source | None) -> int:
        # Never fall back to some other window: a wrong source silently reads nothing.
        if spec is None:
            return -1
        if isinstance(spec, WindowSource):
            windows = [o.window for o in self._sources if o.window is not None]
            match = win32.best_window_match(windows, spec.exe_name, spec.title)
            if match is not None:
                return next(i for i, o in enumerate(self._sources) if o.window is match)
        for i, option in enumerate(self._sources):
            if option.spec == spec:
                return i
        return -1

    def selected_source(self) -> SourceOption | None:
        index = self.source_combo.currentIndex()
        return self._sources[index] if 0 <= index < len(self._sources) else None

    def select_region(self) -> None:
        """Dim every monitor; the user marks the subtitle wherever it is shown."""
        picker = RegionPicker()
        picker.selected.connect(self._on_picked)
        picker.show()
        self._picker = picker  # keep a reference while it is open

    def _on_picked(self, logical: Rect) -> None:
        physical = screens.to_physical(logical, screens.current_screens())
        windows = [
            (w, area)
            for w in win32.list_windows(exclude_pid=os.getpid())
            if (area := visible_client_area(w.hwnd)) is not None
        ]
        pick = resolve(physical, windows, win32.list_monitors())
        if pick is None:
            self.statusBar().showMessage(t("error.region_needs_screen"), 5000)
            return
        source_changed = pick.source != self._profile.source
        self._profile.source = pick.source
        self.refresh_sources()
        self.set_region(pick.region, restart=source_changed)

    def set_region(self, region: NormRect, *, restart: bool = False) -> None:
        self._profile.region = region
        self._update_region_label()
        self._save()
        if self._state in ACTIVE_STATES:
            if restart:  # another window or monitor: the pipeline must reopen its source
                self._send_start()
            else:
                self._engine.send(SetRegion(region))
                self._place_overlay()

    def _update_region_label(self) -> None:
        r, source = self._profile.region, self._profile.source
        if r is None or source is None:
            self.region_label.setText(t("region.none"))
            return
        name = source.title if isinstance(source, WindowSource) else source.device_name
        self.region_label.setText(
            t("region.selected", source=name[:70], x=r.x, y=r.y, w=r.width, h=r.height)
        )

    # --- Ollama ----------------------------------------------------------------------------------

    def check_ollama(self) -> None:
        url = self._config.ollama.url or DEFAULT_URL
        threading.Thread(
            target=lambda: self._bridge.models.emit(list_models(url)), daemon=True
        ).start()

    def _on_models(self, models: list[InstalledModel] | None) -> None:
        if models is None:
            self.ollama_label.setText(t("ollama.offline"))
            self.ollama_button.show()
            return
        self.ollama_label.setText(t("ollama.connected"))
        self.ollama_button.hide()
        names = [m.name for m in models]
        current = self.model_combo.currentText() or self._config.ollama.gpu_model
        if names != [self.model_combo.itemText(i) for i in range(self.model_combo.count())]:
            self.model_combo.clear()
            self.model_combo.addItems(names)
        if current in names:
            self.model_combo.setCurrentText(current)

    def _start_ollama(self) -> None:
        if not start_ollama():
            self.ollama_label.setText(t("ollama.not_installed"))

    # --- start / stop ----------------------------------------------------------------------------

    def toggle(self) -> None:
        if self._state in ACTIVE_STATES:
            self._engine.send(StopPipeline())
            return
        option = self.selected_source()
        if option is None:
            self.statusBar().showMessage(t("error.no_source"), 5000)
            return
        if self._profile.region is None:
            self.statusBar().showMessage(t("error.no_region"), 5000)
            return
        self._profile.source = option.spec
        self._send_start()

    def _send_start(self) -> None:
        if self.model_combo.currentText():
            self._config.ollama.gpu_model = self.model_combo.currentText()
        self._save()
        self._reads = 0
        self._engine.send(
            StartPipeline(
                profile=self._profile.model_copy(deep=True),
                ollama=self._config.ollama,
                profile_id=PROFILE_ID,
            )
        )
        self.start_button.setText(t("stop"))

    # --- engine events ---------------------------------------------------------------------------

    def _poll(self) -> None:
        for event in self._engine.poll():
            self.handle(event)

    def handle(self, event: Event) -> None:
        match event:
            case StateChanged(state=state, detail=detail):
                self._set_state(state, detail)
            case EngineError(kind=kind, message=message):
                self.statusBar().showMessage(t(f"error.{kind}", detail=message), 15000)
            case FrameInfo():
                self._frame = event
                self._place_overlay()
            case SubtitleRead():
                self._on_read(event)
            case SubtitleTranslated():
                self._on_translated(event)
            case SubtitleCleared():
                self._schedule_clear()

    def _set_state(self, state: EngineState, detail: str) -> None:
        self._state = state
        self._paused = state == EngineState.PAUSED
        text = t(f"state.{state}", model=detail)
        if state == EngineState.RUNNING and self._reads == 0:
            text = t("state.waiting")
        self.state_label.setText(text)
        self.start_button.setText(t("stop") if state in ACTIVE_STATES else t("start"))
        if state in (EngineState.PAUSED, EngineState.STOPPED) and not self._calibrating:
            self._overlay.clear()
        if state == EngineState.STOPPED and not (self._calibrating or self._previewing):
            self._overlay.hide()

    # --- hotkeys and calibration mode ------------------------------------------------------------

    def on_hotkey(self, action: str) -> None:
        match action:
            case "calibration":
                self.set_calibration(not self._calibrating)
            case "toggle_overlay":
                self.toggle_overlay()
            case "pause":
                if self._state in ACTIVE_STATES:
                    self._engine.send(SetPaused(not self._paused))

    def toggle_overlay(self) -> None:
        hidden = not self._overlay.suppressed
        self._overlay.suppressed = hidden
        if hidden:
            self._overlay.hide()
        elif self._overlay.text:
            self._overlay.show()
        self.statusBar().showMessage(t("overlay.hidden" if hidden else "overlay.shown"), 4000)

    def set_calibration(self, on: bool) -> None:
        """Calibration mode: the overlay and the region frame can be moved and resized."""
        self._calibrating = on
        self.calibrate_button.setChecked(on)
        self._overlay.suppressed = False
        self._overlay.set_editable(on)
        if on:
            self._place_region_frame()
            self._region_frame.show()
            self._place_overlay()
            self.statusBar().showMessage(t("calibration.on"))
        else:
            self._region_frame.hide()
            self._save_timer.start()
            if self._state not in ACTIVE_STATES and not self._previewing:
                self._overlay.hide()
            self.statusBar().showMessage(t("calibration.off"), 4000)

    def _place_region_frame(self) -> None:
        area, region = self._source_area(), self._profile.region
        if area is None or region is None:
            self._region_frame.hide()
            return
        physical = region.to_pixels(area.width, area.height).translated(area.x, area.y)
        self._region_frame.place(screens.to_logical(physical, screens.current_screens()))

    def _on_region_edited(self, logical: Rect) -> None:
        area = self._source_area()
        if area is None:
            return
        physical = screens.to_physical(logical, screens.current_screens())
        try:
            region = NormRect.from_pixels(
                physical.translated(-area.x, -area.y), area.width, area.height
            )
        except ValueError:  # dragged off the source entirely: snap back
            self._place_region_frame()
            return
        self.set_region(region)
        self._place_region_frame()  # clipped to the source

    # --- overlay placement and appearance --------------------------------------------------------

    def _source_area(self) -> Rect | None:
        """Where the source's frame is on screen right now (physical); None for video devices."""
        option = self.selected_source()
        if option is not None and option.spec == self._profile.source:
            return option.area()
        if self._frame is not None and self._frame.screen_rect is not None:
            return Rect(*self._frame.screen_rect)
        return None

    def _follow(self) -> None:
        self._place_overlay()
        if self._calibrating:
            self._place_region_frame()

    def _place_overlay(self) -> None:
        active = self._state in ACTIVE_STATES
        if not (active or self._previewing or self._overlay.editable):
            return
        s = self._profile.overlay
        monitors = win32.list_monitors()
        area, region = self._source_area(), self._profile.region
        if s.placement == OverlayPlacement.CUSTOM or area is None or region is None:
            monitor = next(
                (m for m in monitors if m.device_name == s.monitor_device),
                next(m for m in monitors if m.is_primary),
            )
            physical = custom_geometry(s.rect, monitor.rect)
        else:
            subtitle = region.to_pixels(area.width, area.height).translated(area.x, area.y)
            center = (subtitle.x + subtitle.width / 2, subtitle.y + subtitle.height / 2)
            monitor = monitor_at(monitors, *center)
            height = round(self._overlay.preferred_height() * monitor.scale)
            physical = overlay_geometry(s.placement, subtitle, monitor.rect, height)
        self._overlay.align = TEXT_ALIGN[s.placement]
        self._overlay.place(screens.to_logical(physical, screens.current_screens()))

    def _on_appearance_changed(self) -> None:
        self._overlay.update()
        self._place_overlay()
        self._save_timer.start()  # debounced: sliders send many changes

    def _on_preview(self, on: bool) -> None:
        self._previewing = on
        if on:
            if not self._overlay.text:
                self._overlay.show_text(t("overlay.sample"), t("overlay.sample_original"))
            self._place_overlay()
        elif self._overlay.text == t("overlay.sample"):
            self._overlay.clear()
            if self._state not in ACTIVE_STATES and not self._calibrating:
                self._overlay.hide()

    def _on_overlay_edited(self, logical: Rect) -> None:
        """Moved or resized in calibration mode: from now on it stays where the user put it."""
        physical = screens.to_physical(logical, screens.current_screens())
        monitor = monitor_at(
            win32.list_monitors(),
            physical.x + physical.width / 2,
            physical.y + physical.height / 2,
        )
        s = self._profile.overlay
        s.monitor_device = monitor.device_name
        s.rect = NormRect.from_pixels(
            physical.translated(-monitor.rect.x, -monitor.rect.y),
            monitor.rect.width,
            monitor.rect.height,
        )
        s.placement = OverlayPlacement.CUSTOM
        self.appearance.refresh()
        self._save_timer.start()

    def _row_for(self, line_id: int) -> int:
        if line_id in self._rows:
            return self._rows[line_id]
        if self.feed.rowCount() >= FEED_LIMIT:
            self.feed.removeRow(self.feed.rowCount() - 1)
            self._rows = {k: v for k, v in self._rows.items() if v < FEED_LIMIT - 1}
        self.feed.insertRow(0)
        self._rows = {k: v + 1 for k, v in self._rows.items()}
        self._rows[line_id] = 0
        return 0

    def _set_cells(self, row: int, values: dict[int, str]) -> None:
        for column, value in values.items():
            self.feed.setItem(row, column, QTableWidgetItem(value))

    def _on_read(self, event: SubtitleRead) -> None:
        """A line was read. The overlay keeps the previous translation until this line's own
        arrives: a blank overlay while the model works looked like subtitles vanishing early."""
        self._clear_generation += 1  # a subtitle is up: cancel a pending clear
        if self._reads == 0 and self._state == EngineState.RUNNING:
            self.state_label.setText(t("state.running"))
        self._reads += 1
        if event.repeat:
            if (text := self._translations.get(event.line_id)) is not None:
                self._overlay.show_text(text)
            return
        row = self._row_for(event.line_id)
        self._set_cells(
            row,
            {
                0: time.strftime("%H:%M:%S"),
                1: event.text,
                2: t("feed.pending"),
                4: f"{event.ocr_ms:.0f}",
            },
        )

    def _on_translated(self, event: SubtitleTranslated) -> None:
        self._translations[event.line_id] = event.translation
        row = self._row_for(event.line_id)
        timing = t("feed.cached") if event.cached else f"{event.translate_ms:.0f}"
        self._set_cells(row, {2: event.translation, 3: event.model, 5: timing})
        self.feed.resizeRowToContents(row)
        self._overlay.show_text(event.translation, event.source)

    def _schedule_clear(self) -> None:
        self._clear_generation += 1
        generation = self._clear_generation

        def clear() -> None:
            if generation == self._clear_generation:
                self._overlay.clear()

        QTimer.singleShot(self._profile.overlay.clear_delay_ms, clear)

    # --- shutdown --------------------------------------------------------------------------------

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt override
        self._poll_timer.stop()
        self._ollama_timer.stop()
        self._follow_timer.stop()
        if self._save_timer.isActive():
            self._save_timer.stop()
            self._save()
        if self._hotkeys is not None:
            self._hotkeys.close()
        self._region_frame.close()
        self._overlay.close()
        self._engine.shutdown()
        super().closeEvent(event)
