"""The app icon, rendered from ``assets/icon.svg``; ``write_ico`` makes the shortcut icon.

Rendered by hand rather than through ``QIcon(path)`` so it does not depend on Qt's SVG icon
engine plugin. The packaged LiveSubs.exe is a signed pythonw.exe whose own icon cannot change;
windows get this icon from Qt, shortcuts from ``icon.ico``.
"""

from __future__ import annotations

import struct
from pathlib import Path

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QRectF, Qt
from PySide6.QtGui import QIcon, QImage, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

from livesubs.ui import theme

SIZES = (16, 24, 32, 48, 64, 256)


def render(size: int) -> QImage:
    image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    QSvgRenderer(str(theme.asset("icon.svg"))).render(painter, QRectF(0, 0, size, size))
    painter.end()
    return image


def app_icon() -> QIcon:
    icon = QIcon()
    for size in SIZES:
        icon.addPixmap(QPixmap.fromImage(render(size)))
    return icon


def write_ico(path: Path) -> None:
    """An .ico holding a PNG per size (supported since Windows Vista)."""
    images = []
    for size in SIZES:
        data = QByteArray()
        buffer = QBuffer(data)
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        render(size).save(buffer, "PNG")
        images.append((size, bytes(data.data())))
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = len(header) + 16 * len(images)
    entries, blobs = b"", b""
    for size, png in images:
        side = 0 if size >= 256 else size  # 0 means 256 in an ICONDIRENTRY
        entries += struct.pack("<BBBBHHII", side, side, 0, 0, 1, 32, len(png), offset)
        blobs += png
        offset += len(png)
    path.write_bytes(header + entries + blobs)
