"""The Live page's feed as a list: the Turkish line prominent, the English one small and muted
under it, newest on top. Its rows mirror the developer table (same order, same row numbers)."""

from __future__ import annotations

from PySide6.QtCore import QModelIndex, QPersistentModelIndex, QRect, QSize, Qt
from PySide6.QtGui import QFont, QFontMetrics, QPainter, QPalette
from PySide6.QtWidgets import (
    QAbstractItemView,
    QListWidget,
    QListWidgetItem,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QWidget,
)

TURKISH = Qt.ItemDataRole.UserRole
ENGLISH = Qt.ItemDataRole.UserRole + 1
PAD_X, PAD_Y, GAP = 4, 9, 2
TURKISH_PT, ENGLISH_PT = 12, 9
WRAP = Qt.TextFlag.TextWordWrap


def _fonts(base: QFont) -> tuple[QFont, QFont]:
    turkish, english = QFont(base), QFont(base)
    turkish.setPointSize(TURKISH_PT)
    turkish.setWeight(QFont.Weight.DemiBold)
    english.setPointSize(ENGLISH_PT)
    return turkish, english


class _LineDelegate(QStyledItemDelegate):
    def __init__(self, view: QListWidget) -> None:
        super().__init__(view)
        self._view = view

    def _layout(self, width: int, index: QModelIndex | QPersistentModelIndex) -> tuple[int, int]:
        turkish, english = _fonts(self._view.font())
        inner = max(1, width - 2 * PAD_X)
        top = QFontMetrics(turkish).boundingRect(0, 0, inner, 10_000, WRAP, index.data(TURKISH))
        low = QFontMetrics(english).boundingRect(0, 0, inner, 10_000, WRAP, index.data(ENGLISH))
        return top.height(), low.height()

    def sizeHint(  # noqa: N802 - Qt override
        self, option: QStyleOptionViewItem, index: QModelIndex | QPersistentModelIndex
    ) -> QSize:
        width = self._view.viewport().width()
        top, low = self._layout(width, index)
        return QSize(width, PAD_Y + top + GAP + low + PAD_Y)

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionViewItem,
        index: QModelIndex | QPersistentModelIndex,
    ) -> None:
        rect: QRect = option.rect  # type: ignore[attr-defined]
        palette: QPalette = option.palette  # type: ignore[attr-defined]
        turkish, english = _fonts(self._view.font())
        top, low = self._layout(rect.width(), index)
        painter.save()
        inner = rect.adjusted(PAD_X, PAD_Y, -PAD_X, -PAD_Y)
        painter.setFont(turkish)
        painter.setPen(palette.color(QPalette.ColorRole.Text))
        painter.drawText(
            QRect(inner.left(), inner.top(), inner.width(), top), WRAP, index.data(TURKISH)
        )
        painter.setFont(english)
        painter.setPen(palette.color(QPalette.ColorRole.PlaceholderText))
        painter.drawText(
            QRect(inner.left(), inner.top() + top + GAP, inner.width(), low),
            WRAP,
            index.data(ENGLISH),
        )
        if index.row() < index.model().rowCount() - 1:  # hairline between lines, none at the end
            painter.setPen(palette.color(QPalette.ColorRole.Mid))
            painter.drawLine(
                rect.left() + PAD_X, rect.bottom(), rect.right() - PAD_X, rect.bottom()
            )
        painter.restore()


class FeedList(QListWidget):
    """Read-only list of translated lines; row 0 is the newest."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent, objectName="feedList")
        self.setItemDelegate(_LineDelegate(self))
        self.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setResizeMode(QListWidget.ResizeMode.Adjust)  # rewrap on resize
        self.setWordWrap(True)

    def insert_top(self) -> None:
        item = QListWidgetItem()
        item.setData(TURKISH, "")
        item.setData(ENGLISH, "")
        self.insertItem(0, item)

    def remove_last(self) -> None:
        self.takeItem(self.count() - 1)

    def set_line(self, row: int, turkish: str, english: str) -> None:
        item = self.item(row)
        item.setData(TURKISH, turkish)
        item.setData(ENGLISH, english)

    def line(self, row: int) -> tuple[str, str]:
        item = self.item(row)
        return item.data(TURKISH), item.data(ENGLISH)
