"""Profiles tab: manage per-game profiles and edit what the active one tells the translator.

Profile management (new, copy, rename, delete) is announced through signals; the main window
owns the profile store and the switching. The editors change the active ``Profile`` in place.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
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
from livesubs.core.translation_pack import PackError, load_pack
from livesubs.i18n import t
from livesubs.platform import win32
from livesubs.ui.rows import info, labeled

TABLE_MIN_HEIGHT = 120  # a few rows visible even when the page is short
CONTEXT_HEIGHT = 64  # three lines of the game description
GAME_LOCATIONS = (None, True, False)  # VramSettings.game_on_this_pc: auto, this PC, elsewhere


def _form(box: QWidget) -> QFormLayout:
    """A card's form on the 8 px spacing scale, labels in one column."""
    form = QFormLayout(box)
    form.setHorizontalSpacing(12)
    form.setVerticalSpacing(8)
    return form


class PairTable(QWidget):
    """An English -> Turkish table with add / remove buttons; rows need both columns."""

    edited = Signal()

    def __init__(self) -> None:
        super().__init__()
        self._loading = False
        self.table = QTableWidget(0, 2)
        self.table.setMinimumHeight(TABLE_MIN_HEIGHT)
        self.table.setHorizontalHeaderLabels([t("feed.english"), t("feed.turkish")])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().hide()
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.itemChanged.connect(self._on_item)
        buttons = QHBoxLayout()
        buttons.addWidget(QPushButton(t("profiles.glossary_add"), clicked=self.add_row))
        buttons.addWidget(QPushButton(t("profiles.glossary_remove"), clicked=self.remove_rows))
        buttons.addStretch(1)
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.addWidget(self.table, 1)
        column.addLayout(buttons)

    def pairs(self) -> dict[str, str]:
        pairs = {}
        for row in range(self.table.rowCount()):
            cells = [self.table.item(row, column) for column in (0, 1)]
            english, turkish = (c.text().strip() if c is not None else "" for c in cells)
            if english and turkish:
                pairs[english] = turkish
        return pairs

    def set_pairs(self, pairs: dict[str, str]) -> None:
        self._loading = True
        self.table.setRowCount(0)
        for english, turkish in pairs.items():
            self._append_row(english, turkish)
        self._loading = False

    def add_row(self) -> None:
        self._loading = True
        self._append_row("", "")
        self._loading = False
        last = self.table.rowCount() - 1
        self.table.setCurrentCell(last, 0)
        self.table.editItem(self.table.item(last, 0))

    def remove_rows(self) -> None:
        rows = sorted({index.row() for index in self.table.selectedIndexes()}, reverse=True)
        for row in rows:
            self.table.removeRow(row)
        self.edited.emit()

    def _append_row(self, english: str, turkish: str) -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setItem(row, 0, QTableWidgetItem(english))
        self.table.setItem(row, 1, QTableWidgetItem(turkish))

    def _on_item(self) -> None:
        if not self._loading:
            self.edited.emit()


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
        layout.setContentsMargins(0, 0, 8, 0)
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
        active = QHBoxLayout()
        active.setSpacing(6)
        active.addWidget(self.active_label)
        active.addWidget(info(t("profiles.hint")))
        active.addStretch(1)
        layout.setSpacing(8)
        layout.addLayout(active)
        layout.addLayout(buttons)
        return box

    def _translation_box(self) -> QGroupBox:
        box = QGroupBox(t("profiles.translation"))
        form = _form(box)
        self.context = QPlainTextEdit()
        self.context.setPlaceholderText(t("profiles.context_placeholder"))
        self.context.setFixedHeight(CONTEXT_HEIGHT)
        self.context.textChanged.connect(self._on_context)
        form.addRow(t("profiles.context"), self.context)

        self.glossary = PairTable()
        self.glossary.edited.connect(self._on_glossary)
        form.addRow(labeled(t("profiles.glossary"), t("profiles.glossary_tip")), self.glossary)

        self.examples = PairTable()
        self.examples.edited.connect(self._on_examples)
        form.addRow(labeled(t("profiles.examples"), t("profiles.examples_tip")), self.examples)

        row = QHBoxLayout()
        row.addWidget(QPushButton(t("profiles.import_pack"), clicked=self._ask_import_pack))
        row.addStretch(1)
        form.addRow(row)
        return box

    def _game_box(self) -> QGroupBox:
        box = QGroupBox(t("profiles.game"))
        form = _form(box)
        self.location = QComboBox()
        self.location.currentIndexChanged.connect(self._on_location)
        form.addRow(t("profiles.location"), self.location)

        self.reserve_auto = QCheckBox(t("profiles.reserve_auto"))
        self.reserve_auto.toggled.connect(self._on_reserve)
        self.reserve = QDoubleSpinBox(suffix=" GB", decimals=1, singleStep=0.5)
        self.reserve.setRange(0, 64)
        self.reserve.valueChanged.connect(self._on_reserve)
        row = QHBoxLayout()
        row.setSpacing(12)
        row.addWidget(self.reserve_auto)
        row.addWidget(self.reserve)
        row.addStretch(1)
        form.addRow(labeled(t("profiles.reserve"), t("profiles.reserve_tip")), row)

        self.display = QComboBox()
        self.display.currentIndexChanged.connect(self._on_display)
        form.addRow(labeled(t("profiles.display"), t("profiles.display_tip")), self.display)
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
            self.glossary.set_pairs(p.translation.glossary)
            self.examples.set_pairs(p.translation.examples)

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
            self.display.addItem(t("profiles.display.web_only"), OverlayDisplay.WEB_ONLY)
            index = 0
            if p.overlay.display == OverlayDisplay.MONITOR:
                index = max(self.display.findData(p.overlay.monitor_device), 0)
            elif p.overlay.display == OverlayDisplay.WEB_ONLY:
                index = self.display.count() - 1
            self.display.setCurrentIndex(index)
        finally:
            self._loading = False

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
        self.profile.translation.glossary = self.glossary.pairs()
        self._edited()

    def _on_examples(self) -> None:
        if self._loading:
            return
        self.profile.translation.examples = self.examples.pairs()
        self._edited()

    def _ask_import_pack(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, t("profiles.import_pack"), "", t("profiles.import_filter")
        )
        if not path:
            return
        hints = self.profile.translation
        if hints.context or hints.glossary or hints.examples:
            answer = QMessageBox.question(
                self,
                t("profiles.import_pack"),
                t("profiles.import_replace", name=self.profile.name),
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        self.import_pack(Path(path))

    def import_pack(self, path: Path) -> bool:
        """Replaces the active profile's context, glossary and examples with the pack's."""
        try:
            pack = load_pack(path)
        except PackError as exc:
            QMessageBox.warning(
                self, t("profiles.import_pack"), t("profiles.import_failed", error=exc)
            )
            return False
        self.profile.translation = pack.translation
        self.refresh()
        self.changed.emit()
        QMessageBox.information(
            self,
            t("profiles.import_pack"),
            t(
                "profiles.import_done",
                terms=len(pack.translation.glossary),
                examples=len(pack.translation.examples),
            ),
        )
        return True

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
        if device == OverlayDisplay.WEB_ONLY:
            overlay.display = OverlayDisplay.WEB_ONLY
        elif device is None:
            overlay.display = OverlayDisplay.AUTO
        else:
            overlay.display = OverlayDisplay.MONITOR
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
