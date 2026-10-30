"""Application entry point: the UI process. The engine runs in a child process."""

import logging
import sys

from PySide6.QtWidgets import QApplication

from livesubs import APP_NAME, __version__
from livesubs.core.logs import setup_file_logging


def main() -> int:
    setup_file_logging("ui.log")

    # Imported here: the engine child process imports this module but never needs the UI.
    from livesubs.engine.process import EngineProcess
    from livesubs.platform import win32
    from livesubs.ui import theme
    from livesubs.ui.main_window import MainWindow
    from livesubs.ui.single_instance import SingleInstance

    win32.set_app_user_model_id(f"{APP_NAME}.App")

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(__version__)
    instance = SingleInstance()
    win32.allow_any_foreground_window()
    if instance.notify_running():
        return 0
    if not instance.listen():
        logging.getLogger(__name__).warning("Single-instance socket not available")
    theme.prepare(app)

    window = MainWindow(EngineProcess())
    instance.activated.connect(window.show_from_tray)
    window.show()
    window.run_setup_check()
    return app.exec()
