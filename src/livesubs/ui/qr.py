"""QR code of the phone page's address, painted with Qt (``qrcode`` gives the modules only)."""

from __future__ import annotations

import qrcode
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPixmap

QUIET_ZONE = 4  # modules of white around the code; scanners need it


def qr_pixmap(text: str, size_px: int = 180) -> QPixmap:
    code = qrcode.QRCode(border=QUIET_ZONE, error_correction=qrcode.constants.ERROR_CORRECT_M)
    code.add_data(text)
    code.make(fit=True)
    matrix = code.get_matrix()  # includes the quiet zone
    module = size_px / len(matrix)
    pixmap = QPixmap(size_px, size_px)
    pixmap.fill(QColor("white"))
    painter = QPainter(pixmap)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor("black"))
    for y, row in enumerate(matrix):
        for x, dark in enumerate(row):
            if dark:
                # A hair wider than a module so no seams show between neighbours.
                painter.drawRect(QRectF(x * module, y * module, module + 0.5, module + 0.5))
    painter.end()
    return pixmap
