"""The app icon, drawn in code until the SVG artwork lands (step 16)."""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QIcon, QLinearGradient, QPainter, QPixmap

SIZES = (16, 24, 32, 48, 64, 256)


def _pixmap(size: int) -> QPixmap:
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    gradient = QLinearGradient(0, 0, size, size)
    gradient.setColorAt(0.0, QColor("#FF2E88"))  # neon pink -> sunset orange
    gradient.setColorAt(1.0, QColor("#FF9A3C"))
    painter.setBrush(gradient)
    painter.setPen(Qt.PenStyle.NoPen)
    radius = size * 0.22
    painter.drawRoundedRect(QRectF(0, 0, size, size), radius, radius)
    font = QFont("Segoe UI")
    font.setBold(True)
    font.setPixelSize(round(size * 0.46))
    painter.setFont(font)
    painter.setPen(QColor("#1A1030"))
    painter.drawText(QRectF(0, 0, size, size), Qt.AlignmentFlag.AlignCenter, "TR")
    painter.end()
    return pixmap


def app_icon() -> QIcon:
    icon = QIcon()
    for size in SIZES:
        icon.addPixmap(_pixmap(size))
    return icon
