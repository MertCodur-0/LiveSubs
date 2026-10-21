"""Application entry point: the UI process. The engine runs in a child process."""

import logging
import logging.handlers
import multiprocessing
import sys

from PySide6.QtWidgets import QApplication

from livesubs import APP_NAME, __version__
from livesubs.core import paths


def _setup_logging() -> None:
    log_dir = paths.log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        log_dir / "ui.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.getLogger().addHandler(handler)
    logging.getLogger().setLevel(logging.INFO)


def main() -> int:
    # Required for the engine process when frozen with PyInstaller.
    multiprocessing.freeze_support()
    _setup_logging()

    # Imported after freeze_support: the engine child process never needs the UI modules.
    from livesubs.engine.process import EngineProcess
    from livesubs.ui.main_window import MainWindow

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(__version__)

    window = MainWindow(EngineProcess())
    window.show()
    return app.exec()
