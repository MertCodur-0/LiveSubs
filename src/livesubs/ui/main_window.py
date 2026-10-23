"""Main window (minimal, step 9): source, region, model, Start/Stop, live feed, overlay.

Talks to the engine process only through messages; polls its events every 30 ms on the UI thread.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from PySide6.QtCore import QCoreApplication, QEvent, QObject, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QCloseEvent, QDesktopServices, QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from livesubs import APP_NAME, __version__
from livesubs.core import paths
from livesubs.core.config import AppConfig, load_config, save_config
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
    Source,
    VisionSettings,
    WindowSource,
)
from livesubs.engine.translate.cache import TranslationCache
from livesubs.i18n import t
from livesubs.ollama.client import DEFAULT_URL, InstalledModel, list_models, start_ollama
from livesubs.platform import win32
from livesubs.ui import screens
from livesubs.ui import theme as themes
from livesubs.ui.header import Header
from livesubs.ui.hotkeys import HotkeyManager
from livesubs.ui.icon import app_icon
from livesubs.ui.inspector import Inspector
from livesubs.ui.overlay import Overlay
from livesubs.ui.pick import resolve
from livesubs.ui.placement import custom_geometry, monitor_at, overlay_geometry
from livesubs.ui.region_frame import RegionFrame
from livesubs.ui.region_select import RegionPicker
from livesubs.ui.setup_check import SetupDialog
from livesubs.ui.tabs.appearance import AppearanceTab
from livesubs.ui.tabs.models import ModelsTab, cpu_share
from livesubs.ui.tabs.profiles import ProfilesTab
from livesubs.ui.tabs.settings import SettingsTab
from livesubs.ui.tray import Tray
from livesubs.web.server import WebServer

log = logging.getLogger(__name__)

DEFAULT_PROFILE_ID = "varsayilan"
FEED_LIMIT = 200
OLLAMA_CHECK_MS = 5000
FOLLOW_MS = 300
FULLSCREEN_CHECK_MS = 3000
WEB_STATUS_MS = 2000
ENGINE_CHECK_MS = 1000
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
SETUP_ERRORS = (ErrorKind.OCR_UNAVAILABLE, ErrorKind.MODEL_NOT_FOUND)
# Games mostly use white subtitles, some speakers yellow.
DEFAULT_TEXT_COLORS = ["#FFFFFF", "#FFFF00"]


class Engine(Protocol):
    @property
    def alive(self) -> bool: ...
    def start(self) -> None: ...
    def restart(self) -> None: ...
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
    spill = Signal(str, float)  # model, part of it that runs on the CPU


class MainWindow(QMainWindow):
    def __init__(
        self, engine: Engine, *, config: AppConfig | None = None, store: ProfileStore | None = None
    ) -> None:
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} {__version__}")
        self.setWindowIcon(app_icon())
        self.resize(1000, 720)
        self._engine = engine
        self._config = config or load_config()
        self._store = store or ProfileStore()
        self._profile_id, self._profile = self._initial_profile()
        self._theme = themes.apply_theme(self._config.theme)
        self._sources: list[SourceOption] = []
        self._state = EngineState.IDLE
        self._frame: FrameInfo | None = None
        self._rows: dict[int, int] = {}
        self._translations: dict[int, str] = {}
        self._clear_generation = 0
        self._reads = 0  # lines read since Start: tells "running" from "waiting for subtitles"
        self._overlay = Overlay(self._profile.overlay)
        self._overlay.edited.connect(self._on_overlay_edited)
        self._overlay.set_capture_visible(self._config.recording_mode)
        self._warned_overlap = False
        self._warned_fullscreen = False
        self._warned_spill = False
        self._region_frame = RegionFrame()
        self._region_frame.edited.connect(self._on_region_edited)
        self._inspector = Inspector(self._profile.vision)
        self._inspector.vision_changed.connect(self._on_vision_changed)
        self._previewing = False
        self._calibrating = False
        self._paused = False
        self._bridge = _Bridge()
        self._bridge.models.connect(self._on_models)
        self._bridge.spill.connect(self._on_spill)
        self._discard_changes = False  # settings were imported: never write the old ones back
        self.setup = SetupDialog(self._config, self)
        self.setup.model_selected.connect(self._on_model_selected)
        self.setup.settings_imported.connect(self._on_settings_imported)

        self.tray = Tray(app_icon())
        self.tray.show_window.connect(self.show_from_tray)
        self.tray.toggle_running.connect(self.toggle)
        self.tray.toggle_overlay.connect(self.toggle_overlay)
        self.tray.quit.connect(self.close)
        self.tray.show()

        self._build()
        self.refresh_sources()
        self._update_region_label()
        self._hotkeys: HotkeyManager | None = None
        if QGuiApplication.platformName() == "windows":  # not offscreen (tests, CI)
            self._hotkeys = HotkeyManager(int(self.winId()))
            self._hotkeys.triggered.connect(self.on_hotkey)
            self._register_hotkeys()

        self._poll_timer = QTimer(self, interval=30, timeout=self._poll)
        self._poll_timer.start()
        self._engine_timer = QTimer(self, interval=ENGINE_CHECK_MS, timeout=self._check_engine)
        self._engine_timer.start()
        # The overlay follows its source: windows move, a region can be edited.
        self._follow_timer = QTimer(self, interval=FOLLOW_MS, timeout=self._follow)
        self._follow_timer.start()
        self._save_timer = QTimer(self, interval=400, singleShot=True, timeout=self._save)
        self._ollama_timer = QTimer(self, interval=OLLAMA_CHECK_MS, timeout=self.check_ollama)
        self._ollama_timer.start()
        self._fullscreen_timer = QTimer(
            self, interval=FULLSCREEN_CHECK_MS, timeout=self._check_fullscreen
        )
        self._fullscreen_timer.start()
        self._web: WebServer | None = None
        self._web_timer = QTimer(self, interval=WEB_STATUS_MS, timeout=self._show_web_status)
        self._apply_web()
        self._apply_display()
        self.check_ollama()
        self._engine.start()

    # --- layout ----------------------------------------------------------------------------------

    def _build(self) -> None:
        live = QWidget()
        layout = QVBoxLayout(live)
        grid = QGridLayout()

        self.profile_combo = QComboBox()
        self.profile_combo.activated.connect(
            lambda i: self.switch_profile(self.profile_combo.itemData(i))
        )
        grid.addWidget(QLabel(t("profile.label")), 0, 0)
        grid.addWidget(self.profile_combo, 0, 1)

        self.source_combo = QComboBox()
        self.source_combo.setPlaceholderText(t("source.none"))
        refresh = QPushButton(t("source.refresh"), clicked=self.refresh_sources)
        grid.addWidget(QLabel(t("source.label")), 1, 0)
        grid.addWidget(self.source_combo, 1, 1)
        grid.addWidget(refresh, 1, 2)

        self.region_label = QLabel()
        self.region_button = QPushButton(t("region.select"), clicked=self.select_region)
        grid.addWidget(QLabel(" "), 2, 0)
        grid.addWidget(self.region_label, 2, 1)
        grid.addWidget(self.region_button, 2, 2)

        self.model_combo = QComboBox()
        self.model_combo.activated.connect(self._on_model_combo)
        self.ollama_label = QLabel(t("ollama.checking"))
        self.ollama_button = QPushButton(t("ollama.start"), clicked=self._start_ollama)
        self.ollama_button.hide()
        self.setup_button = QPushButton(t("setup.button"), clicked=self.open_setup)
        grid.addWidget(QLabel(t("model.label")), 3, 0)
        grid.addWidget(self.model_combo, 3, 1)
        grid.addWidget(self.ollama_label, 3, 2)
        ollama_buttons = QHBoxLayout()
        ollama_buttons.addWidget(self.ollama_button)
        ollama_buttons.addWidget(self.setup_button)
        grid.addLayout(ollama_buttons, 4, 2)
        grid.setColumnStretch(1, 1)
        layout.addLayout(grid)

        self.start_button = QPushButton(t("start"), clicked=self.toggle, objectName="startButton")
        self.start_button.setMinimumHeight(52)
        themes.set_glow(self.start_button, self._theme)
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

        self.settings = SettingsTab(self._config, self._profile)
        self.settings.config_changed.connect(self._save_later)
        self.settings.profile_changed.connect(self._save_later)
        self.settings.hotkeys_changed.connect(lambda: self._register_hotkeys(changed=True))
        self.settings.recording_changed.connect(self._on_recording_changed)
        self.settings.web_changed.connect(self._apply_web)
        self.settings.display_changed.connect(self._on_display_changed)
        self.settings.theme_changed.connect(self.set_theme)
        self.settings.clear_cache_requested.connect(self.clear_cache)
        self.settings.open_logs_requested.connect(self._open_logs)

        self.models = ModelsTab(self._config, self._profile)
        self.models.model_selected.connect(self._on_model_selected)
        self.models.models_changed.connect(self.check_ollama)
        self.models.start_ollama_requested.connect(self._start_ollama)
        self.models.setup_requested.connect(self.open_setup)

        self.tabs = QTabWidget()
        self.tabs.addTab(live, t("tab.live"))
        self.tabs.addTab(self.models, t("tab.models"))
        self.profiles = ProfilesTab(self._profile)
        self.profiles.changed.connect(self._on_profile_edited)
        self.profiles.new_requested.connect(self.new_profile)
        self.profiles.copy_requested.connect(self.copy_profile)
        self.profiles.rename_requested.connect(self.rename_profile)
        self.profiles.delete_requested.connect(self.delete_profile)

        # Long tabs scroll instead of squeezing their rows on short screens.
        for page, title in (
            (self.appearance, "tab.appearance"),
            (self.profiles, "tab.profiles"),
            (self.settings, "tab.settings"),
        ):
            scroll = QScrollArea(widgetResizable=True, frameShape=QScrollArea.Shape.NoFrame)
            scroll.setWidget(page)
            self.tabs.addTab(scroll, t(title))
        self._fill_profile_combo()

        self.header = Header(self._theme)
        central = QWidget()
        column = QVBoxLayout(central)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)
        column.addWidget(self.header)
        body = QVBoxLayout()
        body.setContentsMargins(10, 8, 10, 4)
        body.addWidget(self.tabs)
        column.addLayout(body, 1)
        self.setCentralWidget(central)

    # --- settings tab, tray and warnings ----------------------------------------------------------

    def _save_later(self) -> None:
        self._save_timer.start()  # debounced: spin boxes send many changes

    def _register_hotkeys(self, *, changed: bool = False) -> None:
        if self._hotkeys is None:
            return
        failed = self._hotkeys.register_all(self._config.hotkeys.model_dump())
        self.settings.show_hotkey_result(failed, changed=changed)
        if failed:
            self.statusBar().showMessage(t("hotkey.failed", keys=", ".join(failed)), 15000)

    def set_theme(self, name: str) -> None:
        self._theme = themes.apply_theme(name)
        self.header.set_theme(self._theme)
        themes.set_glow(self.start_button, self._theme)

    def _on_recording_changed(self, on: bool) -> None:
        self._overlay.set_capture_visible(on)
        self._warned_overlap = False
        self._place_overlay()

    # --- phone (web) output -----------------------------------------------------------------------

    def _apply_web(self) -> None:
        """Run the web server when it is on, on the configured port."""
        web = self._config.web
        if self._web is not None and (not web.enabled or self._web.port != web.port):
            self._web.stop()
            self._web = None
        if web.enabled and self._web is None:
            server = WebServer(web.port)
            try:
                server.start()
            except OSError as exc:
                self.settings.show_web(
                    None, error=t("settings.web_failed", port=web.port, detail=exc)
                )
                self.statusBar().showMessage(t("settings.web_failed", port=web.port, detail=exc))
                return
            self._web = server
        if self._web is None:
            self._web_timer.stop()
        else:
            self._web_timer.start()
        self._show_web_status()

    def _show_web_status(self) -> None:
        if self._web is None:
            if not self._config.web.enabled:
                self.settings.show_web(None)
            return
        self.settings.show_web(self._web.url, self._web.client_count)

    def _web_only(self) -> bool:
        return self._profile.overlay.display == OverlayDisplay.WEB_ONLY

    def _apply_display(self) -> None:
        """Phone only: the PC overlay stays hidden and the web output must be on."""
        web_only = self._web_only()
        self._overlay.set_disabled(web_only)
        if web_only and not self._config.web.enabled:
            self.settings.web_enabled.setChecked(True)  # turns the server on
        in_use = self._state in ACTIVE_STATES or self._previewing or self._calibrating
        if in_use and not web_only and self._overlay.text and not self._overlay.suppressed:
            self._overlay.show()
        self._place_overlay()

    def _on_display_changed(self) -> None:
        self._apply_display()
        self.profiles.refresh()
        self.appearance.refresh()

    def clear_cache(self) -> None:
        """The engine opens the cache per start, so it is free to clear while stopped."""
        if self._state in ACTIVE_STATES:
            self.settings.upkeep_status.setText(t("settings.cache_busy"))
            return
        cache = TranslationCache(paths.translation_cache_file())
        try:
            cache.clear()
        finally:
            cache.close()
        self.settings.upkeep_status.setText(t("settings.cache_cleared"))

    def _open_logs(self) -> None:
        folder = paths.log_dir()
        folder.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def show_from_tray(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def changeEvent(self, event: QEvent) -> None:  # noqa: N802 - Qt override
        super().changeEvent(event)
        minimized = event.type() == QEvent.Type.WindowStateChange and self.isMinimized()
        if minimized and self._config.minimize_to_tray and self.tray.icon.isVisible():
            QTimer.singleShot(0, self.hide)  # hiding inside the state change confuses Windows
            self.tray.notify_minimized()

    def _check_fullscreen(self) -> None:
        """An exclusive-fullscreen game draws over every window, the overlay included."""
        fullscreen = self._state in ACTIVE_STATES and win32.is_exclusive_fullscreen_running()
        if fullscreen and not self._warned_fullscreen:
            self.statusBar().showMessage(t("warning.fullscreen"), 30000)
            self.tray.notify(APP_NAME, t("warning.fullscreen"))
        self._warned_fullscreen = fullscreen

    def _check_overlap(self, overlay: Rect, subtitle: Rect | None) -> None:
        """Recording mode: an overlay over the subtitle region would be read back by OCR."""
        overlap = (
            self._config.recording_mode and subtitle is not None and overlay.intersects(subtitle)
        )
        if overlap and not self._warned_overlap:
            self.statusBar().showMessage(t("warning.recording_overlap"), 15000)
        self._warned_overlap = overlap

    # --- profile ---------------------------------------------------------------------------------

    @staticmethod
    def _blank_profile(name: str) -> Profile:
        return Profile(name=name, vision=VisionSettings(text_colors=DEFAULT_TEXT_COLORS))

    def _initial_profile(self) -> tuple[str, Profile]:
        """The profile used last, else any saved one, else a new default profile."""
        profiles = self._store.load_all()
        last = self._config.last_profile
        if last in profiles:
            return last, profiles[last]
        if profiles:
            first = min(profiles)
            return first, profiles[first]
        return DEFAULT_PROFILE_ID, self._blank_profile("Varsayılan")

    def _save(self) -> None:
        if self._discard_changes:
            return
        self._store.save(self._profile, self._profile_id)
        self._config.last_profile = self._profile_id
        save_config(self._config)

    def _fill_profile_combo(self) -> None:
        names = {pid: p.name for pid, p in self._store.load_all().items()}
        names[self._profile_id] = self._profile.name  # also before its first save
        self.profile_combo.clear()
        for profile_id, name in sorted(names.items(), key=lambda item: item[1].casefold()):
            self.profile_combo.addItem(name, profile_id)
        self.profile_combo.setCurrentIndex(self.profile_combo.findData(self._profile_id))

    def switch_profile(self, profile_id: str) -> None:
        """Make another saved profile the active one; a running pipeline follows it."""
        if profile_id == self._profile_id:
            return
        try:
            profile = self._store.load(profile_id)
        except (FileNotFoundError, ValueError) as exc:
            self.statusBar().showMessage(t("error.internal", detail=exc), 8000)
            self._fill_profile_combo()
            return
        self._flush_save()
        self._activate(profile_id, profile)

    def _activate(self, profile_id: str, profile: Profile) -> None:
        was_active = self._state in ACTIVE_STATES
        self._profile_id, self._profile = profile_id, profile
        self._save()
        self._overlay.settings = profile.overlay
        self._overlay.clear()
        self._overlay.update()
        self._inspector.set_vision(profile.vision)
        self.appearance.set_settings(profile.overlay)
        self.settings.set_profile(profile)
        self.profiles.set_profile(profile)
        self._apply_display()
        self.models.set_profile(profile)
        self._fill_profile_combo()
        self.refresh_sources()
        self._update_region_label()
        if was_active:
            if self.selected_source() is not None and profile.region is not None:
                self._send_start()
            else:
                self._engine.send(StopPipeline())
        self._place_overlay()
        if self._calibrating:
            self._place_region_frame()

    def _flush_save(self) -> None:
        """Save now, also a profile never saved yet (the default one before its first edit)."""
        self._save_timer.stop()
        self._save()

    def new_profile(self, name: str) -> None:
        self._flush_save()
        self._activate(self._store.new_id(name), self._blank_profile(name))

    def copy_profile(self, name: str) -> None:
        self._flush_save()
        copy = self._profile.model_copy(deep=True)
        copy.name = name
        self._activate(self._store.new_id(name), copy)

    def rename_profile(self, name: str) -> None:
        """The id (file name) stays: it only has to be unique, not match the name."""
        self._profile.name = name
        self._save()
        self.profiles.refresh()
        self._fill_profile_combo()

    def delete_profile(self) -> None:
        others = sorted(set(self._store.ids()) - {self._profile_id})
        if not others:
            self.statusBar().showMessage(t("profiles.last_one"), 5000)
            return
        self._save_timer.stop()
        self._store.delete(self._profile_id)
        self._activate(others[0], self._store.load(others[0]))

    def _on_profile_edited(self) -> None:
        self._save_timer.start()
        self.settings.refresh_profile()
        self._apply_display()  # the profile may show translations on the phone only now
        self._place_overlay()  # the display monitor may have changed
        self.appearance.refresh()
        self.models.set_profile(self._profile)  # the game's VRAM reserve may have changed

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
        # While translating: is the model slowed down by running partly on the CPU?
        watch = self._state == EngineState.RUNNING and not self._warned_spill
        model = self._config.ollama.gpu_model if watch else ""

        def work() -> None:
            self._bridge.models.emit(list_models(url))
            if model and (share := cpu_share(url, model)) is not None:
                self._bridge.spill.emit(model, share)

        threading.Thread(target=work, daemon=True).start()

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

    def _on_spill(self, model: str, cpu: float) -> None:
        if self._warned_spill or self._state not in ACTIVE_STATES:
            return
        self._warned_spill = True  # once per Start
        text = t("warning.model_spill", model=model, percent=round((1 - cpu) * 100))
        self.statusBar().showMessage(text, 30000)

    def _on_model_combo(self, index: int) -> None:
        self._config.ollama.gpu_model = self.model_combo.itemText(index)
        self._save()
        self.models.mark_in_use()

    def _start_ollama(self) -> None:
        if not start_ollama():
            self.ollama_label.setText(t("ollama.not_installed"))
            self.open_setup()

    # --- setup -----------------------------------------------------------------------------------

    def run_setup_check(self) -> None:
        """At startup: open the setup dialog when OCR, Ollama or the model is missing."""
        self.setup.check_on_startup()

    def open_setup(self) -> None:
        self.setup.refresh()
        self.setup.show()
        self.setup.raise_()
        self.setup.activateWindow()

    def _on_model_selected(self, name: str) -> None:
        self._config.ollama.gpu_model = name
        self._save()
        self.models.mark_in_use()
        self.model_combo.clear()  # refilled by the check with the new model selected
        self.check_ollama()

    def _on_settings_imported(self) -> None:
        self._discard_changes = True
        answer = QMessageBox.question(self.setup, t("setup.restart.title"), t("setup.restart.text"))
        if answer == QMessageBox.StandardButton.Yes:
            self.restart()

    def restart(self) -> None:
        """Start a new instance (same interpreter and arguments) and close this one."""
        subprocess.Popen([sys.executable, *sys.orig_argv[1:]], close_fds=True)
        self.close()

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
        self._warned_spill = False
        if self._web is not None:
            self._web.new_session()  # line ids start again
        self._engine.send(
            StartPipeline(
                profile=self._profile.model_copy(deep=True),
                ollama=self._config.ollama,
                profile_id=self._profile_id,
            )
        )
        if self._calibrating:
            self._engine.send(SetDebug(True))
        self._show_running(True)

    # --- engine events ---------------------------------------------------------------------------

    def _poll(self) -> None:
        for event in self._engine.poll():
            self.handle(event)

    def _check_engine(self) -> None:
        """A native crash (seen in windows-capture) kills the engine without a word: start a new
        one instead of waiting for it forever."""
        if self._engine.alive:
            return
        log.error("The engine process died; starting a new one")
        self._engine.restart()
        self._set_state(EngineState.STOPPED, "")
        self.statusBar().showMessage(t("error.engine_crashed"), 30000)
        self.tray.notify(APP_NAME, t("error.engine_crashed"))

    def handle(self, event: Event) -> None:
        match event:
            case StateChanged(state=state, detail=detail):
                self._set_state(state, detail)
            case EngineError(kind=kind, message=message):
                self.statusBar().showMessage(t(f"error.{kind}", detail=message), 15000)
                if kind in SETUP_ERRORS:  # missing on this PC: the setup dialog fixes it
                    self.open_setup()
            case FrameInfo():
                self._frame = event
                self._place_overlay()
            case SubtitleRead():
                self._on_read(event)
            case SubtitleTranslated():
                self._on_translated(event)
            case SubtitleCleared():
                self._schedule_clear()
            case DebugFrame():
                if self._inspector.isVisible():
                    self._inspector.show_frame(event)

    def _show_running(self, running: bool) -> None:
        self.start_button.setText(t("stop") if running else t("start"))
        if self.start_button.property("running") != running:
            self.start_button.setProperty("running", running)  # restyled by the theme
            themes.repolish(self.start_button)

    def _set_state(self, state: EngineState, detail: str) -> None:
        self._state = state
        self._paused = state == EngineState.PAUSED
        text = t(f"state.{state}", model=detail)
        if state == EngineState.RUNNING and self._reads == 0:
            text = t("state.waiting")
        self.state_label.setText(text)
        self._show_running(state in ACTIVE_STATES)
        self.tray.set_running(state in ACTIVE_STATES)
        self.models.set_busy(state in ACTIVE_STATES)
        if state in (EngineState.PAUSED, EngineState.STOPPED) and not self._calibrating:
            self._overlay.clear()
            if self._web is not None:
                self._web.clear()
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
        elif self._overlay.text and not self._overlay.disabled:
            self._overlay.show()
        self.statusBar().showMessage(t("overlay.hidden" if hidden else "overlay.shown"), 4000)

    def set_calibration(self, on: bool) -> None:
        """Calibration mode: the overlay and the region frame can be moved and resized."""
        self._calibrating = on
        self.calibrate_button.setChecked(on)
        self._overlay.suppressed = False
        self._overlay.set_editable(on)
        if self._state in ACTIVE_STATES:
            self._engine.send(SetDebug(on))
        if on:
            self._place_region_frame()
            self._region_frame.show()
            self._place_overlay()
            self._place_inspector()
            self._inspector.show()
            self.statusBar().showMessage(t("calibration.on"))
        else:
            self._region_frame.hide()
            self._inspector.hide()
            self._save_timer.start()
            if self._state not in ACTIVE_STATES and not self._previewing:
                self._overlay.hide()
            self.statusBar().showMessage(t("calibration.off"), 4000)

    def _place_inspector(self) -> None:
        """Open the Inspector on a monitor other than the game's, so it never covers the game."""
        monitors = win32.list_monitors()
        area = self._source_area()
        game = (
            monitor_at(monitors, area.x + area.width / 2, area.y + area.height / 2)
            if area is not None
            else next(m for m in monitors if m.is_primary)
        )
        target = next((m for m in monitors if m.device_name != game.device_name), game)
        corner = Rect(target.work_rect.x + 40, target.work_rect.y + 40, 1, 1)
        logical = screens.to_logical(corner, screens.current_screens())
        self._inspector.move(logical.x, logical.y)

    def _on_vision_changed(self) -> None:
        self._save_timer.start()
        if self._state in ACTIVE_STATES:
            self._engine.send(SetVision(self._profile.vision.model_copy(deep=True)))

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
        subtitle = (
            region.to_pixels(area.width, area.height).translated(area.x, area.y)
            if area is not None and region is not None
            else None
        )
        on_monitor = s.display == OverlayDisplay.MONITOR  # e.g. the PC screen beside the TV
        if on_monitor or s.placement == OverlayPlacement.CUSTOM or subtitle is None:
            monitor = next(
                (m for m in monitors if m.device_name == s.monitor_device),
                next(m for m in monitors if m.is_primary),
            )
            physical = custom_geometry(s.rect, monitor.rect)
        else:
            center = (subtitle.x + subtitle.width / 2, subtitle.y + subtitle.height / 2)
            monitor = monitor_at(monitors, *center)
            height = round(self._overlay.preferred_height() * monitor.scale)
            physical = overlay_geometry(s.placement, subtitle, monitor.rect, height)
        self._overlay.align = TEXT_ALIGN[s.placement]
        self._overlay.place(screens.to_logical(physical, screens.current_screens()))
        self._check_overlap(physical, subtitle)

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
        if s.display == OverlayDisplay.AUTO:
            s.placement = OverlayPlacement.CUSTOM
        self.appearance.refresh()
        self.profiles.refresh()  # it may have been dragged to another monitor
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
                if self._web is not None:
                    self._web.show(event.line_id, text)
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
        if self._web is not None:
            self._web.show(event.line_id, event.translation)

    def _schedule_clear(self) -> None:
        self._clear_generation += 1
        generation = self._clear_generation

        def clear() -> None:
            if generation == self._clear_generation:
                self._overlay.clear()
                if self._web is not None:
                    self._web.clear()

        QTimer.singleShot(self._profile.overlay.clear_delay_ms, clear)

    # --- shutdown --------------------------------------------------------------------------------

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt override
        self._poll_timer.stop()
        self._engine_timer.stop()
        self._ollama_timer.stop()
        self._follow_timer.stop()
        self._fullscreen_timer.stop()
        self._web_timer.stop()
        if self._web is not None:
            self._web.stop()
        self.tray.hide()
        self._flush_save()
        if self._hotkeys is not None:
            self._hotkeys.close()
        self.setup.shutdown()
        self.models.shutdown()
        self.setup.close()
        self._region_frame.close()
        self._inspector.close()
        self._overlay.close()
        self._engine.shutdown()
        super().closeEvent(event)
        # Qt quits when the last *visible* window closes; this one may be hidden in the tray, or
        # the setup dialog may still be open.
        QCoreApplication.quit()
