"""Global hotkeys through ``RegisterHotKey`` — no low-level keyboard hook.

A hook would see every key the player presses (anti-cheat software dislikes that) and may need
admin rights; a registered hotkey is one key combination Windows reports to us, nothing else.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_RegisterHotKey = _user32.RegisterHotKey
_RegisterHotKey.argtypes = (wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT)
_RegisterHotKey.restype = wintypes.BOOL
_UnregisterHotKey = _user32.UnregisterHotKey
_UnregisterHotKey.argtypes = (wintypes.HWND, ctypes.c_int)
_UnregisterHotKey.restype = wintypes.BOOL

WM_HOTKEY = 0x0312
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x8, 0x4000

_MODIFIERS = {
    "ctrl": MOD_CONTROL,
    "control": MOD_CONTROL,
    "alt": MOD_ALT,
    "shift": MOD_SHIFT,
    "win": MOD_WIN,
}
_NAMED_KEYS = {
    "pause": 0x13,
    "space": 0x20,
    "pageup": 0x21,
    "pgup": 0x21,  # Qt's spelling (key sequence editor)
    "pagedown": 0x22,
    "pgdown": 0x22,
    "end": 0x23,
    "home": 0x24,
    "insert": 0x2D,
    "ins": 0x2D,
    "delete": 0x2E,
    "del": 0x2E,
    "scrolllock": 0x91,
    **{f"f{n}": 0x6F + n for n in range(1, 25)},  # F1 = 0x70 ... F24 = 0x87
    **{f"num{n}": 0x60 + n for n in range(10)},
}


class HotkeyError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Hotkey:
    modifiers: int
    vk: int
    text: str  # as written in the settings, for messages


def parse(text: str) -> Hotkey:
    """'F8', 'Ctrl+Shift+P', 'Alt+F10' -> modifiers and virtual-key code."""
    parts = [p.strip().lower() for p in text.split("+") if p.strip()]
    if not parts:
        raise HotkeyError("empty hotkey")
    *mods, key = parts
    modifiers = 0
    for mod in mods:
        if mod not in _MODIFIERS:
            raise HotkeyError(f"unknown modifier {mod!r} in {text!r}")
        modifiers |= _MODIFIERS[mod]
    if key in _NAMED_KEYS:
        vk = _NAMED_KEYS[key]
    elif len(key) == 1 and key.isascii() and key.isalnum():
        vk = ord(key.upper())  # 'A'..'Z' and '0'..'9' are their own VK codes
    else:
        raise HotkeyError(f"unknown key {key!r} in {text!r}")
    return Hotkey(modifiers, vk, text)


def register(hwnd: int, hotkey_id: int, hotkey: Hotkey) -> bool:
    """False when another application already owns the combination."""
    return bool(_RegisterHotKey(hwnd, hotkey_id, hotkey.modifiers | MOD_NOREPEAT, hotkey.vk))


def unregister(hwnd: int, hotkey_id: int) -> None:
    _UnregisterHotKey(hwnd, hotkey_id)


class MSG(ctypes.Structure):
    _fields_ = (
        ("hwnd", wintypes.HWND),
        ("message", wintypes.UINT),
        ("wParam", wintypes.WPARAM),
        ("lParam", wintypes.LPARAM),
        ("time", wintypes.DWORD),
        ("pt", wintypes.POINT),
    )


def hotkey_id_of(message_address: int) -> int | None:
    """The hotkey id if the native message at that address is WM_HOTKEY."""
    msg = MSG.from_address(message_address)
    return int(msg.wParam) if msg.message == WM_HOTKEY else None
