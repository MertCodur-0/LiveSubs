"""Themes, bundled fonts, the SVG icon and header, overlay styles."""

import struct
from pathlib import Path

import pytest
from PySide6.QtGui import QColor, QFontDatabase, QRawFont
from PySide6.QtWidgets import QApplication
from pytestqt.qtbot import QtBot

from livesubs.core.geometry import Rect
from livesubs.core.profiles import OverlaySettings, OverlayStyle
from livesubs.ui import theme
from livesubs.ui.header import Header
from livesubs.ui.icon import SIZES, app_icon, render, write_ico
from livesubs.ui.overlay import STYLE_PRESETS, VICE_HALO, Overlay, apply_style

TURKISH = "çğıİöşüÇĞÖŞÜ"


@pytest.mark.parametrize("name", theme.THEMES)
def test_every_theme_fills_the_template(name: str) -> None:
    qss = theme.render_qss(theme.load_theme(name))  # raises on a missing token
    assert "{{" not in qss
    assert "#startButton" in qss


def test_themes_define_the_same_tokens() -> None:
    vice, dark = (theme.load_theme(name) for name in theme.THEMES)
    assert vice.tokens.keys() == dark.tokens.keys()
    assert vice.header_art and not dark.header_art


def test_unknown_theme_falls_back_to_the_default() -> None:
    assert theme.load_theme("neon-pink-2000").name == theme.DEFAULT_THEME


def test_template_assets_exist() -> None:
    qss = theme.render_qss(theme.load_theme(theme.DEFAULT_THEME))
    for name in ("arrow_up.svg", "arrow_down.svg"):
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


def test_apply_theme_restyles_the_app(qapp: QApplication) -> None:
    try:
        dark = theme.apply_theme("dark")
        assert qapp.palette().window().color() == dark.color("window")
        assert dark.tokens["accent"] in qapp.styleSheet()
        assert qapp.font().family() == theme.BODY_FONT
    finally:
        theme.apply_theme(theme.DEFAULT_THEME)


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
    header.resize(1000, 88)
    image = header.grab().toImage()
    sun = image.pixelColor(image.width() - 270, 60)  # where the banner's sun sits
    if current.header_art:
        assert sun != current.color("window")
    else:
        assert sun == current.color("window")
    assert image.pixelColor(5, 5) == current.color("window")  # the art fades into the window


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
