"""Windows' default sound output, read through Core Audio (``IMMDeviceEnumerator``).

PortAudio reads the default output only when it is initialised, so a stream opened on "the
default" stays on that device when the user picks another one in Windows. The id read here is
compared while playing to notice that change.
"""

from __future__ import annotations

import ctypes
import logging
from ctypes import wintypes

from livesubs.platform.com import GUID, method, release

log = logging.getLogger(__name__)

_CLSID_MM_DEVICE_ENUMERATOR = GUID.parse("{bcde0395-e52f-467c-8e3d-c4579291692e}")
_IID_IMM_DEVICE_ENUMERATOR = GUID.parse("{a95664d2-9614-4f35-a746-de8db63617e6}")
_GET_DEFAULT_AUDIO_ENDPOINT = 4  # IMMDeviceEnumerator vtable slot
_GET_ID = 5  # IMMDevice vtable slot
_E_RENDER = 0
_E_MULTIMEDIA = 1  # the role PortAudio's WASAPI host asks for
_CLSCTX_ALL = 0x17
_COINIT_MULTITHREADED = 0x0


def default_output_id() -> str:
    """The endpoint id of Windows' default sound output; "" when there is none."""
    ole32 = ctypes.windll.ole32
    ole32.CoTaskMemFree.argtypes = (ctypes.c_void_p,)
    # S_OK / S_FALSE: ours to undo. RPC_E_CHANGED_MODE (Qt's STA thread): COM is up already.
    initialized = ole32.CoInitializeEx(None, _COINIT_MULTITHREADED) >= 0
    enumerator = ctypes.c_void_p()
    device = ctypes.c_void_p()
    try:
        if (
            ole32.CoCreateInstance(
                ctypes.byref(_CLSID_MM_DEVICE_ENUMERATOR),
                None,
                _CLSCTX_ALL,
                ctypes.byref(_IID_IMM_DEVICE_ENUMERATOR),
                ctypes.byref(enumerator),
            )
            < 0
        ):
            log.warning("MMDeviceEnumerator unavailable")
            return ""
        get_default = method(
            enumerator,
            _GET_DEFAULT_AUDIO_ENDPOINT,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_void_p),
        )
        if get_default(enumerator, _E_RENDER, _E_MULTIMEDIA, ctypes.byref(device)) < 0:
            return ""  # E_NOTFOUND: no output connected
        text = wintypes.LPWSTR()
        if method(device, _GET_ID, ctypes.POINTER(wintypes.LPWSTR))(device, ctypes.byref(text)) < 0:
            return ""
        try:
            return text.value or ""
        finally:
            ole32.CoTaskMemFree(text)
    finally:
        if device:
            release(device)
        if enumerator:
            release(enumerator)
        if initialized:
            ole32.CoUninitialize()
