"""Settings tab: translation, reading speed, phone output, capture card preview, hotkeys,
window, recording, upkeep.

Edits change the given ``AppConfig`` / ``Profile`` objects in place and announce it; the main
window saves them and applies what can change while running (hotkeys, recording mode).
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGroupBox,
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
from livesubs.ui.theme import CHOICES as THEME_CHOICES

KEEP_ALIVE = ("5m", "30m", "2h", "-1")  # Ollama durations; -1 keeps the model loaded
HOTKEY_ACTIONS = ("calibration", "toggle_overlay", "pause")


def hotkey_text(sequence: QKeySequence) -> str:
    """Qt's text ("Ctrl+Meta+P") in the form the hotkey parser reads ("Ctrl+Win+P")."""
    if sequence.isEmpty():
        return ""
    text = QKeySequence(sequence[0]).toString(QKeySequence.SequenceFormat.PortableText)
    return text.replace("Meta", "Win")


def _hint(text: str) -> QLabel:
    label = QLabel(text, wordWrap=True)
    label.setProperty("hint", True)  # muted, styled by the theme
    return label


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
        layout.addWidget(self._translation_box())
        layout.addWidget(self._web_box())
        layout.addWidget(self._preview_box())
        layout.addWidget(self._hotkey_box())
        layout.addWidget(self._window_box())
        layout.addWidget(self._upkeep_box())
        layout.addStretch(1)

    # --- groups ----------------------------------------------------------------------------------

    def _translation_box(self) -> QGroupBox:
        box = QGroupBox(t("settings.translation"))
        form = QFormLayout(box)
        self.ollama_url = QLineEdit(self.config.ollama.url)
        self.ollama_url.editingFinished.connect(self._on_url)
        form.addRow(t("settings.ollama_url"), self.ollama_url)

        self.context_lines = QSpinBox()
        self.context_lines.setRange(0, 10)
        self.context_lines.setValue(self.config.ollama.context_lines)
        self.context_lines.valueChanged.connect(lambda v: self._set_ollama("context_lines", v))
        form.addRow(t("settings.context_lines"), self.context_lines)

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
        form.addRow(t("settings.keep_alive"), self.keep_alive)

        self.fps = QSpinBox(suffix=t("settings.fps_suffix"))
        self.fps.setRange(1, 30)
        self.fps.setValue(self.profile.capture.fps)
        self.fps.valueChanged.connect(self._on_fps)
        form.addRow(t("settings.fps"), self.fps)
        form.addRow(_hint(t("settings.translation_hint")))
        return box

    def _web_box(self) -> QGroupBox:
        box = QGroupBox(t("settings.web"))
        row = QHBoxLayout(box)
        form = QFormLayout()
        self.web_enabled = QCheckBox(t("settings.web_enabled"))
        self.web_enabled.setChecked(self.config.web.enabled)
        self.web_enabled.toggled.connect(self._on_web_enabled)
        form.addRow(self.web_enabled)
        self.web_only = QCheckBox(t("settings.web_only"))
        self.web_only.setToolTip(t("settings.web_only_tip"))
        self.web_only.setChecked(self.profile.overlay.display == OverlayDisplay.WEB_ONLY)
        self.web_only.toggled.connect(self._on_web_only)
        form.addRow(self.web_only)
        self.web_port = QSpinBox()
        self.web_port.setRange(1024, 65535)
        self.web_port.setValue(self.config.web.port)
        self.web_port.editingFinished.connect(self._on_web_port)
        form.addRow(t("settings.web_port"), self.web_port)
        self.web_status = QLabel(t("settings.web_off"), wordWrap=True)
        self.web_status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        form.addRow(self.web_status)
        form.addRow(_hint(t("settings.web_hint")))
        self.web_qr = QLabel()
        self.web_qr.hide()
        self._qr_url = ""
        row.addLayout(form, 1)
        row.addWidget(self.web_qr, 0, Qt.AlignmentFlag.AlignTop)
        return box

    def _preview_box(self) -> QGroupBox:
        box = QGroupBox(t("settings.preview"))
        form = QFormLayout(box)
        self.preview_enabled = QCheckBox(t("settings.preview_enabled"))
        self.preview_enabled.setChecked(self.config.preview.enabled)
        self.preview_enabled.toggled.connect(self._on_preview_enabled)
        form.addRow(self.preview_enabled)
        self.preview_device = QComboBox()
        self.preview_device.activated.connect(self._on_preview_device)
        refresh = QPushButton(t("source.refresh"))
        refresh.clicked.connect(self.refresh_preview_choices)
        device_row = QHBoxLayout()
        device_row.addWidget(self.preview_device, 1)
        device_row.addWidget(refresh)
        form.addRow(t("settings.preview_device"), device_row)
        self.preview_monitor = QComboBox()
        self.preview_monitor.activated.connect(self._on_preview_monitor)
        form.addRow(t("settings.preview_monitor"), self.preview_monitor)
        self.preview_status = _hint("")
        form.addRow(self.preview_status)
        form.addRow(_hint(t("settings.preview_hint")))
        self.refresh_preview_choices()
        return box

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

    def _hotkey_box(self) -> QGroupBox:
        box = QGroupBox(t("settings.hotkeys"))
        form = QFormLayout(box)
        self.hotkey_edits: dict[str, QKeySequenceEdit] = {}
        for action in HOTKEY_ACTIONS:
            edit = QKeySequenceEdit(QKeySequence(getattr(self.config.hotkeys, action)))
            edit.setMaximumSequenceLength(1)
            edit.editingFinished.connect(lambda a=action: self._on_hotkey(a))
            self.hotkey_edits[action] = edit
            form.addRow(t(f"settings.hotkey.{action}"), edit)
        reset = QPushButton(t("settings.hotkeys_reset"), clicked=self.reset_hotkeys)
        self.hotkey_status = _hint(t("settings.hotkeys_hint"))
        row = QHBoxLayout()
        row.addWidget(self.hotkey_status, 1)
        row.addWidget(reset)
        form.addRow(row)
        return box

    def _window_box(self) -> QGroupBox:
        box = QGroupBox(t("settings.window"))
        form = QFormLayout(box)
        self.theme = QComboBox()
        for name in THEME_CHOICES:
            self.theme.addItem(t(f"settings.theme.{name}"), name)
        self.theme.setCurrentIndex(max(self.theme.findData(self.config.theme), 0))
        self.theme.currentIndexChanged.connect(self._on_theme)
        form.addRow(t("settings.theme"), self.theme)
        self.minimize_to_tray = QCheckBox(t("settings.minimize_to_tray"))
        self.minimize_to_tray.setChecked(self.config.minimize_to_tray)
        self.minimize_to_tray.toggled.connect(self._on_tray)
        form.addRow(self.minimize_to_tray)
        self.recording = QCheckBox(t("settings.recording_mode"))
        self.recording.setChecked(self.config.recording_mode)
        self.recording.toggled.connect(self._on_recording)
        form.addRow(self.recording)
        form.addRow(_hint(t("settings.recording_hint")))
        self.developer_view = QCheckBox(t("settings.developer_view"))
        self.developer_view.setToolTip(t("settings.developer_view_tip"))
        self.developer_view.setChecked(self.config.developer_view)
        self.developer_view.toggled.connect(self._on_developer_view)
        form.addRow(self.developer_view)
        return box

    def _upkeep_box(self) -> QGroupBox:
        box = QGroupBox(t("settings.upkeep"))
        row = QHBoxLayout(box)
        # Signal to signal through connect(): `clicked=signal` in the constructor raises on click.
        self.clear_cache = QPushButton(t("settings.clear_cache"))
        self.clear_cache.clicked.connect(self.clear_cache_requested)
        self.open_logs = QPushButton(t("settings.open_logs"))
        self.open_logs.clicked.connect(self.open_logs_requested)
        self.open_setup = QPushButton(t("setup.button"))
        self.open_setup.clicked.connect(self.setup_requested)
        self.upkeep_status = _hint("")
        row.addWidget(self.open_setup)
        row.addWidget(self.clear_cache)
        row.addWidget(self.open_logs)
        row.addWidget(self.upkeep_status, 1)
        return box

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
