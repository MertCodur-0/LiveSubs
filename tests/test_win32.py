import ctypes
import os
from collections.abc import Iterator
from ctypes import wintypes

import pytest

from livesubs.core.geometry import Rect
from livesubs.platform import win32

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_CreateWindowExW = _user32.CreateWindowExW
_CreateWindowExW.restype = wintypes.HWND
_CreateWindowExW.argtypes = (
    wintypes.DWORD,
    wintypes.LPCWSTR,
    wintypes.LPCWSTR,
    wintypes.DWORD,
    *(ctypes.c_int,) * 4,
    wintypes.HWND,
    wintypes.HMENU,
    wintypes.HINSTANCE,
    wintypes.LPVOID,
)
_DestroyWindow = _user32.DestroyWindow
_DestroyWindow.argtypes = (wintypes.HWND,)

_WS_POPUP = 0x80000000
_WS_VISIBLE = 0x10000000
TITLE = f"LiveSubs test window {os.getpid()}"
GEOMETRY = Rect(100, 120, 300, 200)


@pytest.fixture(scope="module", autouse=True)
def _dpi_aware() -> None:
    win32.enable_per_monitor_dpi_awareness()


@pytest.fixture
def hwnd() -> Iterator[int]:
    """A borderless top-level window, so its client area equals its window rect."""
    handle = _CreateWindowExW(
        0,
        "STATIC",
        TITLE,
        _WS_POPUP | _WS_VISIBLE,
        GEOMETRY.x,
        GEOMETRY.y,
        GEOMETRY.width,
        GEOMETRY.height,
        None,
        None,
        None,
        None,
    )
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    yield handle
    _DestroyWindow(handle)


def test_dpi_awareness() -> None:
    assert win32.is_per_monitor_dpi_aware()


def test_window_details(hwnd: int) -> None:
    assert win32.is_window(hwnd)
    assert win32.window_title(hwnd) == TITLE
    assert win32.window_pid(hwnd) == os.getpid()
    assert win32.client_rect(hwnd) == GEOMETRY
    assert win32.window_dpi(hwnd) >= win32.BASE_DPI


def test_list_windows_finds_own_window(hwnd: int) -> None:
    found = [w for w in win32.list_windows() if w.hwnd == hwnd]
    assert len(found) == 1
    window = found[0]
    assert window.title == TITLE
    assert window.exe_name.lower().startswith("python")
    assert window.client_rect == GEOMETRY
    assert not window.minimized

    assert all(w.hwnd != hwnd for w in win32.list_windows(exclude_pid=os.getpid()))


def test_closed_window_has_no_rect(hwnd: int) -> None:
    _DestroyWindow(hwnd)
    assert not win32.is_window(hwnd)
    assert win32.client_rect(hwnd) is None


def test_click_through_toggles(hwnd: int) -> None:
    assert not win32.is_click_through(hwnd)
    win32.set_click_through(hwnd, True)
    assert win32.is_click_through(hwnd)
    win32.set_click_through(hwnd, False)
    assert not win32.is_click_through(hwnd)


def test_exclude_from_capture(hwnd: int) -> None:
    if not win32.set_exclude_from_capture(hwnd, True):
        pytest.skip("display affinity not supported in this session")
    assert win32.display_affinity(hwnd) == win32.WDA_EXCLUDEFROMCAPTURE
    assert win32.set_exclude_from_capture(hwnd, False)
    assert win32.display_affinity(hwnd) == 0


def test_monitors(hwnd: int) -> None:
    monitors = win32.list_monitors()
    assert monitors
    assert sum(m.is_primary for m in monitors) == 1
    assert all(m.device_name.startswith("\\\\.\\") and not m.rect.is_empty for m in monitors)

    monitor = win32.monitor_for_window(hwnd)
    assert monitor is not None
    assert monitor.device_name in {m.device_name for m in monitors}


def test_fullscreen_query_does_not_fail() -> None:
    assert isinstance(win32.is_exclusive_fullscreen_running(), bool)


def _window(exe: str, title: str, hwnd: int = 1, minimized: bool = False) -> win32.WindowInfo:
    return win32.WindowInfo(hwnd, title, 0, exe, Rect(0, 0, 1, 1), minimized)


def test_best_window_match() -> None:
    game_menu = _window("GTA5.exe", "Grand Theft Auto V", 1)
    game_other = _window("gta5.exe", "Grand Theft Auto V - 144 FPS", 2)
    browser = _window("chrome.exe", "Grand Theft Auto V", 3)

    match = win32.best_window_match
    assert match([game_other, game_menu, browser], "GTA5.exe", "Grand Theft Auto V") is game_menu
    assert match([game_other, browser], "GTA5.exe", "Grand Theft Auto V") is game_other
    assert match([browser], "GTA5.exe", "Grand Theft Auto V") is browser
    assert match([browser], "GTA5.exe", "Other") is None

    minimized = _window("GTA5.exe", "a", 4, minimized=True)
    restored = _window("GTA5.exe", "b", 5)
    assert match([minimized, restored], "GTA5.exe", "c") is restored
