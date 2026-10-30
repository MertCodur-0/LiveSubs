"""The sidebar's header: the app name and tagline, plain text in Inter (styled by the theme)."""

from __future__ import annotations

from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from livesubs import APP_NAME
from livesubs.i18n import t


class Header(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.logo = QLabel(APP_NAME, objectName="logo")
        self.tagline = QLabel(t("header.tagline"), objectName="tagline")
        self.tagline.setWordWrap(True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 0, 6, 0)
        layout.setSpacing(2)
        layout.addWidget(self.logo)
        layout.addWidget(self.tagline)
