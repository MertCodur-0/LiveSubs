"""The translation overlay: frameless, always on top, click-through, hidden from capture.

Hidden from screen capture (``WDA_EXCLUDEFROMCAPTURE``), so OCR never reads our own text even
when the overlay covers the subtitle region. Text is drawn as a ``QPainterPath``: outline first,
fill on top, so it stays readable on any background.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetricsF,
    QGuiApplication,
    QPainter,
    QPainterPath,
    QPen,
)
from PySide6.QtWidgets import QWidget

from livesubs.core.geometry import Rect
from livesubs.core.profiles import OverlaySettings
from livesubs.platform import win32
from livesubs.ui.screens import qrect

log = logging.getLogger(__name__)

MIN_FONT_PT = 12
_PADDING = 10


def wrap(text: str, width_of: Callable[[str], float], max_width: float) -> list[str]:
    """Greedy word wrap; a single word wider than ``max_width`` gets its own line."""
    lines: list[str] = []
    current = ""
    for word in text.split():
        candidate = f"{current} {word}" if current else word
        if current and width_of(candidate) > max_width:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


class Overlay(QWidget):
    def __init__(self, settings: OverlaySettings | None = None) -> None:
        super().__init__(
            None,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowDoesNotAcceptFocus
            | Qt.WindowType.WindowTransparentForInput,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.settings = settings or OverlaySettings()
        self._text = ""
        self._original = ""

    # --- content ---------------------------------------------------------------------------------

    @property
    def text(self) -> str:
        return self._text

    def place(self, logical: Rect) -> None:
        self.setGeometry(qrect(logical))

    def show_text(self, translation: str, original: str = "") -> None:
        self._text, self._original = translation, original
        if not self.isVisible():
            self.show()
        self.update()

    def clear(self) -> None:
        self._text = self._original = ""
        self.update()

    # --- window ----------------------------------------------------------------------------------

    def showEvent(self, event: object) -> None:  # noqa: N802 - Qt override
        super().showEvent(event)
        if QGuiApplication.platformName() != "windows":
            return  # offscreen (tests, CI): there is no real window to style
        hwnd = int(self.winId())
        try:
            win32.set_click_through(hwnd, True)
            if not win32.set_exclude_from_capture(hwnd, True):
                log.warning("The overlay could not be hidden from screen capture")
        except OSError:
            log.exception("Styling the overlay window failed")

    def paintEvent(self, event: object) -> None:  # noqa: N802 - Qt override
        if not self._text:
            return
        s = self.settings
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        area = QRectF(self.rect()).adjusted(_PADDING, _PADDING, -_PADDING, -_PADDING)
        font, lines = self._layout(area.width(), area.height())
        metrics = QFontMetricsF(font)
        line_height = metrics.lineSpacing()
        block_height = line_height * len(lines)

        if s.background_opacity > 0:
            widest = max(metrics.horizontalAdvance(line) for line in lines)
            box = QRectF(0, 0, widest + 2 * _PADDING, block_height + _PADDING)
            box.moveCenter(QPointF(self.width() / 2, area.bottom() - block_height / 2))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(0, 0, 0, round(255 * s.background_opacity)))
            painter.drawRoundedRect(box, 8, 8)

        path = QPainterPath()
        y = area.bottom() - block_height + metrics.ascent()
        for line in lines:  # bottom-aligned, centered: where subtitles usually sit
            x = area.left() + (area.width() - metrics.horizontalAdvance(line)) / 2
            path.addText(QPointF(x, y), font, line)
            y += line_height

        if s.shadow:
            painter.fillPath(path.translated(2, 2), QColor(0, 0, 0, 160))
        if s.outline_px > 0:
            pen = QPen(QColor(s.outline_color), s.outline_px * 2)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.strokePath(path, pen)
        painter.fillPath(path, QColor(s.text_color))
        painter.end()

    def _layout(self, width: float, height: float) -> tuple[QFont, list[str]]:
        """The largest font size (from the configured one down) whose wrapped text fits."""
        font = QFont(self.settings.font_family)
        font.setBold(True)
        size = self.settings.font_size_pt
        while True:
            font.setPointSizeF(size)
            metrics = QFontMetricsF(font)
            lines = wrap(self._text, metrics.horizontalAdvance, width)
            fits = (
                len(lines) <= self.settings.max_lines
                and metrics.lineSpacing() * len(lines) <= height
            )
            if fits:
                return font, lines
            if size <= MIN_FONT_PT:
                return font, lines[: self.settings.max_lines]  # too long even at the minimum
            size -= 2
