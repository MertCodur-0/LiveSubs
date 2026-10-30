"""Themes: ``base.qss`` filled from a token file (``dark.json``, ``light.json``) over Fusion.

``prepare`` runs once before any widget; ``apply_theme`` restyles at any time. The user picks
"system", "dark" or "light"; "system" follows Windows' app mode (``resolve``) and the main window
re-applies it when Windows switches (``QStyleHints.colorSchemeChanged``). Fusion gets a palette
built from the same tokens, so every corner the style sheet does not reach (popups, check boxes,
dialogs) matches the theme too. Inter is bundled (OFL, see ``assets/fonts``) and registered once
per process.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QFontDatabase, QGuiApplication, QPalette
from PySide6.QtWidgets import QApplication, QWidget

log = logging.getLogger(__name__)

THEME_DIR = Path(__file__).resolve().parent
ASSETS = THEME_DIR.parents[1] / "assets"
THEMES = ("dark", "light")
SYSTEM = "system"
CHOICES = (SYSTEM, *THEMES)  # what the user picks in Settings
DEFAULT_CHOICE = SYSTEM
FALLBACK_THEME = "dark"  # when Windows' choice cannot be read
BODY_FONT = "Inter"
BODY_POINT_SIZE = 10

_TOKEN = re.compile(r"\{\{(\w+)\}\}")
_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_fonts_loaded = False


@dataclass(frozen=True)
class Theme:
    name: str
    tokens: dict[str, str]

    def color(self, token: str) -> QColor:
        return QColor(self.tokens[token])


def asset(name: str) -> Path:
    return ASSETS / name


def system_theme() -> str:
    """Windows' app mode: Qt's color scheme, else the registry (Qt reports Unknown offscreen)."""
    app = QGuiApplication.instance()
    if isinstance(app, QGuiApplication):
        scheme = app.styleHints().colorScheme()
        if scheme == Qt.ColorScheme.Dark:
            return "dark"
        if scheme == Qt.ColorScheme.Light:
            return "light"
    if sys.platform == "win32":
        import winreg

        key = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as handle:
                light, _ = winreg.QueryValueEx(handle, "AppsUseLightTheme")
        except OSError:
            return FALLBACK_THEME
        return "light" if light else "dark"
    return FALLBACK_THEME


def resolve(choice: str) -> str:
    """The theme to show for a Settings choice; anything but "dark"/"light" follows Windows
    (also an old config's removed "vice")."""
    return choice if choice in THEMES else system_theme()


def load_theme(name: str) -> Theme:
    """The named theme; an unknown name gives the fallback."""
    if name not in THEMES:
        log.warning("Unknown theme %r, using %r", name, FALLBACK_THEME)
        name = FALLBACK_THEME
    data = json.loads((THEME_DIR / f"{name}.json").read_text(encoding="utf-8"))
    return Theme(name, data["tokens"])


def render_qss(theme: Theme) -> str:
    template = _COMMENT.sub("", (THEME_DIR / "base.qss").read_text(encoding="utf-8"))

    tokens = {**theme.tokens, "assets": ASSETS.as_posix()}  # for url(...) in the template

    def token(match: re.Match[str]) -> str:
        try:
            return tokens[match.group(1)]
        except KeyError:
            raise KeyError(f"Theme {theme.name!r} has no token {match.group(1)!r}") from None

    return _TOKEN.sub(token, template)


def load_fonts() -> None:
    """Register the bundled fonts with Qt (needs a QGuiApplication); later calls do nothing."""
    global _fonts_loaded
    if _fonts_loaded:
        return
    for path in sorted((ASSETS / "fonts").glob("*.ttf")):
        if QFontDatabase.addApplicationFont(str(path)) < 0:
            log.warning("Font could not be loaded: %s", path.name)
    _fonts_loaded = True


def palette(theme: Theme) -> QPalette:
    role, group = QPalette.ColorRole, QPalette.ColorGroup
    pal = QPalette()
    for r, token in (
        (role.Window, "window"),
        (role.WindowText, "text"),
        (role.Base, "field"),
        (role.AlternateBase, "surface"),
        (role.Text, "text"),
        (role.Button, "control"),
        (role.ButtonText, "text"),
        (role.BrightText, "accent_text"),
        (role.Highlight, "selection"),
        (role.HighlightedText, "selection_text"),
        (role.ToolTipBase, "surface"),
        (role.ToolTipText, "text"),
        (role.PlaceholderText, "text_muted"),
        (role.Link, "accent"),
        (role.Mid, "separator"),
        (role.Dark, "field_border"),
        (role.Light, "surface"),
        (role.Accent, "accent"),
    ):
        pal.setColor(r, theme.color(token))
    for r in (role.WindowText, role.Text, role.ButtonText):
        pal.setColor(group.Disabled, r, theme.color("text_muted"))
    return pal


def prepare(app: QApplication) -> None:
    """Fusion and the bundled fonts; call before the first widget exists, so every widget is
    polished by Fusion from the start (themes change only the palette and style sheet)."""
    app.setStyle("Fusion")
    load_fonts()


def apply_theme(choice: str) -> Theme:
    """Restyle the whole application for a Settings choice ("system", "dark", "light"); returns
    the theme for widgets that paint themselves."""
    app = QApplication.instance()
    if isinstance(app, QApplication):
        # A forced scheme also colors the Windows title bar; "system" hands it back to Windows,
        # so the color scheme reported afterwards is Windows' own again. Our own change is no
        # news: colorSchemeChanged fires synchronously and would call us again.
        hints = app.styleHints()
        wanted = {"dark": Qt.ColorScheme.Dark, "light": Qt.ColorScheme.Light}.get(choice)
        blocked = hints.blockSignals(True)
        try:
            if wanted is None:
                hints.unsetColorScheme()
            elif hints.colorScheme() != wanted:
                hints.setColorScheme(wanted)
        finally:
            hints.blockSignals(blocked)
    theme = load_theme(resolve(choice))
    if not isinstance(app, QApplication):
        return theme
    load_fonts()
    app.setPalette(palette(theme))
    font = QFont(BODY_FONT)
    font.setPointSize(BODY_POINT_SIZE)
    app.setFont(font)
    app.setStyleSheet(render_qss(theme))
    return theme


def repolish(widget: QWidget) -> None:
    """Re-read the style sheet after a dynamic property used in a selector changed."""
    widget.style().unpolish(widget)
    widget.style().polish(widget)
    widget.update()
