"""The mouse wheel scrolls the page and never changes a combo box, spin box or slider."""

import pytest
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QScrollArea,
    QSlider,
    QSpinBox,
    QTableWidget,
    QVBoxLayout,
    QWidget,
)
from pytestqt.qtbot import QtBot

from livesubs.ui import wheel


def _wheel(widget: QWidget, steps: int = -1) -> None:
    """One notch of the wheel over the widget's center (negative = down)."""
    center = QPointF(widget.rect().center())
    event = QWheelEvent(
        center,
        QPointF(widget.mapToGlobal(widget.rect().center())),
        QPoint(),
        QPoint(0, 120 * steps),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.NoScrollPhase,
        False,
    )
    QApplication.sendEvent(widget, event)


@pytest.fixture
def page(qtbot: QtBot, qapp: QApplication) -> tuple[QScrollArea, QComboBox, QSpinBox, QSlider]:
    wheel.install(qapp)
    content = QWidget()
    layout = QVBoxLayout(content)
    combo = QComboBox()
    combo.addItems(["a", "b", "c"])
    spin = QSpinBox()
    spin.setRange(0, 100)
    spin.setValue(50)
    slider = QSlider(Qt.Orientation.Horizontal)
    slider.setRange(0, 100)
    slider.setValue(50)
    for widget in (combo, spin, slider):
        layout.addWidget(widget)
    layout.addSpacing(2000)  # a long page: the scroll area can scroll
    area = QScrollArea(widgetResizable=True)
    area.setWidget(content)
    area.resize(300, 200)
    qtbot.addWidget(area)
    area.show()
    qtbot.waitExposed(area)
    return area, combo, spin, slider


def test_wheel_scrolls_the_page_instead_of_changing_values(
    page: tuple[QScrollArea, QComboBox, QSpinBox, QSlider],
) -> None:
    area, combo, spin, slider = page
    bar = area.verticalScrollBar()
    for widget in (combo, spin, spin.lineEdit(), slider):
        widget.setFocus()  # even a focused control leaves the wheel to the page
        before = bar.value()
        _wheel(widget)
        assert bar.value() > before, widget
    assert (combo.currentIndex(), spin.value(), slider.value()) == (0, 50, 50)


def test_wheel_still_scrolls_lists_and_tables(qtbot: QtBot, qapp: QApplication) -> None:
    wheel.install(qapp)
    table = QTableWidget(200, 2)  # e.g. the glossary
    table.resize(300, 200)
    qtbot.addWidget(table)
    table.show()
    qtbot.waitExposed(table)
    _wheel(table.viewport())
    assert table.verticalScrollBar().value() > 0


def test_controls_outside_a_scroll_area_ignore_the_wheel(qtbot: QtBot, qapp: QApplication) -> None:
    wheel.install(qapp)
    combo = QComboBox()  # e.g. in the "Kaynak ve model" sheet
    combo.addItems(["a", "b"])
    qtbot.addWidget(combo)
    combo.show()
    _wheel(combo)
    assert combo.currentIndex() == 0
