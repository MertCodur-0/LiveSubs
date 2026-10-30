"""Line icons (``assets/icons/*.svg``, drawn black) tinted to a theme color at run time.

The SVGs are rendered with QtSvg and recolored with ``SourceIn``, so one file serves every theme
and the icons always take the text color they sit next to.
"""

from __future__ import annotations

from functools import cache

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QImage, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

from livesubs.ui import theme

NAMES = ("live", "appearance", "profiles", "models", "settings", "info", "warning")


@cache
def _renderer(name: str) -> QSvgRenderer:
    return QSvgRenderer(str(theme.asset(f"icons/{name}.svg")))


def pixmap(name: str, color: QColor, size: int, ratio: float = 1.0) -> QPixmap:
    """The icon at ``size`` logical pixels, every visible pixel in ``color``."""
    px = max(1, round(size * ratio))
    image = QImage(px, px, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    _renderer(name).render(painter, QRectF(0, 0, px, px))
    painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
    painter.fillRect(image.rect(), color)
    painter.end()
    result = QPixmap.fromImage(image)
    result.setDevicePixelRatio(ratio)
    return result


def icon(name: str, color: QColor, size: int = 18, ratio: float = 2.0) -> QIcon:
    """A QIcon of the tinted icon (rendered at ``ratio`` so it stays sharp on high DPI)."""
    return QIcon(pixmap(name, color, size, ratio))
