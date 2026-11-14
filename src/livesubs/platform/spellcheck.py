"""Windows' built-in spell checker (``ISpellChecker``, Windows 8+), through COM.

Used to tell a clean OCR read from a garbled one ("Hey! Get out!" vs "Heyl Get outl"). Nothing is
added to the user's dictionary. One instance belongs to the thread that created it.
"""

from __future__ import annotations

import ctypes
import logging
from ctypes import wintypes

from livesubs.platform.com import GUID, method, release

log = logging.getLogger(__name__)

_CLSID_SPELL_CHECKER_FACTORY = GUID.parse("{7AB36653-1796-484B-BDFA-E74F1DB7C1DC}")
_IID_SPELL_CHECKER_FACTORY = GUID.parse("{8E018A9D-2415-4677-BF08-794EA61F94BB}")
_IS_SUPPORTED = 4  # ISpellCheckerFactory vtable slots
_CREATE_SPELL_CHECKER = 5
_CHECK = 4  # ISpellChecker
_NEXT = 3  # IEnumSpellingError
_START_INDEX = 3  # ISpellingError
_LENGTH = 4
_CLSCTX_INPROC_SERVER = 0x1
_COINIT_MULTITHREADED = 0x0


class SpellCheckerUnavailableError(RuntimeError):
    pass


class SpellChecker:
    """``misspelled(text)``: the words of ``text`` the language's dictionary does not know."""

    def __init__(self, language: str = "en-US") -> None:
        ole32 = ctypes.windll.ole32
        # S_OK / S_FALSE / RPC_E_CHANGED_MODE: COM is usable on this thread either way.
        ole32.CoInitializeEx(None, _COINIT_MULTITHREADED)
        self._checker = ctypes.c_void_p()
        factory = ctypes.c_void_p()
        hr = ole32.CoCreateInstance(
            ctypes.byref(_CLSID_SPELL_CHECKER_FACTORY),
            None,
            _CLSCTX_INPROC_SERVER,
            ctypes.byref(_IID_SPELL_CHECKER_FACTORY),
            ctypes.byref(factory),
        )
        if hr < 0 or not factory:
            raise SpellCheckerUnavailableError(f"SpellCheckerFactory: 0x{hr & 0xFFFFFFFF:08X}")
        try:
            supported = wintypes.BOOL()
            is_supported = method(
                factory, _IS_SUPPORTED, wintypes.LPCWSTR, ctypes.POINTER(wintypes.BOOL)
            )
            if is_supported(factory, language, ctypes.byref(supported)) < 0 or not supported:
                raise SpellCheckerUnavailableError(f"no {language} dictionary")
            create = method(
                factory, _CREATE_SPELL_CHECKER, wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)
            )
            if create(factory, language, ctypes.byref(self._checker)) < 0 or not self._checker:
                raise SpellCheckerUnavailableError(f"no {language} spell checker")
        finally:
            release(factory)

    def misspelled(self, text: str) -> list[str]:
        if not text.strip() or not self._checker:
            return []
        errors = ctypes.c_void_p()
        check = method(self._checker, _CHECK, wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p))
        if check(self._checker, text, ctypes.byref(errors)) < 0 or not errors:
            return []
        words = []
        try:
            next_error = method(errors, _NEXT, ctypes.POINTER(ctypes.c_void_p))
            while True:
                error = ctypes.c_void_p()
                if next_error(errors, ctypes.byref(error)) != 0 or not error:  # S_FALSE: done
                    break
                try:
                    start, length = wintypes.ULONG(), wintypes.ULONG()
                    method(error, _START_INDEX, ctypes.POINTER(wintypes.ULONG))(
                        error, ctypes.byref(start)
                    )
                    method(error, _LENGTH, ctypes.POINTER(wintypes.ULONG))(
                        error, ctypes.byref(length)
                    )
                    words.append(text[start.value : start.value + length.value])
                finally:
                    release(error)
        finally:
            release(errors)
        return words

    def close(self) -> None:
        if self._checker:
            release(self._checker)
            self._checker = ctypes.c_void_p()


def open_spell_checker(language: str = "en-US") -> SpellChecker | None:
    """The spell checker, or ``None`` (logged) where Windows has none for ``language``."""
    try:
        return SpellChecker(language)
    except (OSError, SpellCheckerUnavailableError) as error:
        log.warning("Spell checker unavailable: %s", error)
        return None
