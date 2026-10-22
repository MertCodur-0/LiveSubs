"""The main window's header: logo and tagline over the sunset banner (``assets/header.svg``)."""

from __future__ import annotations

from PySide6.QtCore import QRectF
from PySide6.QtGui import QPainter, QPaintEvent, QPixmap, QResizeEvent
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget

from livesubs import APP_NAME
from livesubs.i18n import t
from livesubs.ui import theme as themes

HEIGHT = 88


class Header(QWidget):
    def __init__(self, theme: themes.Theme) -> None:
        super().__init__()
        self.setFixedHeight(HEIGHT)
        self._art = QSvgRenderer(str(themes.asset("header.svg")), self)
        self._cache: QPixmap | None = None  # the banner at the current size; redrawn on resize
        self.logo = QLabel(APP_NAME, objectName="logo")
        self.tagline = QLabel(t("header.tagline"), objectName="tagline")
        text = QVBoxLayout()
        text.setSpacing(0)
        text.addStretch(1)
        text.addWidget(self.logo)
        text.addWidget(self.tagline)
        text.addStretch(1)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(20, 4, 20, 6)
        layout.addLayout(text)
        layout.addStretch(1)
        self.set_theme(theme)

    def set_theme(self, theme: themes.Theme) -> None:
        self._theme = theme
        self._cache = None
        themes.set_glow(self.logo, theme)
        self.update()

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        self._cache = None

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802 - Qt override
        if self._cache is None or self._cache.size() != self.size():
            self._cache = self._render()
        painter = QPainter(self)
        painter.drawPixmap(0, 0, self._cache)
        painter.end()

    def _render(self) -> QPixmap:
        ratio = self.devicePixelRatioF()
        pixmap = QPixmap(round(self.width() * ratio), round(self.height() * ratio))
        pixmap.setDevicePixelRatio(ratio)
        pixmap.fill(self._theme.color("window"))
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self._theme.header_art and self._art.isValid():
            # Right-aligned at full height; the art's left edge fades into the window color.
            view = self._art.viewBoxF()
            scale = self.height() / view.height()
            width = view.width() * scale
            self._art.render(painter, QRectF(self.width() - width, 0, width, self.height()))
        else:
            painter.setPen(self._theme.color("border"))
            painter.drawLine(0, self.height() - 1, self.width(), self.height() - 1)
        painter.end()
        return pixmap
