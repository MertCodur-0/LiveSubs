"""Themes, bundled fonts, the SVG icon and header, overlay styles."""

import struct
from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFontDatabase, QRawFont
from PySide6.QtWidgets import QApplication
from pytestqt.qtbot import QtBot

from livesubs import APP_NAME
from livesubs.core.config import THEME_CHOICES, AppConfig
from livesubs.core.geometry import Rect
from livesubs.core.profiles import OverlaySettings, OverlayStyle
from livesubs.ui import theme
from livesubs.ui.header import Header
from livesubs.ui.icon import SIZES, app_icon, render, write_ico
from livesubs.ui.overlay import STYLE_PRESETS, VICE_HALO, Overlay, apply_style

TURKISH = "çğıİöşüÇĞÖŞÜ"
# Tokens that feed the palette or painted widgets: colors in every theme (QColor parses them).
COLOR_TOKENS = ("window", "sidebar", "surface", "text", "text_muted", "accent", "separator")


@pytest.mark.parametrize("name", theme.THEMES)
def test_every_theme_fills_the_template(name: str) -> None:
    qss = theme.render_qss(theme.load_theme(name))  # raises on a missing token
    assert "{{" not in qss
    assert "#startButton" in qss


def test_themes_define_the_same_tokens() -> None:
    first, *others = (theme.load_theme(name) for name in theme.THEMES)
    for other in others:
        assert other.tokens.keys() == first.tokens.keys(), other.name


@pytest.mark.parametrize("name", theme.THEMES)
def test_color_tokens_are_colors(name: str) -> None:
    current = theme.load_theme(name)
    for token in COLOR_TOKENS:
        assert current.color(token).isValid(), token
    theme.palette(current)  # every palette token parses


def test_apple_colors_follow_the_design() -> None:
    dark, light = theme.load_theme("apple"), theme.load_theme("apple_light")
    assert (dark.tokens["window"], dark.tokens["surface"]) == ("#1C1C1E", "#2C2C2E")
    assert dark.tokens["accent"] == dark.tokens["start_bg"] == "#0A84FF"
    assert (light.tokens["window"], light.tokens["surface"]) == ("#F5F5F7", "#FFFFFF")
    assert (light.tokens["text"], light.tokens["accent"]) == ("#1D1D1F", "#007AFF")
    for apple in (dark, light):
        assert apple.tokens["font_heading"] == apple.tokens["font_logo"] == "Inter"
        assert apple.glow is None and not apple.header_art


@pytest.mark.parametrize("name", ["apple", "apple_light", "dark"])
def test_plain_themes_have_no_gradients(name: str) -> None:
    qss = theme.render_qss(theme.load_theme(name))
    assert "gradient" not in qss
    assert "border-radius: 12px" in qss  # cards
    assert "border-radius: 22px" in qss  # the Start/Stop pill


def test_vice_keeps_its_look() -> None:
    vice = theme.load_theme("vice")
    assert vice.header_art and vice.glow is not None
    assert "qlineargradient" in vice.tokens["start_bg"]
    assert (vice.tokens["font_heading"], vice.tokens["font_logo"]) == ("Audiowide", "Monoton")


def test_unknown_theme_falls_back() -> None:
    assert theme.load_theme("neon-pink-2000").name == theme.FALLBACK_THEME == "apple"


def test_choices_match_the_config() -> None:
    assert theme.CHOICES == THEME_CHOICES
    assert set(theme.THEMES) < set(theme.CHOICES)
    assert AppConfig().theme == theme.DEFAULT_CHOICE == "apple"
    assert AppConfig(theme="vice").theme == "vice"
    assert AppConfig(theme="light").theme == "apple"  # unknown names get the default


def test_resolve_follows_the_system(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in theme.THEMES:
        assert theme.resolve(name) == name
    for scheme, expected in (("dark", "apple"), ("light", "apple_light")):
        monkeypatch.setattr(theme, "system_scheme", lambda s=scheme: s)
        assert theme.resolve("system") == expected


def test_template_assets_exist() -> None:
    qss = theme.render_qss(theme.load_theme(theme.FALLBACK_THEME))
    for name in ("arrow_up.svg", "arrow_down.svg", "check.svg"):
        assert f"{theme.ASSETS.as_posix()}/{name}" in qss
        assert theme.asset(name).is_file()


def test_bundled_fonts_load_and_cover_turkish(qapp: QApplication) -> None:
    theme.load_fonts()
    families = set(QFontDatabase.families())
    assert {"Inter", "Audiowide", "Monoton"} <= families
    for path in (theme.ASSETS / "fonts").glob("*.ttf"):
        font = QRawFont(str(path), 20)
        # The int overload: PySide maps non-ASCII str arguments wrongly.
        assert all(font.supportsCharacter(ord(c)) for c in TURKISH), path.name
    licenses = {p.name for p in (theme.ASSETS / "fonts").glob("OFL-*.txt")}
    assert licenses == {"OFL-inter.txt", "OFL-audiowide.txt", "OFL-monoton.txt"}


@pytest.mark.parametrize("name", theme.THEMES)
def test_apply_theme_restyles_the_app(qapp: QApplication, name: str) -> None:
    try:
        applied = theme.apply_theme(name)
        assert applied.name == name
        assert qapp.palette().window().color() == applied.color("window")
        assert applied.tokens["accent"] in qapp.styleSheet()
        assert qapp.font().family() == theme.BODY_FONT
        # A forced theme also sets Qt's scheme (the Windows title bar follows it).
        light = name in theme.LIGHT_THEMES
        expected = Qt.ColorScheme.Light if light else Qt.ColorScheme.Dark
        assert qapp.styleHints().colorScheme() == expected
    finally:
        theme.apply_theme(theme.DEFAULT_CHOICE)


# --- icon and header ------------------------------------------------------------------------------


def test_icon_renders_every_size(qapp: QApplication) -> None:
    sizes = {size.width() for size in app_icon().availableSizes()}
    assert set(SIZES) <= sizes
    image = render(32)
    corner, center = image.pixelColor(0, 0), image.pixelColor(16, 16)
    assert corner.alpha() == 0  # rounded corners stay transparent
    assert center.alpha() == 255


def test_write_ico(qapp: QApplication, tmp_path: Path) -> None:
    path = tmp_path / "icon.ico"
    write_ico(path)
    data = path.read_bytes()
    reserved, kind, count = struct.unpack_from("<HHH", data)
    assert (reserved, kind, count) == (0, 1, len(SIZES))
    for i in range(count):
        _, _, _, _, _, _, size, offset = struct.unpack_from("<BBBBHHII", data, 6 + 16 * i)
        assert data[offset : offset + 8] == b"\x89PNG\r\n\x1a\n"
        assert offset + size <= len(data)


def test_shipped_ico_is_valid() -> None:
    data = theme.asset("icon.ico").read_bytes()
    assert struct.unpack_from("<HHH", data) == (0, 1, len(SIZES))


@pytest.mark.parametrize("name", theme.THEMES)
def test_header_paints(qtbot: QtBot, name: str) -> None:
    current = theme.load_theme(name)
    header = Header(current)
    qtbot.addWidget(header)
    header.resize(200, 48)
    assert header.logo.text() == APP_NAME
    assert not header.icon.pixmap().isNull()
    assert (header.logo.graphicsEffect() is not None) == (current.glow is not None)
    image = header.grab().toImage()
    art = image.pixelColor(image.width() - 20, 30)  # the banner's right part
    if current.header_art:
        assert art != current.color("sidebar")


# --- overlay styles -------------------------------------------------------------------------------


def test_apply_style_sets_the_preset() -> None:
    settings = OverlaySettings()
    apply_style(settings, OverlayStyle.BOX)
    assert settings.style == OverlayStyle.BOX
    assert settings.outline_px == 0 and settings.background
    apply_style(settings, OverlayStyle.CLASSIC)
    for field, value in STYLE_PRESETS[OverlayStyle.CLASSIC].items():
        assert getattr(settings, field) == value


def test_classic_preset_is_the_default_look() -> None:
    defaults = OverlaySettings()
    assert defaults.style == OverlayStyle.CLASSIC
    for field, value in STYLE_PRESETS[OverlayStyle.CLASSIC].items():
        assert getattr(defaults, field) == value


def _colors(overlay: Overlay) -> set[str]:
    image = overlay.grab().toImage()
    return {
        image.pixelColor(x, y).name()
        for x in range(0, image.width(), 2)
        for y in range(0, image.height(), 2)
    }


def test_vice_draws_a_cyan_halo(qtbot: QtBot) -> None:
    settings = OverlaySettings()
    overlay = Overlay(settings)
    qtbot.addWidget(overlay)
    overlay.place(Rect(0, 0, 700, 120))
    overlay.show_text("Aynasızlar geliyor!")
    halo = QColor(VICE_HALO).name()
    assert halo not in _colors(overlay)
    apply_style(settings, OverlayStyle.VICE)
    assert halo in _colors(overlay)
