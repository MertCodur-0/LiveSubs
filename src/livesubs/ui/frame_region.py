"""Pick the subtitle region on a still frame (capture card without a preview on screen).

The frame is shown scaled to fit; the user drags a rectangle over the subtitle. The result is
normalized to the frame, like every region.
"""

from __future__ import annotations

import numpy as np
from PySide6.QtCore import QPoint, QRect, Qt, Signal
from PySide6.QtGui import QColor, QImage, QMouseEvent, QPainter, QPaintEvent, QPen, QPixmap
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QLabel, QVBoxLayout, QWidget

from livesubs.core.geometry import NormRect, Rect
from livesubs.i18n import t

MAX_WIDTH = 1280
MIN_SIDE = 4


def to_pixmap(image: np.ndarray) -> QPixmap:
    height, width = image.shape[:2]
    rgb = np.ascontiguousarray(image[:, :, :3])
    picture = QImage(rgb.data, width, height, rgb.strides[0], QImage.Format.Format_BGR888)
    return QPixmap.fromImage(picture.copy())  # copy: ``rgb`` goes away


class _FrameView(QWidget):
    """The frame with a rubber band; ``selection`` is in widget pixels."""

    changed = Signal()

    def __init__(self, pixmap: QPixmap, region: NormRect | None) -> None:
        super().__init__()
        self._pixmap = pixmap
        self.setFixedSize(pixmap.deviceIndependentSize().toSize())
        self.setCursor(Qt.CursorShape.CrossCursor)
        self._anchor: QPoint | None = None
        self.selection = QRect()
        if region is not None:
            r = region.to_pixels(self.width(), self.height())
            self.selection = QRect(r.x, r.y, r.width, r.height)

    def region(self) -> NormRect | None:
        s = self.selection.intersected(self.rect())
        if s.width() < MIN_SIDE or s.height() < MIN_SIDE:
            return None
        return NormRect.from_pixels(
            Rect(s.x(), s.y(), s.width(), s.height()), self.width(), self.height()
        )

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt override
        self._anchor = event.position().toPoint()
        self.selection = QRect(self._anchor, self._anchor)
        self.update()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt override
        if self._anchor is not None:
            self.selection = QRect(self._anchor, event.position().toPoint()).normalized()
            self.update()

    def mouseReleaseEvent(self, _event: QMouseEvent) -> None:  # noqa: N802 - Qt override
        self._anchor = None
        self.changed.emit()

    def paintEvent(self, _event: QPaintEvent) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        painter.drawPixmap(self.rect(), self._pixmap)
        if self.selection.isEmpty():
            return
        painter.fillRect(self.rect(), QColor(0, 0, 0, 110))
        painter.drawPixmap(self.selection, self._pixmap, self._source_rect(self.selection))
        painter.setPen(QPen(QColor("#00E5FF"), 2))
        painter.drawRect(self.selection.adjusted(0, 0, -1, -1))

    def _source_rect(self, rect: QRect) -> QRect:
        sx = self._pixmap.width() / self.width()
        sy = self._pixmap.height() / self.height()
        return QRect(
            round(rect.x() * sx),
            round(rect.y() * sy),
            round(rect.width() * sx),
            round(rect.height() * sy),
        )


class FrameRegionDialog(QDialog):
    def __init__(
        self, image: np.ndarray, region: NormRect | None, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(t("frame_region.title"))
        pixmap = to_pixmap(image)
        if pixmap.width() > MAX_WIDTH:
            pixmap.setDevicePixelRatio(pixmap.width() / MAX_WIDTH)
        self.view = _FrameView(pixmap, region)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(t("frame_region.hint"), wordWrap=True))
        layout.addWidget(self.view)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        self._ok = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self._ok.setEnabled(self.view.region() is not None)
        self.view.changed.connect(lambda: self._ok.setEnabled(self.view.region() is not None))
        layout.addWidget(buttons)

    def region(self) -> NormRect | None:
        return self.view.region()
