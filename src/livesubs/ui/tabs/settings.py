"""Settings tab: translation, reading speed, hotkeys, window behaviour, recording mode, upkeep.

Edits change the given ``AppConfig`` / ``Profile`` objects in place and announce it; the main
window saves them and applies what can change while running (hotkeys, recording mode).
"""

from __future__ import annotations

from PySide6.QtCore import Signal
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
from livesubs.core.profiles import Profile
from livesubs.i18n import t
from livesubs.platform import hotkeys

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
    label.setStyleSheet("color: #9a9a9a;")
    return label


class SettingsTab(QWidget):
    config_changed = Signal()
    profile_changed = Signal()
    hotkeys_changed = Signal()
    recording_changed = Signal(bool)
    clear_cache_requested = Signal()
    open_logs_requested = Signal()

    def __init__(self, config: AppConfig, profile: Profile) -> None:
        super().__init__()
        self.config = config
        self.profile = profile
        layout = QVBoxLayout(self)
        layout.addWidget(self._translation_box())
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
        self.minimize_to_tray = QCheckBox(t("settings.minimize_to_tray"))
        self.minimize_to_tray.setChecked(self.config.minimize_to_tray)
        self.minimize_to_tray.toggled.connect(self._on_tray)
        form.addRow(self.minimize_to_tray)
        self.recording = QCheckBox(t("settings.recording_mode"))
        self.recording.setChecked(self.config.recording_mode)
        self.recording.toggled.connect(self._on_recording)
        form.addRow(self.recording)
        form.addRow(_hint(t("settings.recording_hint")))
        return box

    def _upkeep_box(self) -> QGroupBox:
        box = QGroupBox(t("settings.upkeep"))
        row = QHBoxLayout(box)
        self.clear_cache = QPushButton(
            t("settings.clear_cache"), clicked=self.clear_cache_requested
        )
        self.upkeep_status = _hint("")
        row.addWidget(self.clear_cache)
        row.addWidget(QPushButton(t("settings.open_logs"), clicked=self.open_logs_requested))
        row.addWidget(self.upkeep_status, 1)
        return box

    def set_profile(self, profile: Profile) -> None:
        self.profile = profile
        self.fps.blockSignals(True)
        self.fps.setValue(profile.capture.fps)
        self.fps.blockSignals(False)

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

    def _on_tray(self, on: bool) -> None:
        self.config.minimize_to_tray = on
        self.config_changed.emit()

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
