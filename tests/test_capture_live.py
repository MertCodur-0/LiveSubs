"""Real captures against a flickering window owned by a helper process."""

import ctypes
import os
import platform
import subprocess
import sys
import time
from collections.abc import Iterator
from ctypes import wintypes
from pathlib import Path

import mss
import numpy as np
import pytest

from livesubs.core.profiles import MonitorSource, WindowSource
from livesubs.engine.capture import SourceLostError, SourceUnavailableError, open_source
from livesubs.engine.capture.base import BaseFrameSource
from livesubs.engine.capture.mss_fallback import MssSource
from livesubs.engine.capture.wgc import WgcMonitorSource, WgcWindowSource
from livesubs.platform import win32

pytestmark = pytest.mark.capture

HELPER = Path(__file__).parent / "helpers" / "flicker_window.py"
WIDTH, HEIGHT = 480, 270


class FlickerWindow:
    def __init__(self) -> None:
        self.title = f"LiveSubs capture test {os.getpid()}-{time.monotonic_ns()}"
        self.process = subprocess.Popen(
            [sys.executable, str(HELPER), self.title, "120", "120", str(WIDTH), str(HEIGHT)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        assert self.process.stdout is not None
        assert self.process.stdout.readline().strip() == "ready"
        self.window = next(w for w in win32.list_windows() if w.title == self.title)

    def close(self) -> None:
        if self.process.poll() is None:
            assert self.process.stdin is not None
            self.process.stdin.close()
            self.process.wait(timeout=5)


@pytest.fixture
def flicker() -> Iterator[FlickerWindow]:
    win32.enable_per_monitor_dpi_awareness()
    window = FlickerWindow()
    yield window
    window.close()


def _distinct_frames(source: BaseFrameSource, count: int) -> list[np.ndarray]:
    images, last = [], 0.0
    for _ in range(count):
        frame = source.wait_for_frame(last, timeout=3.0)
        assert frame is not None, "no new frame within 3 s"
        assert frame.timestamp > last
        assert frame.timestamp <= time.monotonic() + 0.01
        images.append(frame.image)
        last = frame.timestamp
    return images


needs_unlocked_screen = pytest.mark.skipif(
    win32.is_session_locked(), reason="screen grabs show the lock screen"
)


@pytest.mark.parametrize(
    "source_type",
    [WgcWindowSource, pytest.param(MssSource.for_window, marks=needs_unlocked_screen)],
)
def test_window_capture_delivers_client_area(flicker: FlickerWindow, source_type: type) -> None:
    client = flicker.window.client_rect
    with source_type(flicker.window.hwnd, max_fps=30) as source:
        images = _distinct_frames(source, 5)
        assert all(img.shape[:2] == (client.height, client.width) for img in images)
        assert source.screen_rect() == client
        # The helper flips colors, so frames must differ: proves they are not stale copies.
        assert len({img[client.height // 2, client.width // 2].tobytes() for img in images}) > 1


def test_wgc_window_close_is_reported(flicker: FlickerWindow) -> None:
    with WgcWindowSource(flicker.window.hwnd) as source:
        assert source.wait_for_frame(0.0, timeout=3.0) is not None
        flicker.close()
        with pytest.raises(SourceLostError):
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                source.wait_for_frame(time.monotonic(), timeout=0.1)


def test_platform_queries_between_sessions_do_not_crash(flicker: FlickerWindow) -> None:
    """Regression: a WMI query after a WGC session crashed the next session's start."""
    for _ in range(2):
        with WgcWindowSource(flicker.window.hwnd) as source:
            assert source.wait_for_frame(0.0, timeout=3.0) is not None
        platform._uname_cache = None  # force a fresh OS query, as on first use
        assert platform.system() == "Windows"
        with mss.MSS():  # calls platform.system() as well
            pass


def test_wgc_refuses_own_windows() -> None:
    user32 = ctypes.WinDLL("user32")
    user32.CreateWindowExW.restype = wintypes.HWND
    hwnd = user32.CreateWindowExW(0, "STATIC", "own", 0, 0, 0, 10, 10, None, None, None, None)
    try:
        with pytest.raises(SourceUnavailableError, match="this process"):
            WgcWindowSource(hwnd).open()
    finally:
        user32.DestroyWindow(wintypes.HWND(hwnd))


@pytest.mark.parametrize("source_type", [WgcMonitorSource, MssSource.for_monitor])
def test_monitor_capture_matches_monitor_size(source_type: type) -> None:
    monitor = next(m for m in win32.list_monitors() if m.is_primary)
    with source_type(monitor.device_name, max_fps=30) as source:
        frame = source.wait_for_frame(0.0, timeout=3.0)
        assert frame is not None
        assert (frame.width, frame.height) == (monitor.rect.width, monitor.rect.height)
        assert source.screen_rect() == monitor.rect


def test_open_source_from_profile_spec(flicker: FlickerWindow) -> None:
    spec = WindowSource(exe_name=flicker.window.exe_name, title=flicker.title)
    source = open_source(spec)
    try:
        assert isinstance(source, WgcWindowSource)
        assert source.hwnd == flicker.window.hwnd
    finally:
        source.close()

    monitor = open_source(MonitorSource(device_name=win32.list_monitors()[0].device_name))
    monitor.close()
