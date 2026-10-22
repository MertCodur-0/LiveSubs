"""Checks a packaged build: run ``dist\\LiveSubs\\LiveSubs.exe smoke_test.py <report>``.

LiveSubs.exe is a windowless pythonw.exe, so the result goes to the report file: "ok" plus the
loaded Qt plugins, or the traceback. Exercises the native parts that a trimmed or incomplete
package would break: Qt widgets, fonts and the SVG/image plugins, OpenCV, WinRT OCR,
windows-capture and the UI and engine modules.
"""

import sys
import traceback
from pathlib import Path


def check() -> list[str]:
    import numpy as np
    from PySide6.QtCore import QByteArray, QCoreApplication
    from PySide6.QtGui import QImage, QPainter, QPainterPath, QPixmap
    from PySide6.QtWidgets import QApplication

    app = QApplication([])
    QPixmap().loadFromData(QByteArray(b'<svg xmlns="http://www.w3.org/2000/svg"/>'), "SVG")
    image = QImage(400, 60, QImage.Format.Format_ARGB32)
    path = QPainterPath()
    path.addText(10, 40, app.font(), "Merhaba, dünya")
    painter = QPainter(image)
    painter.drawPath(path)
    painter.end()

    import windows_capture  # noqa: F401

    from livesubs.engine.ocr.winrt_ocr import WinRtOcr
    from livesubs.engine.process import EngineProcess  # noqa: F401
    from livesubs.ui.main_window import MainWindow  # noqa: F401

    WinRtOcr().recognize(np.full((40, 200), 255, np.uint8))
    return [f"plugin path: {p}" for p in QCoreApplication.libraryPaths()]


if __name__ == "__main__":
    with Path(sys.argv[1]).open("w", encoding="utf-8") as report:
        try:
            lines = check()
        except BaseException:
            report.write(traceback.format_exc())
        else:
            report.write("\n".join(["ok", *lines]) + "\n")
