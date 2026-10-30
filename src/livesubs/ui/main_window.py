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
from collections import deque
from collections.abc import Callable, Collection
from dataclasses import dataclass
from typing import Protocol

from PySide6.QtCore import QCoreApplication, QEvent, QObject, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QCloseEvent, QDesktopServices, QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QComboBox,
    QDialog,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
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
    VideoDeviceSource,
    VisionSettings,
    WindowSource,
)
from livesubs.engine.translate.cache import TranslationCache
from livesubs.i18n import t
from livesubs.ollama.client import DEFAULT_URL, InstalledModel, list_models, start_ollama
from livesubs.platform import win32
from livesubs.platform.video_devices import list_video_devices
from livesubs.ui import icons, rows, screens
from livesubs.ui import theme as themes
from livesubs.ui.capture_card import CaptureCard, Host
from livesubs.ui.feed import FeedList
from livesubs.ui.frame_region import FrameRegionDialog
from livesubs.ui.header import Header
from livesubs.ui.hotkeys import HotkeyManager
from livesubs.ui.icon import app_icon
from livesubs.ui.inspector import Inspector
from livesubs.ui.overlay import Overlay
from livesubs.ui.pick import resolve, resolve_card
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
SIDEBAR_WIDTH = 200
SHEET_WIDTH = 560
AVERAGE_OF = 20  # translations in the state line's mean
NAV_ICON_PX = 18
PAGE_MARGINS = (28, 22, 28, 14)
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


def other_windows(own_pids: Collection[int] = ()) -> list[win32.WindowInfo]:
    """Windows of other apps (ours: this process and the device host with its preview)."""
    return [w for w in win32.list_windows(exclude_pid=os.getpid()) if w.pid not in own_pids]


def available_sources(
    card_area: Callable[[], Rect | None] = lambda: None, own_pids: Collection[int] = ()
) -> list[SourceOption]:
    """Windows, monitors and capture cards. ``card_area``: where the card's picture is shown
    on the desktop (its preview), if anywhere."""
    options = []
    for window in other_windows(own_pids):
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
    for device in list_video_devices():
        spec = VideoDeviceSource(device_name=device.name)
        options.append(SourceOption(t("source.card", name=device.name), spec, card_area))
    return options


def _source_name(source: Source) -> str:
    """A short name for a source: the window title (else its exe), the monitor or the card."""
    if isinstance(source, WindowSource):
        return source.title or source.exe_name
    return source.device_name.removeprefix("\\\\.\\")


def _page(title: str, content: QWidget, *, scrolls: bool) -> QWidget:
    """A sidebar page: its large title over the content, which may scroll."""
    if scrolls:
        content.setObjectName("page")  # transparent inside the scroll area (theme)
        scroll = QScrollArea(widgetResizable=True, frameShape=QScrollArea.Shape.NoFrame)
        scroll.setWidget(content)
        content = scroll
    page = QWidget()
    layout = QVBoxLayout(page)
    layout.setContentsMargins(*PAGE_MARGINS)
    layout.setSpacing(6)
    layout.addWidget(QLabel(title, objectName="pageTitle"))
    layout.addWidget(content, 1)
    return page


class _Bridge(QObject):
    """Carries results from worker threads to the UI thread.

    Owned by the application, and workers capture the bridge, never the window: a worker
    may drop the last reference after the window is gone, and a Qt object destroyed outside
    the UI thread crashes the process.
    """

    models = Signal(object)  # list[InstalledModel] | None
    spill = Signal(str, float)  # model, part of it that runs on the CPU


class MainWindow(QMainWindow):
    def __init__(
        self,
        engine: Engine,
        *,
        config: AppConfig | None = None,
        store: ProfileStore | None = None,
        card_host: Host | None = None,
    ) -> None:
        super().__init__()
        # Tooltips also while another app has focus, as Windows' own controls do.
        self.setAttribute(Qt.WidgetAttribute.WA_AlwaysShowToolTips)
        self.setWindowTitle(f"{APP_NAME} {__version__}")
        self.setWindowIcon(app_icon())
        self.resize(1000, 680)
        self._engine = engine
        self._config = config or load_config()
        self._store = store or ProfileStore()
        self._profile_id, self._profile = self._initial_profile()
        self._theme = themes.apply_theme(self._config.theme)
        QGuiApplication.styleHints().colorSchemeChanged.connect(self._on_color_scheme)
        self._sources: list[SourceOption] = []
        self._state = EngineState.IDLE
        self._frame: FrameInfo | None = None
        self._rows: dict[int, int] = {}
        self._translations: dict[int, str] = {}
        self._clear_generation = 0
        self._ollama_ok: bool | None = None  # None until the first check
        self._translate_ms: deque[float] = deque(maxlen=AVERAGE_OF)  # not cached ones
        self._state_text = t("state.idle")
        self._reads = 0  # lines read since Start: tells "running" from "waiting for subtitles"
        # The capture card: the device host owns it; a start on it waits until it is open.
        self._card = CaptureCard(self._config.preview, card_host)
        self._card.opened.connect(self._on_card_opened)
        self._card.failed.connect(self._on_card_failed)
        self._card.signal_changed.connect(self._on_card_signal)
        self._card.preview_changed.connect(self._on_card_preview)
        self._card_waiting = False  # Start pressed (or card lost while running): start once open
        self._card_started_on = ""  # the shared frames the running pipeline reads
        self._card_region_pending = False  # region pick waits for the card to open
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
        self._bridge = _Bridge(QCoreApplication.instance())
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
        self.tray.toggle_preview.connect(self.toggle_card_preview)
        self.tray.quit.connect(self.close)
        self.tray.show()

        self._build()
        self._update_status()
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
        self._apply_card()
        self.check_ollama()
        self._engine.start()

    # --- layout ----------------------------------------------------------------------------------

    def _build(self) -> None:
        live = QWidget()
        layout = QVBoxLayout(live)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        # Shown only while something is wrong (Ollama missing or stopped).
        self.warn_card = QFrame(objectName="warnCard")
        warn = QHBoxLayout(self.warn_card)
        warn.setContentsMargins(12, 8, 12, 8)
        warn.setSpacing(8)
        self.warn_icon = QLabel()
        self.ollama_label = QLabel(t("ollama.checking"), wordWrap=True)
        self.ollama_button = QPushButton(t("ollama.start"), clicked=self._start_ollama)
        self.setup_button = QPushButton(t("setup.button"), clicked=self.open_setup)
        warn.addWidget(self.warn_icon)
        warn.addWidget(self.ollama_label, 1)
        warn.addWidget(self.ollama_button)
        warn.addWidget(self.setup_button)
        self.warn_card.hide()
        layout.addWidget(self.warn_card)

        # What runs, in one line; the pickers live in the "Change" sheet.
        summary = QFrame(objectName="card")
        row = QHBoxLayout(summary)
        row.setContentsMargins(16, 12, 12, 12)
        row.setSpacing(12)
        text = QVBoxLayout()
        text.setSpacing(2)
        self.summary_title = QLabel(objectName="summaryTitle")
        self.summary_title.setMinimumWidth(1)  # long window titles elide instead of widening
        self.summary_detail = rows.hint("")
        text.addWidget(self.summary_title)
        text.addWidget(self.summary_detail)
        self.change_button = QPushButton(t("live.change"), clicked=self.open_source_sheet)
        row.addLayout(text, 1)
        row.addWidget(self.change_button, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(summary)

        self.profile_combo = QComboBox()
        self.profile_combo.activated.connect(
            lambda i: self.switch_profile(self.profile_combo.itemData(i))
        )
        self.source_combo = QComboBox()
        self.source_combo.setPlaceholderText(t("source.none"))
        self.refresh_button = QPushButton(t("source.refresh"), clicked=self.refresh_sources)
        self.region_label = QLabel()
        self.region_button = QPushButton(t("region.select"), clicked=self.select_region)
        self.model_combo = QComboBox()
        self.model_combo.activated.connect(self._on_model_combo)
        self.source_sheet = self._build_source_sheet()

        self.start_button = QPushButton(t("start"), clicked=self.toggle, objectName="startButton")
        themes.set_glow(self.start_button, self._theme)
        self.calibrate_button = QPushButton(
            t("calibration.button"), checkable=True, clicked=self.set_calibration
        )
        self.calibrate_button.setProperty("pill", True)  # rounded like Start (theme)
        buttons = QHBoxLayout()
        buttons.setSpacing(12)
        buttons.addWidget(self.start_button, 3)
        buttons.addWidget(self.calibrate_button, 1)
        layout.addLayout(buttons)

        self.state_dot = QLabel("●", objectName="statusDot")
        self.state_label = QLabel(t("state.idle"))
        self.state_label.setProperty("hint", True)
        self.state_label.setMinimumWidth(1)
        state = QHBoxLayout()
        state.setContentsMargins(8, 0, 0, 0)
        state.setSpacing(6)
        state.addWidget(self.state_dot)
        state.addWidget(self.state_label, 1)
        layout.addLayout(state)

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
        self.feed.setShowGrid(False)
        self.feed.verticalHeader().hide()
        # The list for reading, the table (time, model, timings) in the developer view.
        self.feed_list = FeedList()
        self.feed_stack = QStackedWidget()
        self.feed_stack.addWidget(self.feed_list)
        self.feed_stack.addWidget(self.feed)
        self._show_feed_view()
        feed_card = QGroupBox(t("live.feed_card"))
        QVBoxLayout(feed_card).addWidget(self.feed_stack)
        layout.addWidget(feed_card, 1)

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
        self.settings.preview_changed.connect(self._apply_card)
        self.settings.theme_changed.connect(self.set_theme)
        self.settings.clear_cache_requested.connect(self.clear_cache)
        self.settings.open_logs_requested.connect(self._open_logs)
        self.settings.setup_requested.connect(self.open_setup)
        self.settings.developer_view_changed.connect(self._show_feed_view)

        self.models = ModelsTab(self._config, self._profile)
        self.models.model_selected.connect(self._on_model_selected)
        self.models.models_changed.connect(self.check_ollama)
        self.models.start_ollama_requested.connect(self._start_ollama)
        self.models.setup_requested.connect(self.open_setup)

        self.profiles = ProfilesTab(self._profile)
        self.profiles.changed.connect(self._on_profile_edited)
        self.profiles.new_requested.connect(self.new_profile)
        self.profiles.copy_requested.connect(self.copy_profile)
        self.profiles.rename_requested.connect(self.rename_profile)
        self.profiles.delete_requested.connect(self.delete_profile)

        # macOS Settings style: a sidebar picks the page, each page has a large title. Long
        # pages scroll instead of squeezing their rows on short screens (Models scrolls itself).
        self.pages = QStackedWidget()
        self._nav_group = QButtonGroup(self)
        self.nav_buttons: list[QPushButton] = []
        for index, (page, key, icon, scrolls) in enumerate(
            (
                (live, "tab.live", "live", False),
                (self.appearance, "tab.appearance", "appearance", True),
                (self.profiles, "tab.profiles", "profiles", True),
                (self.models, "tab.models", "models", False),
                (self.settings, "tab.settings", "settings", True),
            )
        ):
            button = QPushButton(t(key), objectName="navButton", checkable=True)
            button.setProperty("icon_name", icon)
            button.setIconSize(QSize(NAV_ICON_PX, NAV_ICON_PX))
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            button.clicked.connect(lambda _=False, i=index: self.show_page(i))
            self._nav_group.addButton(button, index)
            self.nav_buttons.append(button)
            self.pages.addWidget(_page(t(key), page, scrolls=scrolls))
        self._fill_profile_combo()

        self.header = Header(self._theme)
        self.status_dot = QLabel("●", objectName="statusDot")
        self.status_text = QLabel(objectName="statusText")
        status = QHBoxLayout()
        status.setContentsMargins(8, 0, 8, 0)
        status.setSpacing(6)
        status.addWidget(self.status_dot)
        status.addWidget(self.status_text, 1)
        settings_line = QFrame(objectName="separator")
        sidebar = QWidget(objectName="sidebar")
        sidebar.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        sidebar.setFixedWidth(SIDEBAR_WIDTH)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(8, 12, 8, 12)
        side.setSpacing(2)
        side.addWidget(self.header)
        side.addSpacing(12)
        for button in self.nav_buttons[:-1]:
            side.addWidget(button)
        side.addStretch(1)
        side.addWidget(settings_line)
        side.addSpacing(6)
        side.addWidget(self.nav_buttons[-1])  # Settings: apart at the bottom
        side.addSpacing(10)
        side.addLayout(status)
        self._tint_nav()
        self.show_page(0)

        central = QWidget()
        row = QHBoxLayout(central)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        row.addWidget(sidebar)
        row.addWidget(self.pages, 1)
        self.setCentralWidget(central)

    def _build_source_sheet(self) -> QDialog:
        """Profile, source, region and model: picked here, summed up on the Live page."""
        sheet = QDialog(self)
        sheet.setWindowTitle(t("live.change_title"))
        sheet.setWindowModality(Qt.WindowModality.WindowModal)
        sheet.setMinimumWidth(SHEET_WIDTH)
        card = rows.RowCard()
        card.add_row(t("profile.label"), self.profile_combo)
        source = QHBoxLayout()
        source.addWidget(self.source_combo, 1)
        source.addWidget(self.refresh_button)
        card.add_row(t("source.label"), source)
        region = QHBoxLayout()
        region.addWidget(self.region_label, 1)
        region.addWidget(self.region_button)
        card.add_row(t("region.label"), region)
        card.add_row(t("model.label"), self.model_combo)
        done = QPushButton(t("live.done"), clicked=sheet.accept)
        done.setDefault(True)
        footer = QHBoxLayout()
        footer.addStretch(1)
        footer.addWidget(done)
        layout = QVBoxLayout(sheet)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)
        layout.addWidget(card)
        layout.addLayout(footer)
        return sheet

    def open_source_sheet(self) -> None:
        self.refresh_sources()
        self.source_sheet.show()
        self.source_sheet.raise_()

    def _update_summary(self) -> None:
        """The Live page's summary card: "<source> · <model>" and the profile."""
        source = self._profile.source
        name = t("live.no_source") if source is None else _source_name(source)
        model = self._config.ollama.gpu_model or t("live.no_model")
        self.summary_title.setText(f"{name} · {model}")
        self.summary_title.setToolTip(self.summary_title.text())
        region = "live.region_set" if self._profile.region is not None else "live.region_unset"
        self.summary_detail.setText(
            t("live.summary_detail", profile=self._profile.name, region=t(region))
        )

    def _show_state(self, text: str) -> None:
        """The state line; while translating it adds the mean translation time."""
        self._state_text = text
        if self._state == EngineState.RUNNING and self._reads and self._translate_ms:
            seconds = sum(self._translate_ms) / len(self._translate_ms) / 1000
            text = f"{text} · {t('live.average', seconds=f'{seconds:.1f}'.replace('.', ','))}"
        self.state_label.setText(text)

    def _show_feed_view(self) -> None:
        developer = self._config.developer_view
        self.feed_stack.setCurrentWidget(self.feed if developer else self.feed_list)

    def show_page(self, index: int) -> None:
        """Select a sidebar page (0 = Live, then Appearance, Profiles, Models, Settings)."""
        self.nav_buttons[index].setChecked(True)
        self.pages.setCurrentIndex(index)

    def _tint_nav(self) -> None:
        """Sidebar icons in the theme's text color, the warning in its own (redrawn on a theme
        change)."""
        self.warn_icon.setPixmap(icons.pixmap("warning", self._theme.color("warn"), 18, 2.0))
        color = self._theme.color("text")
        for button in self.nav_buttons:
            button.setIcon(icons.icon(button.property("icon_name"), color, NAV_ICON_PX))

    def _update_status(self) -> None:
        """The sidebar's dot: translating, paused, Ollama off, or stopped."""
        if self._state in ACTIVE_STATES or self._card_waiting:
            paused = self._state == EngineState.PAUSED
            state, text = ("warn", "sidebar.paused") if paused else ("ok", "sidebar.running")
        elif self._ollama_ok is False:
            state, text = "warn", "sidebar.ollama_off"
        else:
            state, text = "idle", "sidebar.stopped"
        self.status_text.setText(t(text))
        for dot in (self.status_dot, self.state_dot):
            if dot.property("state") != state:
                dot.setProperty("state", state)  # colored by the theme
                themes.repolish(dot)

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

    def set_theme(self, choice: str) -> None:
        self._theme = themes.apply_theme(choice)
        self.header.set_theme(self._theme)
        themes.set_glow(self.start_button, self._theme)
        self._tint_nav()

    def _on_color_scheme(self) -> None:
        """Windows switched light/dark: follow it when the theme is "system"."""
        if self._config.theme in themes.THEMES:
            return  # a forced theme ignores Windows
        if themes.resolve(self._config.theme) != self._theme.name:
            self.set_theme(self._config.theme)

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

    # --- capture card ----------------------------------------------------------------------------

    def _profile_card_name(self) -> str:
        source = self._profile.source
        return source.device_name if isinstance(source, VideoDeviceSource) else ""

    def _reading_card(self) -> bool:
        return isinstance(self._profile.source, VideoDeviceSource) and bool(self._card_started_on)

    def _apply_card(self) -> None:
        """Preview settings or the active profile changed: tell the device host."""
        self._card.set_profile_card(self._profile_card_name())
        self._card.apply()
        self._show_preview_status()

    def toggle_card_preview(self) -> None:
        self._config.preview.enabled = not self._config.preview.enabled
        self._save_later()
        self._apply_card()

    def _show_preview_status(self) -> None:
        preview = self._config.preview
        if not preview.enabled:
            text = t("settings.preview_off")
        elif not self._card.preview_device:
            text = t("settings.preview_no_card")
        elif self._card.preview_shown_on is not None:
            name = self._card.preview_shown_on.removeprefix("\\\\.\\")
            text = t("settings.preview_on", monitor=name)
        elif preview.monitor and preview.monitor not in {
            m.device_name for m in win32.list_monitors()
        }:
            text = t("settings.preview_no_monitor")
        else:
            text = t("state.card_opening")
        self.settings.show_preview(text)

    def _on_card_opened(self, shared_frames: str) -> None:
        if self._card_region_pending:
            card = self._card.wanted()
            if card is not None:
                self._pick_on_card_frame(card)
        # A pending start, or the card came back (new buffer) while a pipeline reads it.
        restart = self._state in ACTIVE_STATES and self._reading_card()
        if self._card_waiting or (restart and shared_frames != self._card_started_on):
            self._send_start()
        self._show_preview_status()

    def _on_card_failed(self, kind: str, name: str) -> None:
        text = t(f"card.{kind}", name=name)
        self.statusBar().showMessage(text, 30000)
        if kind == "crashed":
            self.tray.notify(APP_NAME, text)
            if self._reading_card():
                self._card_waiting = True  # the engine loses its frames; restart when back
        self._show_preview_status()

    def _on_card_signal(self, black: bool) -> None:
        text = t("card.black" if black else "card.picture_back")
        self.statusBar().showMessage(text, 60000 if black else 5000)
        if black:
            self.tray.notify(APP_NAME, text)

    def _on_card_preview(self, by_user: bool) -> None:
        if by_user:
            self._save_later()  # the preview was closed on itself: remember it is off
            self.statusBar().showMessage(t("card.preview_closed"), 10000)
        self._show_preview_status()
        self._place_overlay()  # it may have been the subtitle's on-screen area
        if self._calibrating:
            self._place_region_frame()

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
        self._apply_card()
        self.refresh_sources()
        self._update_region_label()
        if was_active or self._card_waiting:
            if self.selected_source() is not None and profile.region is not None:
                self._send_start()
            else:
                self.stop()
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
        self._sources = available_sources(self._card.preview_rect, self._own_pids())
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

    def _own_pids(self) -> set[int]:
        pid = self._card.host_pid
        return {pid} if pid else set()

    def select_region(self) -> None:
        """Dim every monitor; the user marks the subtitle wherever it is shown. A capture card
        without a preview on screen: mark it on a still frame instead."""
        option = self.selected_source()
        if (
            option is not None
            and isinstance(option.spec, VideoDeviceSource)
            and self._card.preview_rect() is None
        ):
            self._pick_on_card_frame(option.spec)
            return
        reopen = self.source_sheet.isVisible()
        self.source_sheet.hide()  # out of the way of the marking
        picker = RegionPicker()
        picker.selected.connect(self._on_picked)
        if reopen:
            picker.selected.connect(lambda _: self.open_source_sheet())
            picker.cancelled.connect(self.open_source_sheet)
        picker.show()
        self._picker = picker  # keep a reference while it is open

    def _on_picked(self, logical: Rect) -> None:
        physical = screens.to_physical(logical, screens.current_screens())
        card = self._card.device_name or self._card.preview_device
        pick = resolve_card(physical, self._card.preview_rect(), card)
        if pick is None:
            windows = [
                (w, area)
                for w in other_windows(self._own_pids())
                if (area := visible_client_area(w.hwnd)) is not None
            ]
            pick = resolve(physical, windows, win32.list_monitors())
        if pick is None:
            self.statusBar().showMessage(t("error.region_needs_screen"), 5000)
            return
        self._use_region(pick.source, pick.region)

    def _use_region(self, source: Source, region: NormRect) -> None:
        source_changed = source != self._profile.source
        self._profile.source = source
        self._card.set_profile_card(self._profile_card_name())
        self.refresh_sources()
        self.set_region(region, restart=source_changed)

    def _pick_on_card_frame(self, card: VideoDeviceSource) -> None:
        """The card's picture is on no screen: pick the region on a frame of it (opened for
        this while it is not open anyway)."""
        if not self._card.is_open(card.device_name):
            self._card_region_pending = True
            self._card.set_snapshot_device(card)
            self.statusBar().showMessage(t("state.card_opening"), 5000)
            return
        self._card_region_pending = False
        image = self._card.snapshot()
        self._card.set_snapshot_device(None)
        if image is None:
            return
        region = self._profile.region if self._profile.source == card else None
        dialog = FrameRegionDialog(image, region, self)
        if dialog.exec() and (picked := dialog.region()) is not None:
            self._use_region(card, picked)

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
        self._update_summary()
        if r is None or source is None:
            self.region_label.setText(t("region.none"))
            self.region_label.setToolTip("")
            return
        name = _source_name(source)[:70]
        self.region_label.setText(t("region.chosen", source=name))
        self.region_label.setToolTip(
            t("region.selected", source=name, x=r.x, y=r.y, w=r.width, h=r.height)
        )

    # --- Ollama ----------------------------------------------------------------------------------

    def check_ollama(self) -> None:
        url = self._config.ollama.url or DEFAULT_URL
        # While translating: is the model slowed down by running partly on the CPU?
        watch = self._state == EngineState.RUNNING and not self._warned_spill
        model = self._config.ollama.gpu_model if watch else ""
        bridge = self._bridge

        def work() -> None:
            bridge.models.emit(list_models(url))
            if model and (share := cpu_share(url, model)) is not None:
                bridge.spill.emit(model, share)

        threading.Thread(target=work, daemon=True).start()

    def _on_models(self, models: list[InstalledModel] | None) -> None:
        self._ollama_ok = models is not None
        self._update_status()
        self.warn_card.setVisible(models is None)
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
        self._update_summary()
        self.models.mark_in_use()

    def _start_ollama(self) -> None:
        if not start_ollama():
            self.ollama_label.setText(t("ollama.not_installed"))
            self.ollama_button.hide()
            self.warn_card.show()
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
        self._update_summary()
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
        if self._state in ACTIVE_STATES or self._card_waiting:
            self.stop()
            return
        option = self.selected_source()
        if option is None:
            self.statusBar().showMessage(t("error.no_source"), 5000)
            return
        if self._profile.region is None:
            self.statusBar().showMessage(t("error.no_region"), 5000)
            return
        self._profile.source = option.spec
        self._update_region_label()  # it named the previous source
        self._send_start()

    def stop(self) -> None:
        self._card_waiting = False
        self._card.set_pipeline_device(None)
        if self._state in ACTIVE_STATES:
            self._engine.send(StopPipeline())
        else:
            self._set_state(self._state, "")  # leaves the "card opening" text

    def _send_start(self) -> None:
        source = self._profile.source
        card = source if isinstance(source, VideoDeviceSource) else None
        self._card.set_pipeline_device(card)
        if card is not None and not self._card.is_open(card.device_name):
            # The device host opens it (a busy card is retried); _on_card_opened starts then.
            if self._state in ACTIVE_STATES:
                self._engine.send(StopPipeline())
            self._card_waiting = True
            self._show_running(True)
            self._show_state(t("state.card_opening"))
            return
        self._card_waiting = False
        self._card_started_on = self._card.shared_frames if card is not None else ""
        if self.model_combo.currentText():
            self._config.ollama.gpu_model = self.model_combo.currentText()
        self._save()
        self._reads = 0
        self._translate_ms.clear()
        self._warned_spill = False
        # Line ids start again: old rows stay in the feed but no new line may land in them.
        self._rows.clear()
        self._translations.clear()
        if self._web is not None:
            self._web.new_session()
        self._engine.send(
            StartPipeline(
                profile=self._profile.model_copy(deep=True),
                ollama=self._config.ollama,
                profile_id=self._profile_id,
                shared_frames=self._card_started_on,
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
        if not self._card_waiting:
            self._card.set_pipeline_device(None)
        self._set_state(EngineState.STOPPED, "")
        self.statusBar().showMessage(t("error.engine_crashed"), 30000)
        self.tray.notify(APP_NAME, t("error.engine_crashed"))

    def handle(self, event: Event) -> None:
        match event:
            case StateChanged(state=state, detail=detail):
                self._set_state(state, detail)
            case EngineError(kind=ErrorKind.SOURCE_LOST) if self._reading_card():
                # The device host reopens the card; the pipeline follows (_on_card_opened).
                self._card_waiting = True
            case EngineError(kind=kind, message=message, fatal=fatal):
                self.statusBar().showMessage(t(f"error.{kind}", detail=message), 15000)
                if fatal:
                    self._card.set_pipeline_device(None)
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
        # STOPPED also comes from a pipeline replaced by a new one: the card is released only
        # by stop(), by a start on another source and by a fatal error.
        if self._card_waiting and state not in ACTIVE_STATES:
            text = t("state.card_opening")
        self._show_state(text)
        running = state in ACTIVE_STATES or self._card_waiting
        self._show_running(running)
        self.tray.set_running(running)
        self.models.set_busy(state in ACTIVE_STATES)
        self._update_status()
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
            self.feed_list.remove_last()
            self._rows = {k: v for k, v in self._rows.items() if v < FEED_LIMIT - 1}
        self.feed.insertRow(0)
        self.feed_list.insert_top()
        self._rows = {k: v + 1 for k, v in self._rows.items()}
        self._rows[line_id] = 0
        return 0

    def _set_cells(self, row: int, values: dict[int, str]) -> None:
        for column, value in values.items():
            self.feed.setItem(row, column, QTableWidgetItem(value))
        english, turkish = (self.feed.item(row, c) for c in (1, 2))
        self.feed_list.set_line(
            row, turkish.text() if turkish else "", english.text() if english else ""
        )

    def _on_read(self, event: SubtitleRead) -> None:
        """A line was read. The overlay keeps the previous translation until this line's own
        arrives: a blank overlay while the model works looked like subtitles vanishing early."""
        self._clear_generation += 1  # a subtitle is up: cancel a pending clear
        if self._reads == 0 and self._state == EngineState.RUNNING:
            self._show_state(t("state.running"))
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
        if not event.cached:
            self._translate_ms.append(event.translate_ms)
            self._show_state(self._state_text)
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
        self._card.shutdown()
        self._engine.shutdown()
        super().closeEvent(event)
        # Qt quits when the last *visible* window closes; this one may be hidden in the tray, or
        # the setup dialog may still be open.
        QCoreApplication.quit()
