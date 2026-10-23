"""One LiveSubs per user: a second start brings the running window forward and exits.

Two instances once ran side by side for hours (one hidden in the tray): both polled Ollama,
fought over the hotkeys and wrote the same settings, and quitting one left the other running.
A local socket (a named pipe on Windows) named after the settings folder finds the first one.
"""

from __future__ import annotations

import hashlib

from PySide6.QtCore import QObject, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket

from livesubs import APP_NAME
from livesubs.core import paths

CONNECT_TIMEOUT_MS = 500


def instance_name() -> str:
    """Per user and per settings folder, so a portable or test home (LIVESUBS_HOME) is apart."""
    folder = str(paths.config_dir()).casefold().encode("utf-8")
    return f"{APP_NAME}-{hashlib.sha1(folder).hexdigest()[:16]}"


class SingleInstance(QObject):
    activated = Signal()  # another start asked this instance to show its window

    def __init__(self, name: str | None = None) -> None:
        super().__init__()
        self._name = name or instance_name()
        self._server: QLocalServer | None = None

    def notify_running(self) -> bool:
        """True when an instance is already running; it has been asked to show its window."""
        socket = QLocalSocket()
        socket.connectToServer(self._name)
        if not socket.waitForConnected(CONNECT_TIMEOUT_MS):
            return False
        socket.write(b"show\n")
        socket.waitForBytesWritten(CONNECT_TIMEOUT_MS)
        socket.disconnectFromServer()
        return True

    def listen(self) -> bool:
        self._server = QLocalServer(self)
        self._server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        self._server.newConnection.connect(self._on_connection)
        return self._server.listen(self._name)

    def close(self) -> None:
        if self._server is not None:
            self._server.close()
            self._server = None

    def _on_connection(self) -> None:
        assert self._server is not None
        while (socket := self._server.nextPendingConnection()) is not None:
            socket.disconnected.connect(socket.deleteLater)
            self.activated.emit()
