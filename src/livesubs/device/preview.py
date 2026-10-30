"""Full-screen preview of the capture card, shown by the device host.

Mainly for a TV light strip ("Ambilight") app that samples a PC monitor: the picture must fill
the screen edge to edge (black bars would turn the edge LEDs off), stay visible to screen
capture (no ``WDA_EXCLUDEFROMCAPTURE``), and the screen must not sleep while nobody touches the
PC. Frames arrive from the device thread; ``show_frame`` only stores the newest one and asks Qt
for a repaint, which Qt merges when frames come faster than the screen.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QCloseEvent, QImage, QKeyEvent, QMouseEvent, QPainter, QPaintEvent
from PySide6.QtWidgets import QWidget

from livesubs.engine.capture.base import Frame
from livesubs.i18n import t
from livesubs.platform import win32
from livesubs.ui import screens


class PreviewWindow(QWidget):
    frame_ready = Signal()
    closed_by_user = Signal()

    def __init__(self) -> None:
        super().__init__(None, Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint)
        self.setWindowTitle(t("preview.title"))
        self.setCursor(Qt.CursorShape.BlankCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self._frame: Frame | None = None
        self.monitor: str | None = None
        self.frame_ready.connect(self.update)

    def show_frame(self, frame: Frame | None) -> None:
        """Any thread: the newest frame (None = no picture, black)."""
        self._frame = frame
        self.frame_ready.emit()

    def show_on(self, monitor: str) -> bool:
        """Full screen on ``monitor``; False when it is not connected."""
        target = next((m for m in win32.list_monitors() if m.device_name == monitor), None)
        if target is None:
            return False
        logical = screens.to_logical(target.rect, screens.current_screens())
        if self.isVisible():
            self.showNormal()  # leave full screen before moving between monitors
        self.setGeometry(screens.qrect(logical))
        self.showFullScreen()
        self.monitor = monitor
        win32.keep_display_awake(True)
        return True

    def hide_preview(self) -> None:
        self.monitor = None
        self.hide()
        win32.keep_display_awake(False)

    def paintEvent(self, _event: QPaintEvent) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        frame = self._frame
        if frame is None:
            painter.fillRect(self.rect(), Qt.GlobalColor.black)
            return
        image = frame.image
        height, width = image.shape[:2]
        pixel_format = (
            QImage.Format.Format_BGR888 if image.shape[2] == 3 else QImage.Format.Format_RGB32
        )
        picture = QImage(image.data, width, height, image.strides[0], pixel_format)
        target = self.rect()
        ratio = self.devicePixelRatio()
        if (round(target.width() * ratio), round(target.height() * ratio)) != (width, height):
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        # Stretched to fill: the card and the monitor are both 16:9, and bars would darken the
        # light strip's edge LEDs.
        painter.drawImage(target, picture)

    def _close_by_user(self) -> None:
        self.hide_preview()
        self.closed_by_user.emit()

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 - Qt override
        if event.key() == Qt.Key.Key_Escape:
            self._close_by_user()
        else:
            super().keyPressEvent(event)

    def mouseDoubleClickEvent(self, _event: QMouseEvent) -> None:  # noqa: N802 - Qt override
        self._close_by_user()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt override
        # Alt+F4 hides it; the host process keeps running for the engine.
        event.ignore()
        self._close_by_user()
