"""The sidebar's header: the app icon and name; Vice paints its sunset banner behind them
(``assets/header.svg``) and makes the name glow."""

from __future__ import annotations

from PySide6.QtCore import QRectF
from PySide6.QtGui import QLinearGradient, QPainter, QPaintEvent, QPixmap, QResizeEvent
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QHBoxLayout, QLabel, QWidget

from livesubs import APP_NAME
from livesubs.i18n import t
from livesubs.ui import theme as themes
from livesubs.ui.icon import render

HEIGHT = 48
ICON_PX = 24
ART_OPACITY = 0.8


class Header(QWidget):
    def __init__(self, theme: themes.Theme) -> None:
        super().__init__()
        self.setFixedHeight(HEIGHT)
        self.setToolTip(t("header.tagline"))
        self._art = QSvgRenderer(str(themes.asset("header.svg")), self)
        self._cache: QPixmap | None = None  # the banner at the current size; redrawn on resize
        self.icon = QLabel()
        self.logo = QLabel(APP_NAME, objectName="appName")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 0, 8, 0)
        layout.setSpacing(8)
        layout.addWidget(self.icon)
        layout.addWidget(self.logo)
        layout.addStretch(1)
        self.set_theme(theme)

    def set_theme(self, theme: themes.Theme) -> None:
        self._theme = theme
        self._cache = None
        pixmap = QPixmap.fromImage(render(round(ICON_PX * self.devicePixelRatioF())))
        pixmap.setDevicePixelRatio(self.devicePixelRatioF())
        self.icon.setPixmap(pixmap)
        themes.set_glow(self.logo, theme)
        self.update()

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        self._cache = None

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802 - Qt override
        if not (self._theme.header_art and self._art.isValid()):
            return  # the sidebar's own background shows through
        if self._cache is None or self._cache.size() != self.size():
            self._cache = self._render()
        painter = QPainter(self)
        painter.drawPixmap(0, 0, self._cache)
        painter.end()

    def _render(self) -> QPixmap:
        ratio = self.devicePixelRatioF()
        pixmap = QPixmap(round(self.width() * ratio), round(self.height() * ratio))
        pixmap.setDevicePixelRatio(ratio)
        pixmap.fill(self._theme.color("sidebar"))
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        # Right-aligned at full height and dimmed; a fade to the sidebar color keeps the name
        # readable over its left part.
        view = self._art.viewBoxF()
        scale = self.height() / view.height()
        width = view.width() * scale
        painter.setOpacity(ART_OPACITY)
        self._art.render(painter, QRectF(self.width() - width, 0, width, self.height()))
        painter.setOpacity(1.0)
        fade = QLinearGradient(0, 0, self.width(), 0)
        solid, clear = self._theme.color("sidebar"), self._theme.color("sidebar")
        clear.setAlpha(0)
        fade.setColorAt(0.0, solid)
        fade.setColorAt(0.55, solid)
        fade.setColorAt(1.0, clear)
        painter.fillRect(self.rect(), fade)
        painter.end()
        return pixmap
