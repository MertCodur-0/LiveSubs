"""Thin ctypes wrappers around the Win32 calls LiveSubs needs.

All coordinates are physical pixels. Every process that reads window or monitor geometry must be
per-monitor DPI aware (``enable_per_monitor_dpi_awareness``); Qt already makes the UI process so.
"""

from __future__ import annotations

import ctypes
from collections.abc import Iterable
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import PureWindowsPath

from livesubs.core.geometry import Rect

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_dwmapi = ctypes.WinDLL("dwmapi")
_shcore = ctypes.WinDLL("shcore")
_shell32 = ctypes.WinDLL("shell32")

_LONG_PTR = ctypes.c_ssize_t
_HRESULT = ctypes.c_long
_WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
_MONITORENUMPROC = ctypes.WINFUNCTYPE(
    wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.LPARAM
)


def _fn(dll: ctypes.WinDLL, name: str, restype: object, *argtypes: object) -> ctypes._CFuncPtr:
    func = getattr(dll, name)
    func.restype = restype
    func.argtypes = argtypes
    return func


_HWND, _BOOL, _UINT, _DWORD = wintypes.HWND, wintypes.BOOL, wintypes.UINT, wintypes.DWORD

_SetProcessDpiAwarenessContext = _fn(
    _user32, "SetProcessDpiAwarenessContext", _BOOL, wintypes.HANDLE
)
_GetThreadDpiAwarenessContext = _fn(_user32, "GetThreadDpiAwarenessContext", wintypes.HANDLE)
_AreDpiAwarenessContextsEqual = _fn(
    _user32, "AreDpiAwarenessContextsEqual", _BOOL, wintypes.HANDLE, wintypes.HANDLE
)
_GetDpiForWindow = _fn(_user32, "GetDpiForWindow", _UINT, _HWND)
_EnumWindows = _fn(_user32, "EnumWindows", _BOOL, _WNDENUMPROC, wintypes.LPARAM)
_IsWindow = _fn(_user32, "IsWindow", _BOOL, _HWND)
_IsWindowVisible = _fn(_user32, "IsWindowVisible", _BOOL, _HWND)
_IsIconic = _fn(_user32, "IsIconic", _BOOL, _HWND)
_GetWindow = _fn(_user32, "GetWindow", _HWND, _HWND, _UINT)
_GetWindowTextLengthW = _fn(_user32, "GetWindowTextLengthW", ctypes.c_int, _HWND)
_GetWindowTextW = _fn(_user32, "GetWindowTextW", ctypes.c_int, _HWND, wintypes.LPWSTR, ctypes.c_int)
_GetWindowThreadProcessId = _fn(
    _user32, "GetWindowThreadProcessId", _DWORD, _HWND, ctypes.POINTER(_DWORD)
)
_GetWindowLongPtrW = _fn(_user32, "GetWindowLongPtrW", _LONG_PTR, _HWND, ctypes.c_int)
_SetWindowLongPtrW = _fn(_user32, "SetWindowLongPtrW", _LONG_PTR, _HWND, ctypes.c_int, _LONG_PTR)
_SetWindowPos = _fn(
    _user32,
    "SetWindowPos",
    _BOOL,
    *(_HWND, _HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, _UINT),
)
_SetLayeredWindowAttributes = _fn(
    _user32, "SetLayeredWindowAttributes", _BOOL, _HWND, wintypes.COLORREF, wintypes.BYTE, _DWORD
)
_GetClientRect = _fn(_user32, "GetClientRect", _BOOL, _HWND, ctypes.POINTER(wintypes.RECT))
_ClientToScreen = _fn(_user32, "ClientToScreen", _BOOL, _HWND, ctypes.POINTER(wintypes.POINT))
_SetWindowDisplayAffinity = _fn(_user32, "SetWindowDisplayAffinity", _BOOL, _HWND, _DWORD)
_GetWindowDisplayAffinity = _fn(
    _user32, "GetWindowDisplayAffinity", _BOOL, _HWND, ctypes.POINTER(_DWORD)
)
_EnumDisplayMonitors = _fn(
    _user32,
    "EnumDisplayMonitors",
    _BOOL,
    *(wintypes.HDC, ctypes.POINTER(wintypes.RECT), _MONITORENUMPROC, wintypes.LPARAM),
)
_MonitorFromWindow = _fn(_user32, "MonitorFromWindow", wintypes.HMONITOR, _HWND, _DWORD)
_GetMonitorInfoW = _fn(_user32, "GetMonitorInfoW", _BOOL, wintypes.HMONITOR, ctypes.c_void_p)
_OpenProcess = _fn(_kernel32, "OpenProcess", wintypes.HANDLE, _DWORD, _BOOL, _DWORD)
_CloseHandle = _fn(_kernel32, "CloseHandle", _BOOL, wintypes.HANDLE)
_QueryFullProcessImageNameW = _fn(
    _kernel32,
    "QueryFullProcessImageNameW",
    _BOOL,
    *(wintypes.HANDLE, _DWORD, wintypes.LPWSTR, ctypes.POINTER(_DWORD)),
)
_DwmGetWindowAttribute = _fn(
    _dwmapi, "DwmGetWindowAttribute", _HRESULT, _HWND, _DWORD, ctypes.c_void_p, _DWORD
)
_GetDpiForMonitor = _fn(
    _shcore,
    "GetDpiForMonitor",
    _HRESULT,
    *(wintypes.HMONITOR, ctypes.c_int, ctypes.POINTER(_UINT), ctypes.POINTER(_UINT)),
)
_SHQueryUserNotificationState = _fn(
    _shell32, "SHQueryUserNotificationState", _HRESULT, ctypes.POINTER(ctypes.c_int)
)

_DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
_GW_OWNER = 4
_GWL_EXSTYLE = -20
_WS_EX_TRANSPARENT = 0x00000020
_WS_EX_TOOLWINDOW = 0x00000080
_WS_EX_APPWINDOW = 0x00040000
_WS_EX_LAYERED = 0x00080000
_WS_EX_NOACTIVATE = 0x08000000
_LWA_ALPHA = 0x2
_SWP_FLAGS_STYLE_CHANGED = 0x0001 | 0x0002 | 0x0004 | 0x0010 | 0x0020  # NOSIZE..FRAMECHANGED
_WDA_NONE = 0x0
WDA_EXCLUDEFROMCAPTURE = 0x11
_DWMWA_CLOAKED = 14
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_MONITOR_DEFAULTTONEAREST = 2
_MONITORINFOF_PRIMARY = 1
_MDT_EFFECTIVE_DPI = 0
_QUNS_RUNNING_D3D_FULL_SCREEN = 3
BASE_DPI = 96


class _MONITORINFOEXW(ctypes.Structure):
    _fields_ = (
        ("cbSize", _DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", _DWORD),
        ("szDevice", wintypes.WCHAR * 32),
    )


def _raise_last_error() -> None:
    raise ctypes.WinError(ctypes.get_last_error())


def _rect(r: wintypes.RECT) -> Rect:
    return Rect(r.left, r.top, r.right - r.left, r.bottom - r.top)


# --- DPI -----------------------------------------------------------------------------------------


def enable_per_monitor_dpi_awareness() -> bool:
    """Make this process per-monitor (v2) DPI aware so Win32 reports physical pixels.

    Returns whether the process is per-monitor aware afterwards. Fails harmlessly when the
    awareness was already set (by Qt or a manifest).
    """
    _SetProcessDpiAwarenessContext(_DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)
    return is_per_monitor_dpi_aware()


def is_per_monitor_dpi_aware() -> bool:
    return bool(
        _AreDpiAwarenessContextsEqual(
            _GetThreadDpiAwarenessContext(), _DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
        )
    )


def window_dpi(hwnd: int) -> int:
    """DPI of the monitor a window is on (96 = 100 %); 0 for an invalid window."""
    return _GetDpiForWindow(hwnd)


# --- Windows -------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WindowInfo:
    hwnd: int
    title: str
    pid: int
    exe_name: str  # "" when the process is not accessible (e.g. elevated)
    client_rect: Rect  # on the virtual desktop, physical pixels
    minimized: bool  # WGC cannot capture minimized windows


def is_window(hwnd: int) -> bool:
    return bool(_IsWindow(hwnd))


def window_title(hwnd: int) -> str:
    length = _GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buffer = ctypes.create_unicode_buffer(length + 1)
    _GetWindowTextW(hwnd, buffer, length + 1)
    return buffer.value


def window_pid(hwnd: int) -> int:
    pid = _DWORD()
    _GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def process_exe_name(pid: int) -> str:
    """File name of a process's executable, or "" when it cannot be queried."""
    handle = _OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        size = _DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not _QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return ""
        return PureWindowsPath(buffer.value).name
    finally:
        _CloseHandle(handle)


def client_rect(hwnd: int) -> Rect | None:
    """The window's client area on the virtual desktop, or None if the window is gone."""
    rect = wintypes.RECT()
    if not _GetClientRect(hwnd, ctypes.byref(rect)):
        return None
    origin = wintypes.POINT(0, 0)
    if not _ClientToScreen(hwnd, ctypes.byref(origin)):
        return None
    return Rect(origin.x, origin.y, rect.right - rect.left, rect.bottom - rect.top)


def _is_cloaked(hwnd: int) -> bool:
    """Hidden by DWM although "visible": UWP background apps, other virtual desktops."""
    cloaked = _DWORD()
    result = _DwmGetWindowAttribute(
        hwnd, _DWMWA_CLOAKED, ctypes.byref(cloaked), ctypes.sizeof(cloaked)
    )
    return result == 0 and cloaked.value != 0


def _is_app_window(hwnd: int) -> bool:
    """A top-level window a user would recognize in Alt+Tab."""
    if not _IsWindowVisible(hwnd) or _GetWindowTextLengthW(hwnd) <= 0:
        return False
    ex_style = _GetWindowLongPtrW(hwnd, _GWL_EXSTYLE)
    if ex_style & _WS_EX_TOOLWINDOW and not ex_style & _WS_EX_APPWINDOW:
        return False
    if _GetWindow(hwnd, _GW_OWNER) and not ex_style & _WS_EX_APPWINDOW:
        return False
    return not _is_cloaked(hwnd)


def list_windows(exclude_pid: int | None = None) -> list[WindowInfo]:
    """Capturable top-level windows in Z order (front first)."""
    handles: list[int] = []

    # Exceptions inside a ctypes callback are swallowed, so only collect handles there.
    @_WNDENUMPROC
    def collect(hwnd: int, _lparam: int) -> bool:
        handles.append(hwnd)
        return True

    _EnumWindows(collect, 0)

    windows = []
    for hwnd in handles:
        if not _is_app_window(hwnd):
            continue
        pid = window_pid(hwnd)
        rect = client_rect(hwnd)
        if pid == exclude_pid or rect is None:
            continue
        windows.append(
            WindowInfo(
                hwnd=hwnd,
                title=window_title(hwnd),
                pid=pid,
                exe_name=process_exe_name(pid),
                client_rect=rect,
                minimized=bool(_IsIconic(hwnd)),
            )
        )
    return windows


def best_window_match(
    windows: Iterable[WindowInfo], exe_name: str, title: str
) -> WindowInfo | None:
    """Find a profile's window again after a restart.

    Preference: same executable and title, then same executable (non-minimized first), then the
    same title from any executable. ``windows`` is expected in Z order, so ties go to the front.
    """
    windows = list(windows)
    exe = exe_name.lower()
    same_exe = [w for w in windows if exe and w.exe_name.lower() == exe]
    for w in same_exe:
        if w.title == title:
            return w
    if same_exe:
        return min(same_exe, key=lambda w: w.minimized)
    return next((w for w in windows if title and w.title == title), None)


# --- Overlay window styles -----------------------------------------------------------------------


def set_click_through(hwnd: int, enabled: bool) -> None:
    """Let mouse input pass through a window (and never activate it), or restore it."""
    ex_style = _GetWindowLongPtrW(hwnd, _GWL_EXSTYLE)
    if enabled:
        if not ex_style & _WS_EX_LAYERED:
            _set_ex_style(hwnd, ex_style | _WS_EX_LAYERED)
            # A layered window is invisible until its attributes are set. Only do this when we
            # added the flag: windows Qt made layered use UpdateLayeredWindow, which this breaks.
            if not _SetLayeredWindowAttributes(hwnd, 0, 255, _LWA_ALPHA):
                _raise_last_error()
        new_style = ex_style | _WS_EX_LAYERED | _WS_EX_TRANSPARENT | _WS_EX_NOACTIVATE
    else:
        new_style = ex_style & ~(_WS_EX_TRANSPARENT | _WS_EX_NOACTIVATE)
    _set_ex_style(hwnd, new_style)


def is_click_through(hwnd: int) -> bool:
    ex_style = _GetWindowLongPtrW(hwnd, _GWL_EXSTYLE)
    return bool(ex_style & _WS_EX_TRANSPARENT and ex_style & _WS_EX_LAYERED)


def _set_ex_style(hwnd: int, ex_style: int) -> None:
    ctypes.set_last_error(0)
    if not _SetWindowLongPtrW(hwnd, _GWL_EXSTYLE, ex_style) and ctypes.get_last_error():
        _raise_last_error()
    _SetWindowPos(hwnd, None, 0, 0, 0, 0, _SWP_FLAGS_STYLE_CHANGED)


def set_exclude_from_capture(hwnd: int, exclude: bool) -> bool:
    """Hide a window from screen capture (WGC, OBS, screenshots) so OCR never reads it.

    Needs Windows 10 2004+. Returns False when Windows refuses (e.g. no DWM composition).
    """
    return bool(_SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE if exclude else _WDA_NONE))


def display_affinity(hwnd: int) -> int:
    affinity = _DWORD()
    if not _GetWindowDisplayAffinity(hwnd, ctypes.byref(affinity)):
        _raise_last_error()
    return affinity.value


# --- Monitors ------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MonitorInfo:
    handle: int
    device_name: str  # e.g. r"\\.\DISPLAY1"; what profiles store
    rect: Rect  # virtual-desktop physical pixels
    work_rect: Rect  # without the taskbar
    is_primary: bool
    dpi: int

    @property
    def scale(self) -> float:
        return self.dpi / BASE_DPI


def _monitor_info(handle: int) -> MonitorInfo | None:
    info = _MONITORINFOEXW()
    info.cbSize = ctypes.sizeof(info)
    if not _GetMonitorInfoW(handle, ctypes.byref(info)):
        return None
    dpi_x, dpi_y = _UINT(), _UINT()
    if _GetDpiForMonitor(handle, _MDT_EFFECTIVE_DPI, ctypes.byref(dpi_x), ctypes.byref(dpi_y)):
        dpi_x.value = BASE_DPI
    return MonitorInfo(
        handle=handle,
        device_name=info.szDevice,
        rect=_rect(info.rcMonitor),
        work_rect=_rect(info.rcWork),
        is_primary=bool(info.dwFlags & _MONITORINFOF_PRIMARY),
        dpi=dpi_x.value,
    )


def list_monitors() -> list[MonitorInfo]:
    handles: list[int] = []

    @_MONITORENUMPROC
    def collect(handle: int, _hdc: int, _rect: object, _lparam: int) -> bool:
        handles.append(handle)
        return True

    _EnumDisplayMonitors(None, None, collect, 0)
    return [info for handle in handles if (info := _monitor_info(handle))]


def monitor_for_window(hwnd: int) -> MonitorInfo | None:
    """The monitor showing most of a window (the nearest one if it is off-screen)."""
    return _monitor_info(_MonitorFromWindow(hwnd, _MONITOR_DEFAULTTONEAREST))


# --- Fullscreen ----------------------------------------------------------------------------------


def is_exclusive_fullscreen_running() -> bool:
    """True while a Direct3D app runs in exclusive fullscreen, which overlays cannot cover."""
    state = ctypes.c_int()
    if _SHQueryUserNotificationState(ctypes.byref(state)) != 0:
        return False
    return state.value == _QUNS_RUNNING_D3D_FULL_SCREEN
