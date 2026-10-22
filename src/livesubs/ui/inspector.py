"""Calibration Inspector: what the pipeline sees, and the preprocessing knobs that matter most.

Shows the captured subtitle region, the OCR input after preprocessing, how much of the region
looks like text, and the last OCR read with the filter's verdict. Edits to the vision settings
apply to the running pipeline at once (``vision_changed``).
"""

from __future__ import annotations

import numpy as np
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QGuiApplication, QImage, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QColorDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from livesubs.core.messages import DebugFrame
from livesubs.core.profiles import VisionSettings
from livesubs.i18n import t
from livesubs.platform import win32

WHITE, YELLOW = "#FFFFFF", "#FFFF00"


def to_pixmap(image: np.ndarray) -> QPixmap:
    height, width = image.shape[:2]
    if image.ndim == 2:
        qimage = QImage(
            image.data, width, height, image.strides[0], QImage.Format.Format_Grayscale8
        )
    else:
        qimage = QImage(image.data, width, height, image.strides[0], QImage.Format.Format_BGR888)
    return QPixmap.fromImage(qimage.copy())  # copy: the numpy buffer is not kept alive


class Inspector(QWidget):
    vision_changed = Signal()

    def __init__(self, vision: VisionSettings) -> None:
        super().__init__(None, Qt.WindowType.Window | Qt.WindowType.WindowStaysOnTopHint)
        self.setWindowTitle(t("inspector.title"))
        self.resize(760, 640)
        self.vision = vision
        self._loading = False  # showing settings: no change events

        self.region_image = QLabel(t("inspector.waiting"))
        self.ocr_image = QLabel()
        for label in (self.region_image, self.ocr_image):
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            label.setMinimumHeight(90)
            label.setStyleSheet("background: #111; border: 1px solid #333;")
        self.stats = QLabel()
        self.last_read = QLabel()
        self.last_read.setWordWrap(True)
        self.last_read.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(t("inspector.region")))
        layout.addWidget(self.region_image)
        layout.addWidget(QLabel(t("inspector.ocr_input")))
        layout.addWidget(self.ocr_image)
        layout.addWidget(self.stats)
        layout.addWidget(self.last_read)
        layout.addWidget(self._settings_box())
        layout.addStretch(1)
        self.refresh()

    def set_vision(self, vision: VisionSettings) -> None:
        """Edit another profile's settings."""
        self.vision = vision
        self.refresh()

    def refresh(self) -> None:
        self._loading = True
        try:
            v = self.vision
            others = [c for c in v.text_colors if c not in (WHITE, YELLOW)]
            if others:
                self._custom = others[0]
            self._paint_custom()
            self.white.setChecked(WHITE in v.text_colors)
            self.yellow.setChecked(YELLOW in v.text_colors)
            self.custom.setChecked(bool(others))
            self.tolerance.slider.setValue(v.color_tolerance)
            self.contrast.setChecked(v.local_contrast)
            self.min_contrast.slider.setValue(v.min_local_contrast)
        finally:
            self._loading = False

    # --- settings --------------------------------------------------------------------------------

    def _settings_box(self) -> QGroupBox:
        box = QGroupBox(t("inspector.settings"))
        form = QFormLayout(box)

        self.white = QCheckBox(t("inspector.white"))
        self.yellow = QCheckBox(t("inspector.yellow"))
        self.custom = QCheckBox(t("inspector.custom"))
        self.custom_color = QPushButton()
        self.custom_color.setFixedWidth(48)
        self.custom_color.clicked.connect(self._pick_custom)
        self._custom = "#00E5FF"
        for box_ in (self.white, self.yellow, self.custom):
            box_.toggled.connect(self._colors_changed)
        row = QHBoxLayout()
        for widget in (self.white, self.yellow, self.custom, self.custom_color):
            row.addWidget(widget)
        row.addStretch(1)
        form.addRow(t("inspector.colors"), row)

        self.tolerance = self._slider(0, 160, self.vision.color_tolerance, "color_tolerance")
        form.addRow(t("inspector.tolerance"), self.tolerance)

        self.contrast = QCheckBox(t("inspector.local_contrast"))
        self.contrast.toggled.connect(lambda v: self._set("local_contrast", v))
        self.min_contrast = self._slider(
            10, 160, self.vision.min_local_contrast, "min_local_contrast"
        )
        row = QHBoxLayout()
        row.addWidget(self.contrast)
        row.addWidget(self.min_contrast, 1)
        form.addRow(t("inspector.contrast"), row)

        hint = QLabel(t("inspector.hint"))
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #9a9a9a;")
        form.addRow(hint)
        return box

    def _slider(self, low: int, high: int, value: int, field: str) -> QWidget:
        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(low, high)
        slider.setValue(value)
        label = QLabel(str(value))
        label.setMinimumWidth(32)
        slider.valueChanged.connect(lambda v: (label.setText(str(v)), self._set(field, v)))
        container = QWidget()
        row = QHBoxLayout(container)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(slider, 1)
        row.addWidget(label)
        container.slider = slider  # type: ignore[attr-defined]  # for tests
        return container

    def _set(self, field: str, value: object) -> None:
        if self._loading:
            return
        setattr(self.vision, field, value)
        self.vision_changed.emit()

    def _colors_changed(self) -> None:
        if self._loading:
            return
        colors = [c for c, box in ((WHITE, self.white), (YELLOW, self.yellow)) if box.isChecked()]
        if self.custom.isChecked():
            colors.append(self._custom)
        if not colors:  # at least one color: keep white
            self.white.setChecked(True)
            return
        self._set("text_colors", colors)

    def _pick_custom(self) -> None:
        chosen = QColorDialog.getColor(QColor(self._custom), self, t("appearance.pick_color"))
        if chosen.isValid():
            self._custom = chosen.name().upper()
            self._paint_custom()
            self.custom.setChecked(True)
            self._colors_changed()

    def _paint_custom(self) -> None:
        self.custom_color.setStyleSheet(
            f"background-color: {self._custom}; border: 1px solid #888;"
        )

    # --- frames ----------------------------------------------------------------------------------

    def show_frame(self, frame: DebugFrame) -> None:
        width = max(self.region_image.width() - 4, 100)
        for label, image in ((self.region_image, frame.region), (self.ocr_image, frame.ocr_input)):
            pixmap = to_pixmap(image)
            label.setPixmap(pixmap.scaledToWidth(min(width, pixmap.width() * 2)))
        self.stats.setText(
            t("inspector.stats", fraction=frame.text_fraction, change=t(f"change.{frame.change}"))
        )
        if not frame.last_read:
            self.last_read.setText(t("inspector.no_read"))
        elif frame.rejected:
            self.last_read.setText(
                t(
                    "inspector.read_rejected",
                    text=frame.last_read,
                    reason=t(f"reject.{frame.rejected}"),
                )
            )
        else:
            self.last_read.setText(t("inspector.read_ok", text=frame.last_read))

    def showEvent(self, event: object) -> None:  # noqa: N802 - Qt override
        super().showEvent(event)
        if QGuiApplication.platformName() == "windows":
            # A monitor source on the same screen must not capture its own debug view.
            win32.set_exclude_from_capture(int(self.winId()), True)
