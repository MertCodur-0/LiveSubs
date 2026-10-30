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
