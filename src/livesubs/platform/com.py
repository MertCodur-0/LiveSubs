"""Minimal COM plumbing for ctypes: GUIDs and vtable calls (DXGI, Media Foundation)."""

from __future__ import annotations

import ctypes
import uuid
from ctypes import wintypes

RELEASE = 2  # IUnknown::Release vtable slot


class GUID(ctypes.Structure):
    _fields_ = (
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    )

    @classmethod
    def parse(cls, text: str) -> GUID:
        """From the usual "{xxxxxxxx-xxxx-...}" form."""
        value = uuid.UUID(text)
        return cls(
            value.time_low,
            value.time_mid,
            value.time_hi_version,
            (ctypes.c_ubyte * 8)(*value.bytes[8:]),
        )


def method(obj: ctypes.c_void_p, slot: int, *argtypes: object) -> ctypes._CFuncPtr:
    """The HRESULT-returning method at vtable ``slot`` of ``obj``; call it with ``obj`` first."""
    vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    prototype = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)
    return prototype(vtable[slot])


def release(obj: ctypes.c_void_p) -> None:
    ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(
        ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents[RELEASE]
    )(obj)


_mta_cookie: ctypes.c_void_p | None = None


def keep_mta_alive() -> bool:
    """Keep this process's COM multithreaded apartment alive until the process ends.

    The MTA lives as long as some thread has COM initialised in it. The pipeline thread does
    (WinRT OCR, the spell checker) and ends with every Stop; when it was the only one, the
    apartment was torn down with it, and the next Windows Graphics Capture session crashed the
    process in ``start_free_threaded`` (windows-capture keeps objects of the old apartment).
    Reproduced on a browser window playing video: 5 of 8 runs of eight start/stop rounds
    crashed, none of 8 with the apartment kept. ``CoIncrementMTAUsage`` is made for this.
    """
    global _mta_cookie
    if _mta_cookie is not None:
        return True
    cookie = ctypes.c_void_p()
    try:
        increment = ctypes.windll.combase.CoIncrementMTAUsage
    except (AttributeError, OSError):  # not Windows 8+
        return False
    increment.argtypes = (ctypes.POINTER(ctypes.c_void_p),)
    increment.restype = ctypes.c_long
    if increment(ctypes.byref(cookie)) < 0:
        return False
    _mta_cookie = cookie
    return True
