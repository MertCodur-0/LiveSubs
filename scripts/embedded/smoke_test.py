"""Checks a packaged build: run ``dist\\LiveSubs\\LiveSubs.exe smoke_test.py <report>``.

LiveSubs.exe is a windowless pythonw.exe, so the result goes to the report file: "ok" plus the
loaded Qt plugins, or the traceback. Exercises the native parts that a trimmed or incomplete
package would break: Qt widgets, every theme with the bundled fonts and SVG art, the SVG/image
plugins, OpenCV, WinRT OCR, windows-capture, the capture card parts (Media Foundation listing,
shared frames, the preview) and the UI and engine modules.
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
    from PySide6.QtGui import QFontDatabase

    from livesubs.ui import theme
    from livesubs.ui.icon import app_icon

    theme.prepare(app)
    for name in theme.CHOICES:
        theme.apply_theme(name)  # template, token files, bundled fonts, system scheme
    missing = {"Inter", "Audiowide", "Monoton"} - set(QFontDatabase.families())
    if missing:
        raise RuntimeError(f"Bundled fonts missing: {sorted(missing)}")
    if app_icon().isNull() or not theme.asset("icon.ico").is_file():
        raise RuntimeError("App icon missing")
    QPixmap().loadFromData(QByteArray(b'<svg xmlns="http://www.w3.org/2000/svg"/>'), "SVG")
    image = QImage(400, 60, QImage.Format.Format_ARGB32)
    path = QPainterPath()
    path.addText(10, 40, app.font(), "Merhaba, dünya")
    painter = QPainter(image)
    painter.drawPath(path)
    painter.end()

    import os

    import windows_capture  # noqa: F401

    from livesubs.device.host import DeviceHost  # noqa: F401
    from livesubs.device.preview import PreviewWindow
    from livesubs.engine.capture.shared_frames import SharedFrameReader, SharedFrameWriter
    from livesubs.engine.capture.video_device import VideoDeviceCapture  # noqa: F401
    from livesubs.engine.ocr.winrt_ocr import WinRtOcr
    from livesubs.engine.process import EngineProcess  # noqa: F401
    from livesubs.platform.video_devices import list_video_devices
    from livesubs.ui.main_window import MainWindow  # noqa: F401
    from livesubs.ui.qr import qr_pixmap
    from livesubs.web.server import FONT, render_page

    # Capture card: OpenCV's MSMF must open cards fast (set before cv2 is imported), Media
    # Foundation must list devices, frames must cross shared memory, the preview must paint.
    assert os.environ.get("OPENCV_VIDEOIO_MSMF_ENABLE_HW_TRANSFORMS") == "0"
    list_video_devices()
    writer = SharedFrameWriter(32, 18)
    reader = SharedFrameReader(writer.name)
    writer.write(np.full((18, 32, 3), 7, np.uint8), 1.0)
    assert int(reader.read()[1].image[0, 0, 0]) == 7
    reader.close()
    writer.close()
    assert not PreviewWindow().grab().isNull()

    WinRtOcr().recognize(np.full((40, 200), 255, np.uint8))
    assert b"EventSource" in render_page() and FONT.is_file()  # package data of the phone page
    assert not qr_pixmap("http://192.168.1.2:8765/").isNull()
    return [f"plugin path: {p}" for p in QCoreApplication.libraryPaths()]


if __name__ == "__main__":
    with Path(sys.argv[1]).open("w", encoding="utf-8") as report:
        try:
            lines = check()
        except BaseException:
            report.write(traceback.format_exc())
        else:
            report.write("\n".join(["ok", *lines]) + "\n")
