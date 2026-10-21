"""The subtitle region drawn on screen in calibration mode; drag or resize it to adjust.

Hidden from screen capture like the overlay, so a monitor source never reads its border.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtGui import QColor, QGuiApplication, QMouseEvent, QPainter, QPen
from PySide6.QtWidgets import QWidget

from livesubs.core.geometry import Rect
from livesubs.i18n import t
from livesubs.platform import win32
from livesubs.ui.movable import MoveResize
from livesubs.ui.screens import qrect

log = logging.getLogger(__name__)


class RegionFrame(QWidget):
    edited = Signal(object)  # Rect, logical

    def __init__(self) -> None:
        super().__init__(
            None,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setMouseTracking(True)
        self._mover = MoveResize(self, self.edited.emit, QPoint(40, 16))

    def place(self, logical: Rect) -> None:
        if not self._mover.dragging:
            self.setGeometry(qrect(logical))

    def showEvent(self, event: object) -> None:  # noqa: N802 - Qt override
        super().showEvent(event)
        if QGuiApplication.platformName() == "windows":
            win32.set_exclude_from_capture(int(self.winId()), True)

    def paintEvent(self, event: object) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        # A faint fill (not alpha 0) so the inside takes the mouse too.
        painter.fillRect(self.rect(), QColor(0, 229, 255, 22))
        painter.setPen(QPen(QColor("#00E5FF"), 2))
        painter.drawRect(self.rect().adjusted(1, 1, -2, -2))
        label = t("region_frame.label")
        metrics = painter.fontMetrics()
        box = metrics.boundingRect(label).adjusted(-6, -3, 6, 3)
        box.moveTopLeft(QPoint(4, 4))
        painter.fillRect(box, QColor(0, 0, 0, 170))
        painter.setPen(QColor("#00E5FF"))
        painter.drawText(box, Qt.AlignmentFlag.AlignCenter, label)
        painter.end()

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt override
        self._mover.handle_press(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt override
        self._mover.handle_move(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt override
        self._mover.handle_release(event)
