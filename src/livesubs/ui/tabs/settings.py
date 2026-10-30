"""Settings tab, macOS Settings style: cards of rows (window and theme, phone output,
capture card preview, hotkeys) and a folded "Gelişmiş" card for what rarely changes (Ollama,
context, model keep-alive, reading speed, upkeep). Long explanations sit in (i) tooltips.

Edits change the given ``AppConfig`` / ``Profile`` objects in place and announce it; the main
window saves them and applies what can change while running (hotkeys, recording mode).
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QKeySequenceEdit,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from livesubs.core.config import AppConfig, HotkeySettings
from livesubs.core.profiles import OverlayDisplay, Profile
from livesubs.i18n import t
from livesubs.platform import hotkeys, win32
from livesubs.platform.video_devices import list_video_devices
from livesubs.ui.qr import qr_pixmap
from livesubs.ui.rows import Disclosure, RowCard, hint
from livesubs.ui.theme import CHOICES as THEME_CHOICES

KEEP_ALIVE = ("5m", "30m", "2h", "-1")  # Ollama durations; -1 keeps the model loaded
HOTKEY_ACTIONS = ("calibration", "toggle_overlay", "pause")
FIELD_WIDTH = 150  # spin boxes and key fields at the right edge of a row


def hotkey_text(sequence: QKeySequence) -> str:
    """Qt's text ("Ctrl+Meta+P") in the form the hotkey parser reads ("Ctrl+Win+P")."""
    if sequence.isEmpty():
        return ""
    text = QKeySequence(sequence[0]).toString(QKeySequence.SequenceFormat.PortableText)
    return text.replace("Meta", "Win")


def _switch(checked: bool, on_toggle: Callable[[bool], None]) -> QCheckBox:
    """A text-less check box for the right edge of a row (its row names it)."""
    box = QCheckBox()
    box.setChecked(checked)
    box.toggled.connect(on_toggle)
    return box


class SettingsTab(QWidget):
    config_changed = Signal()
    profile_changed = Signal()
    hotkeys_changed = Signal()
    recording_changed = Signal(bool)
    web_changed = Signal()  # turned on/off or another port
    display_changed = Signal()  # the active profile shows translations on the phone only, or not
    preview_changed = Signal()  # capture card preview: on/off, card or monitor
    theme_changed = Signal(str)
    clear_cache_requested = Signal()
    open_logs_requested = Signal()
    developer_view_changed = Signal()
    setup_requested = Signal()  # the setup dialog: installs, shortcuts, settings backup

    def __init__(self, config: AppConfig, profile: Profile) -> None:
        super().__init__()
        self.config = config
        self.profile = profile
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 8, 0)
        layout.setSpacing(12)
        layout.addWidget(self._window_box())
        layout.addWidget(self._web_box())
        layout.addWidget(self._preview_box())
        layout.addWidget(self._hotkey_box())
        self.advanced = Disclosure(t("settings.advanced"), self._advanced_box())
        layout.addWidget(self.advanced)
        layout.addStretch(1)

    # --- groups ----------------------------------------------------------------------------------

    def _window_box(self) -> RowCard:
        card = RowCard(t("settings.window"))
        self.theme = QComboBox()
        for name in THEME_CHOICES:
            self.theme.addItem(t(f"settings.theme.{name}"), name)
        self.theme.setCurrentIndex(max(self.theme.findData(self.config.theme), 0))
        self.theme.currentIndexChanged.connect(self._on_theme)
        card.add_row(t("settings.theme"), self.theme)
        self.minimize_to_tray = _switch(self.config.minimize_to_tray, self._on_tray)
        card.add_row(t("settings.minimize_to_tray"), self.minimize_to_tray, expand=False)
        self.recording = _switch(self.config.recording_mode, self._on_recording)
        card.add_row(
            t("settings.recording_mode"),
            self.recording,
            tip=t("settings.recording_hint"),
            expand=False,
        )
        self.developer_view = _switch(self.config.developer_view, self._on_developer_view)
        card.add_row(
            t("settings.developer_view"),
            self.developer_view,
            tip=t("settings.developer_view_tip"),
            expand=False,
        )
        return card

    def _web_box(self) -> RowCard:
        card = RowCard(t("settings.web"))
        self.web_enabled = _switch(self.config.web.enabled, self._on_web_enabled)
        card.add_row(t("settings.web_enabled"), self.web_enabled, expand=False)
        self.web_only = _switch(
            self.profile.overlay.display == OverlayDisplay.WEB_ONLY, self._on_web_only
        )
        card.add_row(
            t("settings.web_only"), self.web_only, tip=t("settings.web_only_tip"), expand=False
        )
        self.web_port = QSpinBox()
        self.web_port.setRange(1024, 65535)
        self.web_port.setValue(self.config.web.port)
        self.web_port.setFixedWidth(FIELD_WIDTH)
        self.web_port.editingFinished.connect(self._on_web_port)
        card.add_row(t("settings.web_port"), self.web_port, expand=False)
        self.web_status = QLabel(t("settings.web_off"), wordWrap=True)
        self.web_status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        card.add_widget(self.web_status, tip=t("settings.web_hint"))
        self.web_qr = QLabel()
        self.web_qr.hide()
        self._qr_url = ""
        card.set_side(self.web_qr)
        return card

    def _preview_box(self) -> RowCard:
        card = RowCard(t("settings.preview"))
        self.preview_enabled = _switch(self.config.preview.enabled, self._on_preview_enabled)
        card.add_row(
            t("settings.preview_enabled"),
            self.preview_enabled,
            tip=t("settings.preview_hint"),
            expand=False,
        )
        self.preview_device = QComboBox()
        self.preview_device.activated.connect(self._on_preview_device)
        refresh = QPushButton(t("source.refresh"))
        refresh.clicked.connect(self.refresh_preview_choices)
        device_row = QHBoxLayout()
        device_row.addWidget(self.preview_device, 1)
        device_row.addWidget(refresh)
        card.add_row(t("settings.preview_device"), device_row)
        self.preview_monitor = QComboBox()
        self.preview_monitor.activated.connect(self._on_preview_monitor)
        card.add_row(t("settings.preview_monitor"), self.preview_monitor)
        self.preview_status = hint("")
        card.add_widget(self.preview_status)
        self.refresh_preview_choices()
        return card

    def refresh_preview_choices(self) -> None:
        """Connected cards and monitors; a chosen one that is missing stays listed."""
        preview = self.config.preview
        self.preview_device.clear()
        self.preview_device.addItem(t("settings.preview_profile_card"), "")
        names = [d.name for d in list_video_devices()]
        if preview.device and preview.device not in names:
            names.append(preview.device)
        for name in names:
            self.preview_device.addItem(name, name)
        self.preview_device.setCurrentIndex(max(self.preview_device.findData(preview.device), 0))
        self.preview_monitor.clear()
        self.preview_monitor.addItem(t("settings.preview_primary"), "")
        for monitor in win32.list_monitors():
            label = t(
                "source.monitor",
                name=monitor.device_name.removeprefix("\\\\.\\"),
                width=monitor.rect.width,
                height=monitor.rect.height,
            )
            self.preview_monitor.addItem(label, monitor.device_name)
        self.preview_monitor.setCurrentIndex(max(self.preview_monitor.findData(preview.monitor), 0))

    def show_preview(self, text: str) -> None:
        """The preview's state (shown where, or why not), and the checkbox in step with it."""
        self.preview_status.setText(text)
        self.preview_enabled.blockSignals(True)
        self.preview_enabled.setChecked(self.config.preview.enabled)
        self.preview_enabled.blockSignals(False)

    def _hotkey_box(self) -> RowCard:
        card = RowCard(t("settings.hotkeys"))
        self.hotkey_edits: dict[str, QKeySequenceEdit] = {}
        for action in HOTKEY_ACTIONS:
            edit = QKeySequenceEdit(QKeySequence(getattr(self.config.hotkeys, action)))
            edit.setMaximumSequenceLength(1)
            edit.setFixedWidth(FIELD_WIDTH)
            edit.editingFinished.connect(lambda a=action: self._on_hotkey(a))
            self.hotkey_edits[action] = edit
            card.add_row(t(f"settings.hotkey.{action}"), edit, expand=False)
        reset = QPushButton(t("settings.hotkeys_reset"), clicked=self.reset_hotkeys)
        self.hotkey_status = hint(t("settings.hotkeys_hint"))
        row = QHBoxLayout()
        row.addWidget(self.hotkey_status, 1)
        row.addWidget(reset)
        card.add_layout(row)
        return card

    def _advanced_box(self) -> RowCard:
        """Settings that rarely change, folded away: Ollama, context, reading speed, upkeep."""
        card = RowCard()
        self.ollama_url = QLineEdit(self.config.ollama.url)
        self.ollama_url.editingFinished.connect(self._on_url)
        card.add_row(t("settings.ollama_url"), self.ollama_url)

        self.context_lines = QSpinBox()
        self.context_lines.setRange(0, 10)
        self.context_lines.setValue(self.config.ollama.context_lines)
        self.context_lines.setFixedWidth(FIELD_WIDTH)
        self.context_lines.valueChanged.connect(lambda v: self._set_ollama("context_lines", v))
        card.add_row(
            t("settings.context_lines"),
            self.context_lines,
            tip=t("settings.context_lines_tip"),
            expand=False,
        )

        self.keep_alive = QComboBox()
        for value in KEEP_ALIVE:
            self.keep_alive.addItem(t(f"settings.keep_alive.{value}"), value)
        current = self.keep_alive.findData(self.config.ollama.keep_alive)
        if current < 0:  # a value set by hand in config.json
            self.keep_alive.addItem(self.config.ollama.keep_alive, self.config.ollama.keep_alive)
            current = self.keep_alive.count() - 1
        self.keep_alive.setCurrentIndex(current)
        self.keep_alive.currentIndexChanged.connect(
            lambda i: self._set_ollama("keep_alive", self.keep_alive.itemData(i))
        )
        card.add_row(t("settings.keep_alive"), self.keep_alive)

        self.fps = QSpinBox(suffix=t("settings.fps_suffix"))
        self.fps.setRange(1, 30)
        self.fps.setValue(self.profile.capture.fps)
        self.fps.setFixedWidth(FIELD_WIDTH)
        self.fps.valueChanged.connect(self._on_fps)
        card.add_row(t("settings.fps"), self.fps, tip=t("settings.fps_tip"), expand=False)

        # Signal to signal through connect(): `clicked=signal` in the constructor raises on click.
        self.setup_button = QPushButton(t("setup.button"))
        self.setup_button.clicked.connect(self.setup_requested)
        self.clear_cache = QPushButton(t("settings.clear_cache"))
        self.clear_cache.clicked.connect(self.clear_cache_requested)
        self.open_logs = QPushButton(t("settings.open_logs"))
        self.open_logs.clicked.connect(self.open_logs_requested)
        buttons = QHBoxLayout()
        buttons.addWidget(self.setup_button)
        buttons.addWidget(self.clear_cache)
        buttons.addWidget(self.open_logs)
        buttons.addStretch(1)
        card.add_row(t("settings.upkeep"), buttons)
        self.upkeep_status = hint("")
        card.add_widget(self.upkeep_status)
        return card

    def set_profile(self, profile: Profile) -> None:
        self.profile = profile
        self.refresh_profile()

    def refresh_profile(self) -> None:
        """Show the active profile's values (they can also change on the Profiles tab)."""
        self.fps.blockSignals(True)
        self.fps.setValue(self.profile.capture.fps)
        self.fps.blockSignals(False)
        self.web_only.blockSignals(True)
        self.web_only.setChecked(self.profile.overlay.display == OverlayDisplay.WEB_ONLY)
        self.web_only.blockSignals(False)

    def show_web(self, url: str | None, clients: int = 0, error: str = "") -> None:
        """The web output's state: its address and QR code while it runs."""
        if error:
            self.web_status.setText(error)
        elif url is None:
            self.web_status.setText(t("settings.web_off"))
        else:
            self.web_status.setText(t("settings.web_address", url=url, clients=clients))
        if url and url != self._qr_url:
            self.web_qr.setPixmap(qr_pixmap(url))
        self._qr_url = url or ""
        self.web_qr.setVisible(bool(url))

    # --- edits -----------------------------------------------------------------------------------

    def _set_ollama(self, field: str, value: object) -> None:
        setattr(self.config.ollama, field, value)
        self.config_changed.emit()

    def _on_url(self) -> None:
        url = self.ollama_url.text().strip().rstrip("/")
        if not url.startswith(("http://", "https://")):
            self.ollama_url.setText(self.config.ollama.url)
            return
        if url != self.config.ollama.url:
            self._set_ollama("url", url)

    def _on_fps(self, fps: int) -> None:
        self.profile.capture.fps = fps
        self.profile_changed.emit()

    def _on_web_enabled(self, on: bool) -> None:
        self.config.web.enabled = on
        self.config_changed.emit()
        self.web_changed.emit()

    def _on_web_port(self) -> None:
        if self.web_port.value() != self.config.web.port:
            self.config.web.port = self.web_port.value()
            self.config_changed.emit()
            self.web_changed.emit()

    def _on_web_only(self, on: bool) -> None:
        overlay = self.profile.overlay
        if on:
            overlay.display = OverlayDisplay.WEB_ONLY
        elif overlay.display == OverlayDisplay.WEB_ONLY:
            overlay.display = OverlayDisplay.AUTO
        self.profile_changed.emit()
        self.display_changed.emit()

    def _on_preview_enabled(self, on: bool) -> None:
        self.config.preview.enabled = on
        self.config_changed.emit()
        self.preview_changed.emit()

    def _on_preview_device(self, index: int) -> None:
        self.config.preview.device = self.preview_device.itemData(index)
        self.config_changed.emit()
        self.preview_changed.emit()

    def _on_preview_monitor(self, index: int) -> None:
        self.config.preview.monitor = self.preview_monitor.itemData(index)
        self.config_changed.emit()
        self.preview_changed.emit()

    def _on_theme(self, index: int) -> None:
        self.config.theme = self.theme.itemData(index)
        self.config_changed.emit()
        self.theme_changed.emit(self.config.theme)

    def _on_tray(self, on: bool) -> None:
        self.config.minimize_to_tray = on
        self.config_changed.emit()

    def _on_developer_view(self, on: bool) -> None:
        self.config.developer_view = on
        self.config_changed.emit()
        self.developer_view_changed.emit()

    def _on_recording(self, on: bool) -> None:
        self.config.recording_mode = on
        self.config_changed.emit()
        self.recording_changed.emit(on)

    def _on_hotkey(self, action: str) -> None:
        edit = self.hotkey_edits[action]
        text = hotkey_text(edit.keySequence())
        current = getattr(self.config.hotkeys, action)
        others = {getattr(self.config.hotkeys, a).lower() for a in HOTKEY_ACTIONS if a != action}
        try:
            hotkeys.parse(text)
        except hotkeys.HotkeyError:
            self.hotkey_status.setText(t("settings.hotkey_invalid", hotkey=text or "—"))
            edit.setKeySequence(QKeySequence(current))
            return
        if text.lower() in others:
            self.hotkey_status.setText(t("settings.hotkey_taken", hotkey=text))
            edit.setKeySequence(QKeySequence(current))
            return
        if text != current:
            setattr(self.config.hotkeys, action, text)
            self.config_changed.emit()
            self.hotkeys_changed.emit()

    def reset_hotkeys(self) -> None:
        defaults = HotkeySettings()
        for action in HOTKEY_ACTIONS:
            setattr(self.config.hotkeys, action, getattr(defaults, action))
            self.hotkey_edits[action].setKeySequence(QKeySequence(getattr(defaults, action)))
        self.config_changed.emit()
        self.hotkeys_changed.emit()

    def show_hotkey_result(self, failed: list[str], *, changed: bool) -> None:
        if failed:
            self.hotkey_status.setText(t("hotkey.failed", keys=", ".join(failed)))
        elif changed:
            self.hotkey_status.setText(t("settings.hotkeys_saved"))
