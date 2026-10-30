"""Video capture devices (capture cards, webcams), listed through Media Foundation.

``MFEnumDeviceSources`` is what OpenCV's MSMF backend enumerates too, so a device's position in
this list is its OpenCV index (``cv2.VideoCapture(index, cv2.CAP_MSMF)``). DirectShow-only
devices such as "OBS Virtual Camera" are not listed: MSMF cannot open them anyway.
"""

from __future__ import annotations

import ctypes
import logging
from ctypes import wintypes
from dataclasses import dataclass

from livesubs.platform.com import GUID, method, release

log = logging.getLogger(__name__)

# IMFAttributes vtable slots (IUnknown 0-2 first); IMFActivate inherits them.
_GET_ALLOCATED_STRING = 13
_SET_GUID = 24

_SOURCE_TYPE = GUID.parse("{c60ac5fe-252a-478f-a0ef-bc8fa5f7cad3}")
_SOURCE_TYPE_VIDCAP = GUID.parse("{8ac3587a-4ae7-42d8-99e0-0a6013eef90f}")
_FRIENDLY_NAME = GUID.parse("{60d0e559-52f8-4fa2-bbce-acdb34a8ec01}")
_VIDCAP_SYMBOLIC_LINK = GUID.parse("{58f0aad8-22bf-4f8a-bb3d-d2c4978c6e2f}")

_COINIT_MULTITHREADED = 0x0


@dataclass(frozen=True, slots=True)
class VideoDevice:
    index: int  # OpenCV MSMF index
    name: str  # friendly name, e.g. "V-Z632"; what profiles store
    symbolic_link: str  # unique per device instance (two identical cards differ here)


def _allocated_string(activate: ctypes.c_void_p, key: GUID) -> str:
    get = method(
        activate,
        _GET_ALLOCATED_STRING,
        ctypes.POINTER(GUID),
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(wintypes.UINT),
    )
    text = wintypes.LPWSTR()
    length = wintypes.UINT()
    if get(activate, ctypes.byref(key), ctypes.byref(text), ctypes.byref(length)) < 0:
        return ""
    try:
        return text.value or ""
    finally:
        ctypes.windll.ole32.CoTaskMemFree(text)


def list_video_devices() -> list[VideoDevice]:
    """Video capture devices in MSMF order; empty when Media Foundation is unavailable."""
    try:
        mfplat = ctypes.WinDLL("mfplat")
        mf = ctypes.WinDLL("mf")
    except OSError:
        return []
    ole32 = ctypes.windll.ole32
    ole32.CoTaskMemFree.argtypes = (ctypes.c_void_p,)
    # S_OK / S_FALSE: ours to undo. RPC_E_CHANGED_MODE (Qt's STA thread): COM is up already.
    initialized = ole32.CoInitializeEx(None, _COINIT_MULTITHREADED) >= 0
    attributes = ctypes.c_void_p()
    try:
        if mfplat.MFCreateAttributes(ctypes.byref(attributes), 1) < 0:
            log.warning("MFCreateAttributes failed")
            return []
        set_guid = method(attributes, _SET_GUID, ctypes.POINTER(GUID), ctypes.POINTER(GUID))
        set_guid(attributes, ctypes.byref(_SOURCE_TYPE), ctypes.byref(_SOURCE_TYPE_VIDCAP))
        activates = ctypes.POINTER(ctypes.c_void_p)()
        count = wintypes.UINT()
        if mf.MFEnumDeviceSources(attributes, ctypes.byref(activates), ctypes.byref(count)) < 0:
            log.warning("MFEnumDeviceSources failed")
            return []
        devices = []
        for index in range(count.value):
            activate = ctypes.c_void_p(activates[index])
            try:
                devices.append(
                    VideoDevice(
                        index,
                        _allocated_string(activate, _FRIENDLY_NAME),
                        _allocated_string(activate, _VIDCAP_SYMBOLIC_LINK),
                    )
                )
            finally:
                release(activate)
        ole32.CoTaskMemFree(activates)
        return devices
    finally:
        if attributes:
            release(attributes)
        if initialized:
            ole32.CoUninitialize()


def find_video_device(name: str) -> VideoDevice | None:
    """The first device called ``name`` (case-insensitive)."""
    wanted = name.casefold()
    return next((d for d in list_video_devices() if d.name.casefold() == wanted), None)
