"""Drag a rectangle over the source to mark where its subtitles appear.

A dimmed, frameless window covers the source's area on screen (its client area or monitor);
the selection is returned normalized to that area, i.e. to the source's frame space.
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, QRect, Qt, Signal
from PySide6.QtGui import QColor, QKeyEvent, QMouseEvent, QPainter, QPen
from PySide6.QtWidgets import QWidget

from livesubs.core.geometry import NormRect, Rect
from livesubs.i18n import t
from livesubs.ui.screens import qrect

MIN_SIZE_PX = 8


class RegionSelector(QWidget):
    selected = Signal(object)  # NormRect
    cancelled = Signal()

    def __init__(self, area_logical: Rect, initial: NormRect | None = None) -> None:
        super().__init__(
            None,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setGeometry(qrect(area_logical))
        self._origin: QPoint | None = None
        self._selection = QRect()
        if initial is not None:
            r = initial.to_pixels(area_logical.width, area_logical.height)
            self._selection = QRect(r.x, r.y, r.width, r.height)

    def normalized(self) -> NormRect | None:
        sel = self._selection.normalized().intersected(self.rect())
        if sel.width() < MIN_SIZE_PX or sel.height() < MIN_SIZE_PX:
            return None
        return NormRect.from_pixels(
            Rect(sel.x(), sel.y(), sel.width(), sel.height()), self.width(), self.height()
        )

    # --- input -----------------------------------------------------------------------------------

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt override
        if event.button() == Qt.MouseButton.LeftButton:
            self._origin = event.position().toPoint()
            self._selection = QRect(self._origin, self._origin)
            self.update()
        elif event.button() == Qt.MouseButton.RightButton:
            self._cancel()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt override
        if self._origin is not None:
            self._selection = QRect(self._origin, event.position().toPoint())
            self.update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt override
        self._origin = None

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt override
        self._confirm()

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 - Qt override
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._confirm()
        elif event.key() == Qt.Key.Key_Escape:
            self._cancel()

    def _confirm(self) -> None:
        region = self.normalized()
        if region is not None:
            self.selected.emit(region)
            self.close()

    def _cancel(self) -> None:
        self.cancelled.emit()
        self.close()

    # --- drawing ---------------------------------------------------------------------------------

    def paintEvent(self, event: object) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(0, 0, 0, 110))
        sel = self._selection.normalized()
        if not sel.isEmpty():
            # Alpha 1, not 0: fully transparent pixels of a layered window pass clicks through,
            # and a new drag must be able to start inside the old selection.
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
            painter.fillRect(sel, QColor(0, 0, 0, 1))
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
            painter.setPen(QPen(QColor("#00E5FF"), 2))
            painter.drawRect(sel.adjusted(0, 0, -1, -1))
        painter.setPen(QColor("white"))
        font = painter.font()
        font.setPointSize(13)
        painter.setFont(font)
        painter.drawText(
            self.rect().adjusted(0, 20, 0, 0),
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
            t("region.hint"),
        )
        painter.end()
