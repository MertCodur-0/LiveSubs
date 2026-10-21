"""Which OCR languages Windows has, and how to add English when it is missing.

Turkish Windows installs usually lack the English OCR pack, so the startup check uses this to
explain the fix and to offer a one-click, elevated install.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes

from winrt.windows.media.ocr import OcrEngine

ENGLISH_US = "en-US"
EN_US_CAPABILITY = "Language.OCR~~~en-US~0.0.1.0"
INSTALL_COMMAND = f"Add-WindowsCapability -Online -Name {EN_US_CAPABILITY}"


def available_languages() -> list[str]:
    """BCP-47 tags of the installed OCR languages, e.g. ["en-US", "tr"]."""
    return [language.language_tag for language in OcrEngine.available_recognizer_languages]


def best_english(tags: list[str]) -> str | None:
    """en-US if installed, otherwise any other English variant (en-GB reads the same text)."""
    if ENGLISH_US in tags:
        return ENGLISH_US
    return next((tag for tag in tags if tag.lower().split("-")[0] == "en"), None)


def english_ocr_language() -> str | None:
    return best_english(available_languages())


_ShellExecuteW = ctypes.WinDLL("shell32").ShellExecuteW
_ShellExecuteW.restype = wintypes.HINSTANCE
_ShellExecuteW.argtypes = (
    wintypes.HWND,
    wintypes.LPCWSTR,
    wintypes.LPCWSTR,
    wintypes.LPCWSTR,
    wintypes.LPCWSTR,
    ctypes.c_int,
)
_SW_SHOWNORMAL = 1


def request_english_install() -> bool:
    """Run the install command in an elevated PowerShell (Windows shows the UAC prompt).

    Returns whether the process was started; False when the user declined elevation. The new
    language is usable once the window closes, usually without a restart; the caller re-checks
    ``english_ocr_language()``.
    """
    arguments = f'-NoProfile -Command "{INSTALL_COMMAND}"'
    result = _ShellExecuteW(None, "runas", "powershell.exe", arguments, None, _SW_SHOWNORMAL)
    return (result or 0) > 32  # ShellExecute success values are > 32
