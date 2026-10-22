"""Regenerate ``src/livesubs/assets/icon.ico`` (shortcut icon) from ``icon.svg``.

Run after editing the SVG: ``.venv/Scripts/python.exe scripts/make_icon.py``.
"""

from PySide6.QtGui import QGuiApplication

from livesubs.ui import theme
from livesubs.ui.icon import write_ico

if __name__ == "__main__":
    app = QGuiApplication([])
    target = theme.asset("icon.ico")
    write_ico(target)
    print(f"Wrote {target}")
