"""Physical pixels (engine, Win32, capture) <-> Qt logical pixels, per monitor.

Qt (per-monitor DPI aware, PassThrough rounding) keeps every screen's top-left corner at its
physical desktop position and divides only its size by the scale: a 2560x1440 panel at 150 % at
(1920, -164) is the logical rect (1920, -164, 1707, 960). Screens are matched by that corner,
because ``QScreen.name()`` is a device name for some monitors and a model name for others.
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QRect
from PySide6.QtGui import QGuiApplication

from livesubs.core.geometry import Rect
from livesubs.platform import win32


@dataclass(frozen=True, slots=True)
class ScreenMap:
    physical: Rect
    scale: float

    @property
    def logical(self) -> Rect:
        p = self.physical
        return Rect(p.x, p.y, round(p.width / self.scale), round(p.height / self.scale))


def _containing(screens: list[ScreenMap], x: float, y: float, logical: bool) -> ScreenMap:
    def area(s: ScreenMap) -> Rect:
        return s.logical if logical else s.physical

    for screen in screens:
        r = area(screen)
        if r.x <= x < r.right and r.y <= y < r.bottom:
            return screen

    def distance(s: ScreenMap) -> float:
        r = area(s)
        cx, cy = r.x + r.width / 2, r.y + r.height / 2
        return (cx - x) ** 2 + (cy - y) ** 2

    return min(screens, key=distance)


def to_logical(rect: Rect, screens: list[ScreenMap]) -> Rect:
    """A physical rect in the logical coordinates of the screen holding its center."""
    s = _containing(screens, rect.x + rect.width / 2, rect.y + rect.height / 2, logical=False)
    o = s.physical
    return Rect(
        round(o.x + (rect.x - o.x) / s.scale),
        round(o.y + (rect.y - o.y) / s.scale),
        round(rect.width / s.scale),
        round(rect.height / s.scale),
    )


def to_physical(rect: Rect, screens: list[ScreenMap]) -> Rect:
    """Inverse of ``to_logical``."""
    s = _containing(screens, rect.x + rect.width / 2, rect.y + rect.height / 2, logical=True)
    o = s.physical
    return Rect(
        round(o.x + (rect.x - o.x) * s.scale),
        round(o.y + (rect.y - o.y) * s.scale),
        round(rect.width * s.scale),
        round(rect.height * s.scale),
    )


def current_screens() -> list[ScreenMap]:
    """The monitors as Win32 reports them, with Qt's scale for each (matched by top-left)."""
    qt_screens = {
        (s.geometry().x(), s.geometry().y()): s.devicePixelRatio()
        for s in QGuiApplication.screens()
    }
    return [
        ScreenMap(m.rect, qt_screens.get((m.rect.x, m.rect.y), m.scale))
        for m in win32.list_monitors()
    ]


def qrect(rect: Rect) -> QRect:
    return QRect(rect.x, rect.y, rect.width, rect.height)


def from_qrect(rect: QRect) -> Rect:
    return Rect(rect.x(), rect.y(), rect.width(), rect.height())
