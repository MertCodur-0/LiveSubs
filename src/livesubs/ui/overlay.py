"""The translation overlay: frameless, always on top, click-through, hidden from capture.

Hidden from screen capture (``WDA_EXCLUDEFROMCAPTURE``), so OCR never reads our own text even
when the overlay covers the subtitle region. Several subtitle lines (people talking at once) are
stacked like the game's, each with its own original; when they do not fit, the oldest leave first.
Text is drawn as a ``QPainterPath``: outline first,
fill on top, so it stays readable on any background. Styles are presets of the look fields;
"vice" also draws a thin cyan halo around the translation's pink outline (the small original
keeps a plain black outline: neon edges on small text hurt readability).

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
from livesubs.core.profiles import OverlaySettings, OverlayStyle
from livesubs.i18n import t
from livesubs.platform import win32
from livesubs.ui.movable import MoveResize
from livesubs.ui.screens import qrect
from livesubs.ui.shown_lines import ShownLine

log = logging.getLogger(__name__)

MIN_FONT_PT = 12
ORIGINAL_SCALE = 0.55  # English original under the translation, relative to its font size
ORIGINAL_MAX_LINES = 2
_PADDING = 10
VICE_HALO = "#00E5FF"
VICE_HALO_PX = 1.5  # outside the outline

STYLE_PRESETS: dict[OverlayStyle, dict[str, object]] = {
    OverlayStyle.CLASSIC: {
        "text_color": "#FFFFFF",
        "outline_color": "#000000",
        "outline_px": 3.0,
        "shadow": True,
        "background": True,
        "background_opacity": 0.55,
    },
    # Readability first: white text, thin neon edges around it, a box behind.
    OverlayStyle.VICE: {
        "text_color": "#FFFFFF",
        "outline_color": "#FF2E88",
        "outline_px": 2.0,
        "shadow": True,
        "background": True,
        "background_opacity": 0.45,
    },
    OverlayStyle.BOX: {
        "text_color": "#FFFFFF",
        "outline_color": "#000000",
        "outline_px": 0.0,
        "shadow": False,
        "background": True,
        "background_opacity": 0.8,
    },
}


def apply_style(settings: OverlaySettings, style: OverlayStyle) -> None:
    """Set the look fields to the style's preset; each can still be changed afterwards."""
    settings.style = style
    for field, value in STYLE_PRESETS[style].items():
        setattr(settings, field, value)


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


def _block_height(
    metrics: QFontMetricsF, small: QFontMetricsF, lines: list[str], original: list[str]
) -> float:
    """One subtitle line's height: its translation, then its original 4 px below."""
    height = metrics.lineSpacing() * len(lines)
    if original:
        height += 4 + small.lineSpacing() * len(original)
    return height


class Overlay(QWidget):
    edited = Signal(object)  # Rect, logical: moved or resized by the user in edit mode

    def __init__(self, settings: OverlaySettings | None = None) -> None:
        super().__init__(None, self._flags(editable=False))
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setMouseTracking(True)
        self.settings = settings or OverlaySettings()
        self.align = Qt.AlignmentFlag.AlignBottom  # vertical alignment of the text block
        self._lines: list[ShownLine] = []
        self._editable = False
        self.suppressed = False  # hidden by the user (hotkey): keep the text, stay invisible
        self.disabled = False  # the profile shows translations on the phone only
        self.capture_visible = False  # recording mode: let OBS and screenshots see it
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
        return "\n".join(line.translation for line in self._lines)

    @property
    def lines(self) -> list[ShownLine]:
        return list(self._lines)

    @property
    def editable(self) -> bool:
        return self._editable

    def place(self, logical: Rect) -> None:
        if not self._mover.dragging:  # never fight the user's mouse
            self.setGeometry(qrect(logical))

    def show_text(self, translation: str, original: str = "") -> None:
        self.show_lines([ShownLine(translation, original)])

    def show_lines(self, lines: list[ShownLine]) -> None:
        """Several subtitle lines, top to bottom, each with its own original (people talking
        at once). When they do not all fit, the oldest (top) ones are left out, as games do."""
        self._lines = [line for line in lines if line.translation]
        if self._lines and not self.isVisible() and not (self.suppressed or self.disabled):
            self.show()
        self.update()

    def clear(self) -> None:
        self._lines = []
        self.update()

    def set_editable(self, editable: bool) -> None:
        """Calibration mode: take the mouse, show a frame and sample text."""
        if editable == self._editable:
            return
        self._editable = editable
        visible = self.isVisible()
        self.setWindowFlags(self._flags(editable=editable))  # hides the window
        if (visible or editable) and not self.disabled:
            self.show()
        self.update()

    def set_disabled(self, disabled: bool) -> None:
        self.disabled = disabled
        if disabled:
            self.hide()

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
        except OSError:
            log.exception("Styling the overlay window failed")
        self._apply_capture_visibility()

    def set_capture_visible(self, visible: bool) -> None:
        self.capture_visible = visible
        if self.isVisible():
            self._apply_capture_visibility()

    def _apply_capture_visibility(self) -> None:
        if QGuiApplication.platformName() != "windows":
            return
        try:
            if not win32.set_exclude_from_capture(int(self.winId()), not self.capture_visible):
                log.warning("The overlay's capture visibility could not be changed")
        except OSError:
            log.exception("Changing the overlay's capture visibility failed")

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
        items = self._lines
        if not items and self._editable:
            original = t("overlay.sample_original") if self.settings.show_original else ""
            items = [ShownLine(t("overlay.sample"), original)]
        if items:
            self._paint_lines(painter, items)
        painter.end()

    def _paint_lines(self, painter: QPainter, items: list[ShownLine]) -> None:
        s = self.settings
        area = QRectF(self.rect()).adjusted(_PADDING, _PADDING, -_PADDING, -_PADDING)
        small_font = self._original_font(s.font_size_pt)
        small = QFontMetricsF(small_font)
        font, blocks = self._layout(items, area.width(), area.height())
        metrics = QFontMetricsF(font)
        block_height = sum(_block_height(metrics, small, lines, orig) for lines, orig in blocks)
        if self.align == Qt.AlignmentFlag.AlignTop:
            top = area.top()
        elif self.align == Qt.AlignmentFlag.AlignVCenter:
            top = area.center().y() - block_height / 2
        else:
            top = area.bottom() - block_height

        if s.background and s.background_opacity > 0:
            widths = [metrics.horizontalAdvance(line) for lines, _ in blocks for line in lines]
            widths += [small.horizontalAdvance(line) for _, orig in blocks for line in orig]
            box = QRectF(0, 0, max(widths) + 2 * _PADDING, block_height + _PADDING)
            box.moveCenter(QPointF(self.width() / 2, top + block_height / 2))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(0, 0, 0, round(255 * s.background_opacity)))
            painter.drawRoundedRect(box, 8, 8)

        path, small_path, y = QPainterPath(), QPainterPath(), top
        for lines, original in blocks:
            for line in lines:
                point = QPointF(self._x(area, metrics, line), y + metrics.ascent())
                path.addText(point, font, line)
                y += metrics.lineSpacing()
            if original:
                y += 4  # the original starts 4 px below its translation
            for line in original:
                point = QPointF(self._x(area, small, line), y + small.ascent())
                small_path.addText(point, small_font, line)
                y += small.lineSpacing()

        vice = s.style == OverlayStyle.VICE
        for p, scale in ((path, 1.0), (small_path, ORIGINAL_SCALE)):
            if p.isEmpty():
                continue
            outline = s.outline_px * scale
            color = QColor(s.outline_color)
            if s.shadow:
                painter.fillPath(p.translated(2, 2), QColor(0, 0, 0, 160))
            if vice and p is path:
                halo = QPen(QColor(VICE_HALO), (outline + VICE_HALO_PX) * 2)
                halo.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
                painter.strokePath(p, halo)
            elif vice:
                color = QColor("#000000")
            if outline > 0:
                pen = QPen(color, outline * 2)
                pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
                painter.strokePath(p, pen)
        painter.fillPath(path, QColor(s.text_color))
        faded = QColor(s.text_color)
        faded.setAlpha(215)
        painter.fillPath(small_path, faded)

    @staticmethod
    def _x(area: QRectF, metrics: QFontMetricsF, line: str) -> float:
        return area.left() + (area.width() - metrics.horizontalAdvance(line)) / 2

    def _layout(
        self, items: list[ShownLine], width: float, height: float
    ) -> tuple[QFont, list[tuple[list[str], list[str]]]]:
        """Wrapped (translation, original) lines per item. Several items that do not fit at the
        configured size lose the oldest first; the last one left shrinks from the configured
        size down until it fits."""
        s = self.settings
        small = QFontMetricsF(self._original_font(s.font_size_pt))

        def wrapped(font: QFont, items: list[ShownLine]) -> list[tuple[list[str], list[str]]]:
            metrics = QFontMetricsF(font)
            blocks = []
            for item in items:
                original = item.original if s.show_original else ""
                blocks.append(
                    (
                        wrap(item.translation, metrics.horizontalAdvance, width),
                        wrap(original, small.horizontalAdvance, width)[:ORIGINAL_MAX_LINES],
                    )
                )
            return blocks

        def fits(font: QFont, blocks: list[tuple[list[str], list[str]]]) -> bool:
            metrics = QFontMetricsF(font)
            count = sum(len(lines) for lines, _ in blocks)
            total = sum(_block_height(metrics, small, lines, orig) for lines, orig in blocks)
            return count <= s.max_lines and total <= height

        size = float(s.font_size_pt)
        font = self._font(size)
        while len(items) > 1 and not fits(font, wrapped(font, items)):
            items = items[1:]  # the oldest scrolls out, as in the game
        while True:
            font = self._font(size)
            blocks = wrapped(font, items)
            if fits(font, blocks):
                return font, blocks
            if size <= MIN_FONT_PT:  # too long even at the minimum
                lines, original = blocks[-1]
                return font, [(lines[: s.max_lines], original)]
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
