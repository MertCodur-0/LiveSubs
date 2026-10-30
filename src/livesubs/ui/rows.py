"""macOS Settings-style cards: a title above, rows inside with the label on the left, the control
on the right and a hairline between rows. Long explanations go into an (i) tooltip."""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLayout,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from livesubs.ui import icons

LABEL_WIDTH = 190  # the label column, so controls line up across rows and cards
ROW_MARGINS = (0, 7, 0, 7)
INFO_PX = 15
INFO_COLOR = QColor("#8E8E93")  # neutral gray: readable on every theme's card


def info(text: str) -> QLabel:
    """A small (i) that shows ``text`` as its tooltip."""
    label = QLabel(objectName="info")
    label.setPixmap(icons.pixmap("info", INFO_COLOR, INFO_PX, 2.0))
    label.setToolTip(text)
    label.setCursor(Qt.CursorShape.WhatsThisCursor)
    return label


def labeled(text: str, tip: str) -> QWidget:
    """A form label with an (i) after it, for QFormLayout rows."""
    widget = QWidget()
    row = QHBoxLayout(widget)
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(6)
    row.addWidget(QLabel(text))
    row.addWidget(info(tip))
    row.addStretch(1)
    return widget


def hint(text: str) -> QLabel:
    label = QLabel(text, wordWrap=True)
    label.setProperty("hint", True)  # muted, styled by the theme
    return label


def separator() -> QFrame:
    line = QFrame(objectName="separator")
    line.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
    return line


class RowCard(QGroupBox):
    """A card of rows; without a title it sits flush (no room kept for one)."""

    def __init__(self, title: str = "") -> None:
        super().__init__(title)
        if not title:
            self.setProperty("untitled", True)  # no title margin (theme)
        self._outer = QHBoxLayout(self)
        self._outer.setContentsMargins(0, 0, 0, 0)
        self._outer.setSpacing(16)
        self._rows = QVBoxLayout()
        self._rows.setContentsMargins(0, 0, 0, 0)
        self._rows.setSpacing(0)
        self._outer.addLayout(self._rows, 1)

    def set_side(self, widget: QWidget) -> None:
        """A widget beside the rows, at the top right (the phone output's QR code)."""
        self._outer.addWidget(widget, 0, Qt.AlignmentFlag.AlignTop)

    def _add(self, item: QWidget | QLayout) -> None:
        if self._rows.count():
            self._rows.addWidget(separator())
        if isinstance(item, QLayout):
            self._rows.addLayout(item)
        else:
            self._rows.addWidget(item)

    def add_row(
        self,
        label: str,
        *controls: QWidget | QLayout,
        tip: str = "",
        expand: bool = True,
    ) -> QHBoxLayout:
        """A labeled row. ``expand``: the controls take the rest of the width (fields, combos);
        otherwise they sit at the right edge (check boxes, buttons)."""
        row = QHBoxLayout()
        row.setContentsMargins(*ROW_MARGINS)
        row.setSpacing(8)
        # The label and its (i) share the label column, so controls line up across rows.
        title = labeled(label, tip) if tip else QLabel(label)
        title.setMinimumWidth(LABEL_WIDTH if expand else 0)
        row.addWidget(title)
        if not expand:
            row.addStretch(1)
        for control in controls:
            if isinstance(control, QLayout):
                row.addLayout(control, 1 if expand else 0)
            else:
                row.addWidget(control, 1 if expand else 0)
        self._add(row)
        return row

    def add_widget(self, widget: QWidget, *, tip: str = "") -> None:
        """A row that is one widget (a check box, a status line), with an optional (i)."""
        row = QHBoxLayout()
        row.setContentsMargins(*ROW_MARGINS)
        row.setSpacing(8)
        row.addWidget(widget, 1)
        if tip:
            row.addWidget(info(tip))
        self._add(row)

    def add_layout(self, layout: QLayout) -> None:
        wrapper = QHBoxLayout()
        wrapper.setContentsMargins(*ROW_MARGINS)
        wrapper.addLayout(layout)
        self._add(wrapper)


class Disclosure(QWidget):
    """A title that folds its card in and out ("Gelişmiş"); folded at first."""

    def __init__(self, title: str, card: QWidget) -> None:
        super().__init__()
        self.card = card
        self.toggle = QToolButton(objectName="disclosure", text=title, checkable=True)
        self.toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.toggle.setArrowType(Qt.ArrowType.RightArrow)
        self.toggle.setIconSize(QSize(10, 10))
        self.toggle.toggled.connect(self.set_open)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(4)
        layout.addWidget(self.toggle, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(card)
        card.hide()

    def set_open(self, on: bool) -> None:
        self.toggle.setChecked(on)
        self.toggle.setArrowType(Qt.ArrowType.DownArrow if on else Qt.ArrowType.RightArrow)
        self.card.setVisible(on)
