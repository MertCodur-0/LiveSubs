"""System tray icon: quick Start/Stop and show/hide while the main window is out of the way."""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from livesubs import APP_NAME
from livesubs.i18n import t


class Tray(QObject):
    show_window = Signal()
    toggle_running = Signal()
    toggle_overlay = Signal()
    toggle_preview = Signal()
    quit = Signal()

    def __init__(self, icon: QIcon) -> None:
        super().__init__()
        self.menu = QMenu()
        self.show_action = QAction(t("tray.show"), self.menu, triggered=self.show_window)
        self.run_action = QAction(t("start"), self.menu, triggered=self.toggle_running)
        self.overlay_action = QAction(t("tray.toggle_overlay"), self.menu)
        self.overlay_action.triggered.connect(self.toggle_overlay)
        self.preview_action = QAction(t("tray.toggle_preview"), self.menu)
        self.preview_action.triggered.connect(self.toggle_preview)
        self.quit_action = QAction(t("tray.quit"), self.menu, triggered=self.quit)
        for action in (
            self.show_action,
            self.run_action,
            self.overlay_action,
            self.preview_action,
        ):
            self.menu.addAction(action)
        self.menu.addSeparator()
        self.menu.addAction(self.quit_action)
        self.icon = QSystemTrayIcon(icon)
        self.icon.setToolTip(APP_NAME)
        self.icon.setContextMenu(self.menu)
        self.icon.activated.connect(self._on_activated)
        self._told_about_tray = False

    def show(self) -> None:
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.icon.show()

    def hide(self) -> None:
        self.icon.hide()

    def set_running(self, running: bool) -> None:
        self.run_action.setText(t("stop") if running else t("start"))
        self.icon.setToolTip(f"{APP_NAME} — {t('tray.running' if running else 'tray.idle')}")

    def notify(self, title: str, message: str) -> None:
        if self.icon.isVisible():
            self.icon.showMessage(title, message, QSystemTrayIcon.MessageIcon.Information, 5000)

    def notify_minimized(self) -> None:
        """Say once per session where the window went."""
        if not self._told_about_tray:
            self._told_about_tray = True
            self.notify(APP_NAME, t("tray.minimized"))

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self.show_window.emit()
