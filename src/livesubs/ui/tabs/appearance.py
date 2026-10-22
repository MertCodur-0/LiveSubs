"""Overlay appearance: where the translation goes and how it looks. Edits apply at once."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDoubleSpinBox,
    QFontComboBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from livesubs.core.profiles import OverlayDisplay, OverlayPlacement, OverlaySettings
from livesubs.i18n import t

PLACEMENTS = (
    OverlayPlacement.ABOVE,
    OverlayPlacement.OVER,
    OverlayPlacement.BELOW,
    OverlayPlacement.CUSTOM,
)


class _ColorButton(QPushButton):
    def __init__(self, color: str, on_pick: Callable[[str], None]) -> None:
        super().__init__()
        self.setFixedWidth(64)
        self._on_pick = on_pick
        self.set_color(color)
        self.clicked.connect(self._pick)

    def set_color(self, color: str) -> None:
        self._color = color
        self.setStyleSheet(f"background-color: {color}; border: 1px solid #888; min-height: 20px;")

    def _pick(self) -> None:
        chosen = QColorDialog.getColor(QColor(self._color), self, t("appearance.pick_color"))
        if chosen.isValid():
            self.set_color(chosen.name().upper())
            self._on_pick(self._color)


class AppearanceTab(QWidget):
    changed = Signal()
    preview_toggled = Signal(bool)

    def __init__(self, settings: OverlaySettings) -> None:
        super().__init__()
        self.settings = settings
        form = QFormLayout()

        self.placement = QComboBox()
        for placement in PLACEMENTS:
            self.placement.addItem(t(f"appearance.placement.{placement}"), placement)
        self.placement.currentIndexChanged.connect(
            lambda i: self._set("placement", self.placement.itemData(i))
        )
        form.addRow(t("appearance.placement"), self.placement)

        self.background = QCheckBox(t("appearance.background"))
        self.background.toggled.connect(self._on_background)
        self.opacity = QSlider(Qt.Orientation.Horizontal)
        self.opacity.setRange(0, 100)
        self.opacity_label = QLabel()
        self.opacity.valueChanged.connect(self._on_opacity)
        row = QHBoxLayout()
        row.addWidget(self.background)
        row.addWidget(self.opacity, 1)
        row.addWidget(self.opacity_label)
        form.addRow(t("appearance.box"), row)

        self.font = QFontComboBox()
        self.font.currentFontChanged.connect(lambda f: self._set("font_family", f.family()))
        self.size = QSpinBox(suffix=" pt")
        self.size.setRange(10, 72)
        self.size.valueChanged.connect(lambda v: self._set("font_size_pt", v))
        row = QHBoxLayout()
        row.addWidget(self.font, 1)
        row.addWidget(self.size)
        form.addRow(t("appearance.font"), row)

        self.text_color = _ColorButton(settings.text_color, lambda c: self._set("text_color", c))
        self.outline_color = _ColorButton(
            settings.outline_color, lambda c: self._set("outline_color", c)
        )
        self.outline = QDoubleSpinBox(suffix=" px", decimals=1, singleStep=0.5)
        self.outline.setRange(0, 8)
        self.outline.valueChanged.connect(lambda v: self._set("outline_px", v))
        self.shadow = QCheckBox(t("appearance.shadow"))
        self.shadow.toggled.connect(lambda v: self._set("shadow", v))
        row = QHBoxLayout()
        row.addWidget(QLabel(t("appearance.text_color")))
        row.addWidget(self.text_color)
        row.addSpacing(12)
        row.addWidget(QLabel(t("appearance.outline")))
        row.addWidget(self.outline_color)
        row.addWidget(self.outline)
        row.addSpacing(12)
        row.addWidget(self.shadow)
        row.addStretch(1)
        form.addRow(t("appearance.colors"), row)

        self.max_lines = QSpinBox()
        self.max_lines.setRange(1, 4)
        self.max_lines.valueChanged.connect(lambda v: self._set("max_lines", v))
        form.addRow(t("appearance.max_lines"), self.max_lines)

        self.show_original = QCheckBox(t("appearance.show_original"))
        self.show_original.toggled.connect(lambda v: self._set("show_original", v))
        form.addRow("", self.show_original)

        self.clear_delay = QDoubleSpinBox(suffix=" sn", decimals=1, singleStep=0.5)
        self.clear_delay.setRange(0, 10)
        self.clear_delay.valueChanged.connect(
            lambda v: self._set("clear_delay_ms", round(v * 1000))
        )
        form.addRow(t("appearance.clear_delay"), self.clear_delay)

        self.preview = QPushButton(t("appearance.preview"), checkable=True)
        self.preview.toggled.connect(self.preview_toggled)
        hint = QLabel(t("appearance.hint"))
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #9a9a9a;")

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.preview)
        layout.addWidget(hint)
        layout.addStretch(1)
        self.refresh()

    def set_settings(self, settings: OverlaySettings) -> None:
        """Edit another profile's overlay."""
        self.settings = settings
        self.refresh()

    def refresh(self) -> None:
        """Show the current settings (e.g. after the overlay was moved in calibration mode)."""
        s = self.settings
        widgets = (
            self.placement,
            self.background,
            self.opacity,
            self.font,
            self.size,
            self.outline,
            self.shadow,
            self.max_lines,
            self.show_original,
            self.clear_delay,
        )
        for widget in widgets:
            widget.blockSignals(True)
        self.placement.setCurrentIndex(PLACEMENTS.index(s.placement))
        # On a chosen monitor (Profiles tab) the translation sits where it was moved to.
        on_monitor = s.display == OverlayDisplay.MONITOR
        self.placement.setEnabled(not on_monitor)
        self.placement.setToolTip(t("appearance.placement_on_monitor") if on_monitor else "")
        self.background.setChecked(s.background)
        self.opacity.setValue(round(s.background_opacity * 100))
        self.opacity.setEnabled(s.background)
        self.opacity_label.setText(f"%{round(s.background_opacity * 100)}")
        self.font.setCurrentFont(QFont(s.font_family))
        self.size.setValue(s.font_size_pt)
        self.text_color.set_color(s.text_color)
        self.outline_color.set_color(s.outline_color)
        self.outline.setValue(s.outline_px)
        self.shadow.setChecked(s.shadow)
        self.max_lines.setValue(s.max_lines)
        self.show_original.setChecked(s.show_original)
        self.clear_delay.setValue(s.clear_delay_ms / 1000)
        for widget in widgets:
            widget.blockSignals(False)

    def _set(self, field: str, value: object) -> None:
        setattr(self.settings, field, value)  # validated by the settings model
        self.changed.emit()

    def _on_background(self, enabled: bool) -> None:
        self.opacity.setEnabled(enabled)
        self._set("background", enabled)

    def _on_opacity(self, percent: int) -> None:
        self.opacity_label.setText(f"%{percent}")
        self._set("background_opacity", percent / 100)
