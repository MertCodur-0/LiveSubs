"""Drag a rectangle anywhere on the desktop to mark where subtitles appear.

Every monitor gets a dimmed full-screen layer; the user draws on whichever shows the game. The
result is a rectangle in Qt's global logical coordinates; ``ui.pick`` turns it into a source and a
region.
"""

from __future__ import annotations

from PySide6.QtCore import QObject, QPoint, QRect, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QCursor,
    QGuiApplication,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPen,
    QScreen,
)
from PySide6.QtWidgets import QWidget

from livesubs.core.geometry import Rect
from livesubs.i18n import t

MIN_SIZE_PX = 8


class _ScreenLayer(QWidget):
    """One monitor's dimmed layer."""

    def __init__(self, picker: RegionPicker, screen: QScreen) -> None:
        super().__init__(
            None,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool,
        )
        self._picker = picker
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setScreen(screen)
        self.setGeometry(screen.geometry())
        self._origin: QPoint | None = None
        self._selection = QRect()

    def selection_global(self) -> Rect | None:
        sel = self._selection.normalized().intersected(self.rect())
        if sel.width() < MIN_SIZE_PX or sel.height() < MIN_SIZE_PX:
            return None
        top_left = self.mapToGlobal(sel.topLeft())
        return Rect(top_left.x(), top_left.y(), sel.width(), sel.height())

    def clear_selection(self) -> None:
        self._selection = QRect()
        self.update()

    # --- input -----------------------------------------------------------------------------------

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt override
        if event.button() == Qt.MouseButton.LeftButton:
            self._picker.focus(self)
            self._origin = event.position().toPoint()
            self._selection = QRect(self._origin, self._origin)
            self.update()
        elif event.button() == Qt.MouseButton.RightButton:
            self._picker.cancel()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt override
        if self._origin is not None:
            self._selection = QRect(self._origin, event.position().toPoint())
            self.update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt override
        self._origin = None

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt override
        self._picker.confirm()

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 - Qt override
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._picker.confirm()
        elif event.key() == Qt.Key.Key_Escape:
            self._picker.cancel()

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
        font.setPointSize(14)
        painter.setFont(font)
        painter.drawText(
            self.rect().adjusted(0, 40, 0, 0),
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
            t("region.hint"),
        )
        painter.end()


class RegionPicker(QObject):
    """Covers every monitor; emits ``selected(Rect)`` in global logical coordinates."""

    selected = Signal(object)  # Rect
    cancelled = Signal()

    def __init__(self) -> None:
        super().__init__()
        self._layers = [_ScreenLayer(self, screen) for screen in QGuiApplication.screens()]
        self._active: _ScreenLayer | None = None

    def show(self) -> None:
        for layer in self._layers:
            layer.show()
        under_cursor = QGuiApplication.screenAt(QCursor.pos())
        for layer in self._layers:
            if layer.screen() is under_cursor:
                layer.activateWindow()

    def focus(self, layer: _ScreenLayer) -> None:
        """A drag started on ``layer``: a selection lives on one monitor only."""
        self._active = layer
        for other in self._layers:
            if other is not layer:
                other.clear_selection()
        layer.activateWindow()

    def confirm(self) -> None:
        rect = self._active.selection_global() if self._active else None
        if rect is None:
            return
        self._close()
        self.selected.emit(rect)

    def cancel(self) -> None:
        self._close()
        self.cancelled.emit()

    def _close(self) -> None:
        for layer in self._layers:
            layer.close()
        self._layers = []
