import ctypes

import pytest
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QWidget
from pytestqt.qtbot import QtBot

from livesubs.platform import hotkeys
from livesubs.ui.hotkeys import HotkeyManager


@pytest.mark.parametrize(
    ("text", "modifiers", "vk"),
    [
        ("F8", 0, 0x77),
        ("f10", 0, 0x79),
        ("F24", 0, 0x87),
        ("Ctrl+Shift+P", hotkeys.MOD_CONTROL | hotkeys.MOD_SHIFT, ord("P")),
        ("Alt + 5", hotkeys.MOD_ALT, ord("5")),
        ("Win+Pause", hotkeys.MOD_WIN, 0x13),
        ("Num3", 0, 0x63),
    ],
)
def test_parse(text: str, modifiers: int, vk: int) -> None:
    hotkey = hotkeys.parse(text)
    assert (hotkey.modifiers, hotkey.vk, hotkey.text) == (modifiers, vk, text)


@pytest.mark.parametrize("text", ["", "Hyper+F8", "Ctrl+", "F99", "Ctrl+Ğ"])
def test_parse_rejects_nonsense(text: str) -> None:
    with pytest.raises(hotkeys.HotkeyError):
        hotkeys.parse(text)


def test_manager_turns_wm_hotkey_into_an_action(qtbot: QtBot) -> None:
    if QGuiApplication.platformName() != "windows":
        pytest.skip("needs real windows (CI runs offscreen)")
    window = QWidget()
    qtbot.addWidget(window)
    window.show()
    hwnd = int(window.winId())
    manager = HotkeyManager(hwnd)
    try:
        # An unusual combination nobody else owns; registration itself must succeed.
        assert manager.register_all({"calibration": "Ctrl+Alt+Shift+F24", "bad": "F99"}) == ["F99"]
        # Deliver the message Windows would send, without injecting real key presses.
        with qtbot.waitSignal(manager.triggered) as fired:
            ctypes.windll.user32.PostMessageW(hwnd, hotkeys.WM_HOTKEY, 1, 0)
        assert fired.args == ["calibration"]
    finally:
        manager.close()
