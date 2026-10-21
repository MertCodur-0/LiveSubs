"""Move and resize a frameless window with the mouse (calibration mode).

Drag inside to move, drag within ``EDGE_PX`` of an edge or corner to resize. The widget calls
``handle_press/move/release`` from its mouse events; ``on_edited`` gets the final geometry.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QWidget

from livesubs.core.geometry import Rect
from livesubs.ui.screens import from_qrect

EDGE_PX = 10

_LEFT, _RIGHT = Qt.Edge.LeftEdge.value, Qt.Edge.RightEdge.value
_TOP, _BOTTOM = Qt.Edge.TopEdge.value, Qt.Edge.BottomEdge.value


class MoveResize:
    def __init__(
        self, widget: QWidget, on_edited: Callable[[Rect], None], min_size: QPoint
    ) -> None:
        self._widget = widget
        self._on_edited = on_edited
        self._min = min_size
        self._drag: tuple[int, QPoint, QRect] | None = None  # edges, start, geometry

    @property
    def dragging(self) -> bool:
        return self._drag is not None

    def edges_at(self, pos: QPoint) -> int:
        w = self._widget
        edges = 0
        if pos.x() < EDGE_PX:
            edges |= _LEFT
        if pos.x() > w.width() - EDGE_PX:
            edges |= _RIGHT
        if pos.y() < EDGE_PX:
            edges |= _TOP
        if pos.y() > w.height() - EDGE_PX:
            edges |= _BOTTOM
        return edges

    def handle_press(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag = (
                self.edges_at(event.position().toPoint()),
                event.globalPosition().toPoint(),
                self._widget.geometry(),
            )

    def handle_move(self, event: QMouseEvent) -> None:
        if self._drag is None:
            self._widget.setCursor(cursor_for(self.edges_at(event.position().toPoint())))
            return
        edges, start, geometry = self._drag
        delta = event.globalPosition().toPoint() - start
        rect = QRect(geometry)
        if not edges:
            rect.translate(delta)
        if edges & _LEFT:
            rect.setLeft(min(rect.left() + delta.x(), rect.right() - self._min.x()))
        if edges & _RIGHT:
            rect.setRight(max(rect.right() + delta.x(), rect.left() + self._min.x()))
        if edges & _TOP:
            rect.setTop(min(rect.top() + delta.y(), rect.bottom() - self._min.y()))
        if edges & _BOTTOM:
            rect.setBottom(max(rect.bottom() + delta.y(), rect.top() + self._min.y()))
        self._widget.setGeometry(rect)

    def handle_release(self, event: QMouseEvent) -> None:
        if self._drag is not None:
            self._drag = None
            self._on_edited(from_qrect(self._widget.geometry()))


def cursor_for(edges: int) -> Qt.CursorShape:
    horizontal, vertical = edges & (_LEFT | _RIGHT), edges & (_TOP | _BOTTOM)
    if horizontal and vertical:
        if (edges & _LEFT and edges & _TOP) or (edges & _RIGHT and edges & _BOTTOM):
            return Qt.CursorShape.SizeFDiagCursor
        return Qt.CursorShape.SizeBDiagCursor
    if horizontal:
        return Qt.CursorShape.SizeHorCursor
    if vertical:
        return Qt.CursorShape.SizeVerCursor
    return Qt.CursorShape.SizeAllCursor
