"""The mouse wheel scrolls pages, never values: combo boxes, spin boxes and sliders ignore it.

Qt lets the wheel change whatever control is under the pointer, so scrolling a long page changed
settings by accident. One application-wide event filter hands such wheel events to the scroll
area around the control instead (or drops them where there is none, e.g. in a dialog).
"""

from __future__ import annotations

from PySide6.QtCore import QCoreApplication, QEvent, QObject
from PySide6.QtWidgets import (
    QAbstractScrollArea,
    QAbstractSpinBox,
    QApplication,
    QComboBox,
    QLineEdit,
    QSlider,
    QWidget,
)

GUARDED = (QComboBox, QAbstractSpinBox, QSlider)


class WheelGuard(QObject):
    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802 - Qt override
        if event.type() != QEvent.Type.Wheel or not _guarded(watched):
            return False
        area = _scroll_area(watched)  # type: ignore[arg-type]  # a widget: _guarded checked
        if area is not None:
            bar = area.verticalScrollBar()
            if bar.isVisible():
                QCoreApplication.sendEvent(bar, event)
        return True  # never to the control itself


def _guarded(watched: QObject) -> bool:
    """A guarded control, or the line edit inside one (spin boxes, the editable font combo)."""
    if isinstance(watched, GUARDED):
        return True
    return isinstance(watched, QLineEdit) and isinstance(watched.parentWidget(), GUARDED)


def _scroll_area(widget: QWidget) -> QAbstractScrollArea | None:
    parent = widget.parentWidget()
    while parent is not None:
        if isinstance(parent, QAbstractScrollArea):
            return parent
        parent = parent.parentWidget()
    return None


_guard: WheelGuard | None = None


def install(app: QApplication) -> None:
    """Install the guard once per application."""
    global _guard
    if _guard is None:
        _guard = WheelGuard(app)
        app.installEventFilter(_guard)
