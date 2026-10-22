"""Graphics adapters and their dedicated memory, read through DXGI.

DXGI rather than WMI: ``Win32_VideoController.AdapterRAM`` is a 32-bit field (caps at 4 GB), and
WMI must stay out of the engine process anyway (see the WGC pitfalls in CLAUDE.md).
"""

from __future__ import annotations

import ctypes
import logging
from ctypes import wintypes
from dataclasses import dataclass

log = logging.getLogger(__name__)

_DXGI_ADAPTER_FLAG_SOFTWARE = 2  # Microsoft Basic Render Driver
# COM vtable slots (IUnknown 0-2, IDXGIObject 3-6, then the interface's own methods)
_RELEASE = 2
_ENUM_ADAPTERS1 = 12  # IDXGIFactory1
_GET_DESC1 = 10  # IDXGIAdapter1


class _GUID(ctypes.Structure):
    _fields_ = (
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    )


# IID_IDXGIFactory1 {770aae78-f26f-4dba-a829-253c83d1b387}
_IID_IDXGIFactory1 = _GUID(
    0x770AAE78, 0xF26F, 0x4DBA, (ctypes.c_ubyte * 8)(0xA8, 0x29, 0x25, 0x3C, 0x83, 0xD1, 0xB3, 0x87)
)


class _DXGI_ADAPTER_DESC1(ctypes.Structure):  # noqa: N801 - Windows name
    _fields_ = (
        ("Description", wintypes.WCHAR * 128),
        ("VendorId", wintypes.UINT),
        ("DeviceId", wintypes.UINT),
        ("SubSysId", wintypes.UINT),
        ("Revision", wintypes.UINT),
        ("DedicatedVideoMemory", ctypes.c_size_t),
        ("DedicatedSystemMemory", ctypes.c_size_t),
        ("SharedSystemMemory", ctypes.c_size_t),
        ("AdapterLuid", ctypes.c_int64),
        ("Flags", wintypes.UINT),
    )


@dataclass(frozen=True, slots=True)
class GpuAdapter:
    name: str
    dedicated_bytes: int


def _method(obj: ctypes.c_void_p, slot: int, *argtypes: object) -> ctypes._CFuncPtr:
    vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    prototype = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)
    return prototype(vtable[slot])


def _release(obj: ctypes.c_void_p) -> None:
    ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(
        ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents[_RELEASE]
    )(obj)


def list_adapters() -> list[GpuAdapter]:
    """Hardware graphics adapters, largest dedicated memory first; empty when DXGI fails."""
    try:
        create = ctypes.WinDLL("dxgi").CreateDXGIFactory1
    except OSError:
        return []
    create.restype = ctypes.c_long
    create.argtypes = (ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p))
    factory = ctypes.c_void_p()
    if create(ctypes.byref(_IID_IDXGIFactory1), ctypes.byref(factory)) < 0:
        log.warning("CreateDXGIFactory1 failed")
        return []
    adapters = []
    try:
        enum = _method(factory, _ENUM_ADAPTERS1, wintypes.UINT, ctypes.POINTER(ctypes.c_void_p))
        index = 0
        while True:
            adapter = ctypes.c_void_p()
            if enum(factory, index, ctypes.byref(adapter)) < 0:  # DXGI_ERROR_NOT_FOUND: done
                break
            index += 1
            try:
                desc = _DXGI_ADAPTER_DESC1()
                get_desc = _method(adapter, _GET_DESC1, ctypes.POINTER(_DXGI_ADAPTER_DESC1))
                if get_desc(adapter, ctypes.byref(desc)) < 0:
                    continue
                if desc.Flags & _DXGI_ADAPTER_FLAG_SOFTWARE:
                    continue
                adapters.append(GpuAdapter(desc.Description, desc.DedicatedVideoMemory))
            finally:
                _release(adapter)
    finally:
        _release(factory)
    return sorted(adapters, key=lambda a: a.dedicated_bytes, reverse=True)


def main_adapter() -> GpuAdapter | None:
    """The adapter Ollama will use: the one with the most dedicated memory."""
    adapters = list_adapters()
    return adapters[0] if adapters else None
