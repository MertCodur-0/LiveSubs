"""Application entry point.

Placeholder until the real main window lands (step 9); it only proves the Qt stack starts.
"""

import multiprocessing
import sys

from PySide6.QtWidgets import QApplication, QLabel, QMainWindow

from livesubs import APP_NAME, __version__


def main() -> int:
    # Required for the engine process when frozen with PyInstaller.
    multiprocessing.freeze_support()

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(__version__)

    window = QMainWindow()
    window.setWindowTitle(f"{APP_NAME} {__version__}")
    window.setCentralWidget(QLabel(APP_NAME))
    window.resize(640, 400)
    window.show()
    return app.exec()
