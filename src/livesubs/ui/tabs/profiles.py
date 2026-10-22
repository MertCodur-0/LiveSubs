"""Profiles tab: manage per-game profiles and edit what the active one tells the translator.

Profile management (new, copy, rename, delete) is announced through signals; the main window
owns the profile store and the switching. The editors change the active ``Profile`` in place.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from livesubs.core.profiles import OverlayDisplay, Profile, infer_game_on_this_pc
from livesubs.i18n import t
from livesubs.platform import win32

GAME_LOCATIONS = (None, True, False)  # VramSettings.game_on_this_pc: auto, this PC, elsewhere


def _hint(text: str) -> QLabel:
    label = QLabel(text, wordWrap=True)
    label.setProperty("hint", True)  # muted, styled by the theme
    return label


class ProfilesTab(QWidget):
    changed = Signal()  # the active profile was edited
    new_requested = Signal(str)  # name
    copy_requested = Signal(str)  # name of the copy
    rename_requested = Signal(str)  # new name
    delete_requested = Signal()

    def __init__(
        self,
        profile: Profile,
        monitors: Callable[[], list[win32.MonitorInfo]] = win32.list_monitors,
    ) -> None:
        super().__init__()
        self.profile = profile
        self._monitors = monitors
        self._loading = False
        layout = QVBoxLayout(self)
        layout.addWidget(self._manage_box())
        layout.addWidget(self._translation_box(), 1)
        layout.addWidget(self._game_box())
        layout.addStretch(0)
        self.refresh()

    # --- groups ----------------------------------------------------------------------------------

    def _manage_box(self) -> QGroupBox:
        box = QGroupBox(t("profiles.manage"))
        layout = QVBoxLayout(box)
        self.active_label = QLabel()
        buttons = QHBoxLayout()
        for key, slot in (
            ("profiles.new", self._ask_new),
            ("profiles.copy", self._ask_copy),
            ("profiles.rename", self._ask_rename),
            ("profiles.delete", self._ask_delete),
        ):
            buttons.addWidget(QPushButton(t(key), clicked=slot))
        buttons.addStretch(1)
        layout.addWidget(self.active_label)
        layout.addLayout(buttons)
        layout.addWidget(_hint(t("profiles.hint")))
        return box

    def _translation_box(self) -> QGroupBox:
        box = QGroupBox(t("profiles.translation"))
        form = QFormLayout(box)
        self.context = QPlainTextEdit()
        self.context.setPlaceholderText(t("profiles.context_placeholder"))
        self.context.setMaximumHeight(80)
        self.context.textChanged.connect(self._on_context)
        form.addRow(t("profiles.context"), self.context)

        self.glossary = QTableWidget(0, 2)
        self.glossary.setHorizontalHeaderLabels([t("feed.english"), t("feed.turkish")])
        self.glossary.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.glossary.verticalHeader().hide()
        self.glossary.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.glossary.itemChanged.connect(self._on_glossary)
        add = QPushButton(t("profiles.glossary_add"), clicked=self._add_term)
        remove = QPushButton(t("profiles.glossary_remove"), clicked=self._remove_terms)
        buttons = QHBoxLayout()
        buttons.addWidget(add)
        buttons.addWidget(remove)
        buttons.addStretch(1)
        column = QVBoxLayout()
        column.addWidget(self.glossary, 1)
        column.addLayout(buttons)
        form.addRow(t("profiles.glossary"), column)
        form.addRow(_hint(t("profiles.translation_hint")))
        return box

    def _game_box(self) -> QGroupBox:
        box = QGroupBox(t("profiles.game"))
        form = QFormLayout(box)
        self.location = QComboBox()
        self.location.currentIndexChanged.connect(self._on_location)
        form.addRow(t("profiles.location"), self.location)

        self.reserve_auto = QCheckBox(t("profiles.reserve_auto"))
        self.reserve_auto.toggled.connect(self._on_reserve)
        self.reserve = QDoubleSpinBox(suffix=" GB", decimals=1, singleStep=0.5)
        self.reserve.setRange(0, 64)
        self.reserve.valueChanged.connect(self._on_reserve)
        row = QHBoxLayout()
        row.addWidget(self.reserve_auto)
        row.addWidget(self.reserve)
        row.addStretch(1)
        form.addRow(t("profiles.reserve"), row)

        self.display = QComboBox()
        self.display.currentIndexChanged.connect(self._on_display)
        form.addRow(t("profiles.display"), self.display)
        form.addRow(_hint(t("profiles.game_hint")))
        return box

    # --- showing a profile -----------------------------------------------------------------------

    def set_profile(self, profile: Profile) -> None:
        self.profile = profile
        self.refresh()

    def refresh(self) -> None:
        self._loading = True
        p = self.profile
        try:
            self.active_label.setText(t("profiles.active", name=p.name))
            if self.context.toPlainText() != p.translation.context:
                self.context.setPlainText(p.translation.context)
            self.glossary.setRowCount(0)
            for english, turkish in p.translation.glossary.items():
                self._append_row(english, turkish)

            inferred = infer_game_on_this_pc(p.source)
            self.location.clear()
            auto = t("profiles.guess.this_pc" if inferred else "profiles.guess.elsewhere")
            self.location.addItem(t("profiles.location.auto", guess=auto), None)
            self.location.addItem(t("profiles.location.this_pc"), True)
            self.location.addItem(t("profiles.location.elsewhere"), False)
            self.location.setCurrentIndex(GAME_LOCATIONS.index(p.vram.game_on_this_pc))

            self.reserve_auto.setChecked(p.vram.game_reserve_gb is None)
            self.reserve.setEnabled(p.vram.game_reserve_gb is not None)
            self.reserve.setValue(p.game_vram_reserve_gb())

            self.display.clear()
            self.display.addItem(t("profiles.display.auto"), None)
            for monitor in self._monitors():
                name = monitor.device_name.removeprefix("\\\\.\\")
                label = t(
                    "profiles.display.monitor",
                    name=name,
                    width=monitor.rect.width,
                    height=monitor.rect.height,
                )
                self.display.addItem(label, monitor.device_name)
            index = 0
            if p.overlay.display == OverlayDisplay.MONITOR:
                index = max(self.display.findData(p.overlay.monitor_device), 0)
            self.display.setCurrentIndex(index)
        finally:
            self._loading = False

    def _append_row(self, english: str, turkish: str) -> None:
        row = self.glossary.rowCount()
        self.glossary.insertRow(row)
        self.glossary.setItem(row, 0, QTableWidgetItem(english))
        self.glossary.setItem(row, 1, QTableWidgetItem(turkish))

    # --- edits -----------------------------------------------------------------------------------

    def _edited(self) -> None:
        if not self._loading:
            self.changed.emit()

    def _on_context(self) -> None:
        if self._loading:
            return
        self.profile.translation.context = self.context.toPlainText().strip()
        self._edited()

    def _on_glossary(self) -> None:
        if self._loading:
            return
        terms = {}
        for row in range(self.glossary.rowCount()):
            cells = [self.glossary.item(row, column) for column in (0, 1)]
            english, turkish = (c.text().strip() if c is not None else "" for c in cells)
            if english and turkish:
                terms[english] = turkish
        self.profile.translation.glossary = terms
        self._edited()

    def _add_term(self) -> None:
        self._loading = True
        self._append_row("", "")
        self._loading = False
        self.glossary.setCurrentCell(self.glossary.rowCount() - 1, 0)
        self.glossary.editItem(self.glossary.item(self.glossary.rowCount() - 1, 0))

    def _remove_terms(self) -> None:
        rows = sorted({index.row() for index in self.glossary.selectedIndexes()}, reverse=True)
        for row in rows:
            self.glossary.removeRow(row)
        self._on_glossary()

    def _on_location(self, index: int) -> None:
        if self._loading or index < 0:
            return
        self.profile.vram.game_on_this_pc = self.location.itemData(index)
        if self.profile.vram.game_reserve_gb is None:  # the default follows the location
            self._loading = True
            self.reserve.setValue(self.profile.game_vram_reserve_gb())
            self._loading = False
        self._edited()

    def _on_reserve(self) -> None:
        if self._loading:
            return
        auto = self.reserve_auto.isChecked()
        self.reserve.setEnabled(not auto)
        self.profile.vram.game_reserve_gb = None if auto else self.reserve.value()
        if auto:
            self._loading = True
            self.reserve.setValue(self.profile.game_vram_reserve_gb())
            self._loading = False
        self._edited()

    def _on_display(self, index: int) -> None:
        if self._loading or index < 0:
            return
        device = self.display.itemData(index)
        overlay = self.profile.overlay
        overlay.display = OverlayDisplay.AUTO if device is None else OverlayDisplay.MONITOR
        if device is not None:
            overlay.monitor_device = device
        self._edited()

    # --- management ------------------------------------------------------------------------------

    def _ask_name(self, title_key: str, default: str) -> str | None:
        name, ok = QInputDialog.getText(self, t(title_key), t("profiles.name"), text=default)
        name = name.strip()[:80]
        return name if ok and name else None

    def _ask_new(self) -> None:
        if name := self._ask_name("profiles.new", ""):
            self.new_requested.emit(name)

    def _ask_copy(self) -> None:
        if name := self._ask_name("profiles.copy", t("profiles.copy_name", name=self.profile.name)):
            self.copy_requested.emit(name)

    def _ask_rename(self) -> None:
        if name := self._ask_name("profiles.rename", self.profile.name):
            self.rename_requested.emit(name)

    def _ask_delete(self) -> None:
        answer = QMessageBox.question(
            self, t("profiles.delete"), t("profiles.delete_confirm", name=self.profile.name)
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.delete_requested.emit()
