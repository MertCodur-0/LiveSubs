"""The translation overlay: frameless, always on top, click-through, hidden from capture.

Hidden from screen capture (``WDA_EXCLUDEFROMCAPTURE``), so OCR never reads our own text even
when the overlay covers the subtitle region. Text is drawn as a ``QPainterPath``: outline first,
fill on top, so it stays readable on any background.

In edit mode (calibration) it takes the mouse: drag to move, drag an edge or corner to resize;
``edited`` reports the new geometry when the mouse is released.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from PySide6.QtCore import QPoint, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetricsF,
    QGuiApplication,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPen,
)
from PySide6.QtWidgets import QWidget

from livesubs.core.geometry import Rect
from livesubs.core.profiles import OverlaySettings
from livesubs.i18n import t
from livesubs.platform import win32
from livesubs.ui.movable import MoveResize
from livesubs.ui.screens import qrect

log = logging.getLogger(__name__)

MIN_FONT_PT = 12
ORIGINAL_SCALE = 0.55  # English original under the translation, relative to its font size
ORIGINAL_MAX_LINES = 2
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
    edited = Signal(object)  # Rect, logical: moved or resized by the user in edit mode

    def __init__(self, settings: OverlaySettings | None = None) -> None:
        super().__init__(None, self._flags(editable=False))
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setMouseTracking(True)
        self.settings = settings or OverlaySettings()
        self.align = Qt.AlignmentFlag.AlignBottom  # vertical alignment of the text block
        self._text = ""
        self._original = ""
        self._editable = False
        self.suppressed = False  # hidden by the user (hotkey): keep the text, stay invisible
        self._mover = MoveResize(self, self.edited.emit, QPoint(160, 48))

    @staticmethod
    def _flags(*, editable: bool) -> Qt.WindowType:
        flags = (
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        if not editable:
            flags |= (
                Qt.WindowType.WindowDoesNotAcceptFocus | Qt.WindowType.WindowTransparentForInput
            )
        return flags

    # --- content ---------------------------------------------------------------------------------

    @property
    def text(self) -> str:
        return self._text

    @property
    def editable(self) -> bool:
        return self._editable

    def place(self, logical: Rect) -> None:
        if not self._mover.dragging:  # never fight the user's mouse
            self.setGeometry(qrect(logical))

    def show_text(self, translation: str, original: str = "") -> None:
        self._text, self._original = translation, original
        if not self.isVisible() and not self.suppressed:
            self.show()
        self.update()

    def clear(self) -> None:
        self._text = self._original = ""
        self.update()

    def set_editable(self, editable: bool) -> None:
        """Calibration mode: take the mouse, show a frame and sample text."""
        if editable == self._editable:
            return
        self._editable = editable
        visible = self.isVisible()
        self.setWindowFlags(self._flags(editable=editable))  # hides the window
        if visible or editable:
            self.show()
        self.update()

    def preferred_height(self) -> float:
        """Logical height for ``max_lines`` lines at the configured size (plus the original)."""
        s = self.settings
        main = QFontMetricsF(self._font(s.font_size_pt)).lineSpacing() * s.max_lines
        original = 0.0
        if s.show_original:
            small = QFontMetricsF(self._original_font(s.font_size_pt)).lineSpacing()
            original = small * ORIGINAL_MAX_LINES + 4
        return main + original + 2 * _PADDING + 4

    # --- window ----------------------------------------------------------------------------------

    def showEvent(self, event: object) -> None:  # noqa: N802 - Qt override
        super().showEvent(event)
        if QGuiApplication.platformName() != "windows":
            return  # offscreen (tests, CI): there is no real window to style
        hwnd = int(self.winId())
        try:
            win32.set_click_through(hwnd, not self._editable)
            if not win32.set_exclude_from_capture(hwnd, True):
                log.warning("The overlay could not be hidden from screen capture")
        except OSError:
            log.exception("Styling the overlay window failed")

    # --- painting --------------------------------------------------------------------------------

    def _font(self, size: float) -> QFont:
        font = QFont(self.settings.font_family)
        font.setBold(True)
        font.setPointSizeF(size)
        return font

    def _original_font(self, size: float) -> QFont:
        font = QFont(self.settings.font_family)
        font.setPointSizeF(max(9.0, size * ORIGINAL_SCALE))
        return font

    def paintEvent(self, event: object) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self._editable:
            painter.fillRect(self.rect(), QColor(0, 229, 255, 40))
            pen = QPen(QColor("#00E5FF"), 2, Qt.PenStyle.DashLine)
            painter.setPen(pen)
            painter.drawRect(self.rect().adjusted(1, 1, -2, -2))
        text = self._text or (t("overlay.sample") if self._editable else "")
        if text:
            original = self._original if self.settings.show_original else ""
            if self._editable and not self._text and self.settings.show_original:
                original = t("overlay.sample_original")
            self._paint_text(painter, text, original)
        painter.end()

    def _paint_text(self, painter: QPainter, text: str, original: str) -> None:
        s = self.settings
        area = QRectF(self.rect()).adjusted(_PADDING, _PADDING, -_PADDING, -_PADDING)

        small_font = self._original_font(s.font_size_pt)
        small = QFontMetricsF(small_font)
        small_lines = wrap(original, small.horizontalAdvance, area.width())[:ORIGINAL_MAX_LINES]
        small_height = small.lineSpacing() * len(small_lines) + (4 if small_lines else 0)

        font, lines = self._layout(text, area.width(), area.height() - small_height)
        metrics = QFontMetricsF(font)
        block_height = metrics.lineSpacing() * len(lines) + small_height
        if self.align == Qt.AlignmentFlag.AlignTop:
            top = area.top()
        elif self.align == Qt.AlignmentFlag.AlignVCenter:
            top = area.center().y() - block_height / 2
        else:
            top = area.bottom() - block_height

        if s.background and s.background_opacity > 0:
            widths = [metrics.horizontalAdvance(line) for line in lines]
            widths += [small.horizontalAdvance(line) for line in small_lines]
            box = QRectF(0, 0, max(widths) + 2 * _PADDING, block_height + _PADDING)
            box.moveCenter(QPointF(self.width() / 2, top + block_height / 2))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(0, 0, 0, round(255 * s.background_opacity)))
            painter.drawRoundedRect(box, 8, 8)

        path, y = QPainterPath(), top + metrics.ascent()
        for line in lines:
            path.addText(QPointF(self._x(area, metrics, line), y), font, line)
            y += metrics.lineSpacing()
        # The original starts 4 px below the last translated line.
        small_path, y = QPainterPath(), y - metrics.ascent() + 4 + small.ascent()
        for line in small_lines:
            small_path.addText(QPointF(self._x(area, small, line), y), small_font, line)
            y += small.lineSpacing()

        for p, outline in ((path, s.outline_px), (small_path, s.outline_px * ORIGINAL_SCALE)):
            if p.isEmpty():
                continue
            if s.shadow:
                painter.fillPath(p.translated(2, 2), QColor(0, 0, 0, 160))
            if outline > 0:
                pen = QPen(QColor(s.outline_color), outline * 2)
                pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
                painter.strokePath(p, pen)
        painter.fillPath(path, QColor(s.text_color))
        faded = QColor(s.text_color)
        faded.setAlpha(215)
        painter.fillPath(small_path, faded)

    @staticmethod
    def _x(area: QRectF, metrics: QFontMetricsF, line: str) -> float:
        return area.left() + (area.width() - metrics.horizontalAdvance(line)) / 2

    def _layout(self, text: str, width: float, height: float) -> tuple[QFont, list[str]]:
        """The largest font size (from the configured one down) whose wrapped text fits."""
        size = float(self.settings.font_size_pt)
        while True:
            font = self._font(size)
            metrics = QFontMetricsF(font)
            lines = wrap(text, metrics.horizontalAdvance, width)
            fits = (
                len(lines) <= self.settings.max_lines
                and metrics.lineSpacing() * len(lines) <= height
            )
            if fits:
                return font, lines
            if size <= MIN_FONT_PT:
                return font, lines[: self.settings.max_lines]  # too long even at the minimum
            size -= 2

    # --- edit mode: move and resize --------------------------------------------------------------

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt override
        if self._editable:
            self._mover.handle_press(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt override
        if self._editable:
            self._mover.handle_move(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt override
        if self._editable:
            self._mover.handle_release(event)
