"""Themes: ``base.qss`` filled from a token file (``vice.json``, ``dark.json``) over Fusion.

``prepare`` runs once before any widget; ``apply_theme`` restyles at any time. Fusion gets a
palette built from the same tokens, so every corner the style sheet does not reach (popups,
check boxes, dialogs) matches the theme too. Fonts are bundled (OFL, see ``assets/fonts``) and
registered once per process.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtGui import QColor, QFont, QFontDatabase, QPalette
from PySide6.QtWidgets import QApplication, QGraphicsDropShadowEffect, QWidget

log = logging.getLogger(__name__)

THEME_DIR = Path(__file__).resolve().parent
ASSETS = THEME_DIR.parents[1] / "assets"
THEMES = ("vice", "dark")
DEFAULT_THEME = "vice"
BODY_FONT = "Inter"
BODY_POINT_SIZE = 10
GLOW_BLUR = 28

_TOKEN = re.compile(r"\{\{(\w+)\}\}")
_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_fonts_loaded = False


@dataclass(frozen=True)
class Theme:
    name: str
    tokens: dict[str, str]
    header_art: bool  # the sunset banner behind the logo

    def color(self, token: str) -> QColor:
        return QColor(self.tokens[token])

    @property
    def glow(self) -> QColor | None:
        value = self.tokens.get("glow", "")
        return QColor(value) if value else None


def asset(name: str) -> Path:
    return ASSETS / name


def load_theme(name: str) -> Theme:
    """The named theme; an unknown name (e.g. a hand-edited config) gives the default."""
    if name not in THEMES:
        log.warning("Unknown theme %r, using %r", name, DEFAULT_THEME)
        name = DEFAULT_THEME
    data = json.loads((THEME_DIR / f"{name}.json").read_text(encoding="utf-8"))
    return Theme(name, data["tokens"], bool(data.get("header_art", False)))


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
        (role.Base, "surface"),
        (role.AlternateBase, "surface_alt"),
        (role.Text, "text"),
        (role.Button, "surface_alt"),
        (role.ButtonText, "text"),
        (role.BrightText, "accent_text"),
        (role.Highlight, "accent"),
        (role.HighlightedText, "accent_text"),
        (role.ToolTipBase, "surface_alt"),
        (role.ToolTipText, "text"),
        (role.PlaceholderText, "text_muted"),
        (role.Link, "accent_2"),
        (role.Mid, "border"),
        (role.Dark, "window"),
        (role.Light, "surface_alt"),
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


def apply_theme(name: str) -> Theme:
    """Restyle the whole application; returns the theme for widgets that paint themselves."""
    theme = load_theme(name)
    app = QApplication.instance()
    if not isinstance(app, QApplication):
        return theme
    load_fonts()
    app.setPalette(palette(theme))
    font = QFont(BODY_FONT)
    font.setPointSize(BODY_POINT_SIZE)
    app.setFont(font)
    app.setStyleSheet(render_qss(theme))
    return theme


def set_glow(widget: QWidget, theme: Theme) -> None:
    """A neon glow behind the widget; used sparingly (logo, Start) since it costs a redraw."""
    color = theme.glow
    if color is None:
        widget.setGraphicsEffect(None)
        return
    effect = QGraphicsDropShadowEffect(widget)
    effect.setBlurRadius(GLOW_BLUR)
    effect.setOffset(0, 0)
    effect.setColor(color)
    widget.setGraphicsEffect(effect)


def repolish(widget: QWidget) -> None:
    """Re-read the style sheet after a dynamic property used in a selector changed."""
    widget.style().unpolish(widget)
    widget.style().polish(widget)
    widget.update()
