import threading
import time

import numpy as np
import pytest

from livesubs.core.geometry import Rect
from livesubs.core.profiles import MonitorSource, VideoDeviceSource, WindowSource
from livesubs.engine.capture import factory
from livesubs.engine.capture.base import (
    Frame,
    LatestFrameSlot,
    SourceLostError,
    SourceUnavailableError,
    is_black_frame,
)
from livesubs.engine.capture.mss_fallback import MssSource
from livesubs.engine.capture.shared_frames import SharedFrameSource, SharedFrameWriter
from livesubs.engine.capture.wgc import (
    WgcMonitorSource,
    WgcWindowSource,
    client_crop,
    frame_timestamp,
)
from livesubs.platform import win32


def _frame(timestamp: float) -> Frame:
    return Frame(np.zeros((2, 3, 4), np.uint8), timestamp)


def test_frame_size() -> None:
    frame = _frame(1.0)
    assert (frame.width, frame.height) == (3, 2)


def test_slot_newest_wins() -> None:
    slot = LatestFrameSlot()
    assert slot.get() is None
    first, second = _frame(1.0), _frame(2.0)
    slot.put(first)
    slot.put(second)
    assert slot.get() is second


def test_slot_wait_newer() -> None:
    slot = LatestFrameSlot()
    slot.put(_frame(1.0))
    assert slot.wait_newer(1.0, timeout=0.01) is None

    newer = _frame(2.0)
    threading.Timer(0.02, slot.put, (newer,)).start()
    assert slot.wait_newer(1.0, timeout=2.0) is newer


def test_slot_failure_wakes_waiters_and_sticks() -> None:
    slot = LatestFrameSlot()
    threading.Timer(0.02, slot.fail, (SourceLostError("gone"),)).start()
    with pytest.raises(SourceLostError):
        slot.wait_newer(0.0, timeout=2.0)
    with pytest.raises(SourceLostError):
        slot.get()
    slot.reset()
    assert slot.get() is None


def test_black_frame() -> None:
    black = np.zeros((1080, 1920, 4), np.uint8)
    black[..., 3] = 255  # alpha does not count
    assert is_black_frame(black)
    black[500:600, 500:600, :3] = 200
    assert not is_black_frame(black)


def test_client_crop_skips_title_bar() -> None:
    # Measured on Windows 11: an 800x500 overlapped window at (200, 200).
    bounds, client = Rect(207, 200, 786, 493), Rect(208, 231, 784, 461)
    assert client_crop(bounds, client, 786, 493) == Rect(1, 31, 784, 461)


def test_client_crop_clips_to_frame() -> None:
    # Maximized windows report a client area slightly larger than what is captured.
    bounds, client = Rect(0, 0, 1920, 1032), Rect(-8, -8, 1936, 1048)
    assert client_crop(bounds, client, 1920, 1032) == Rect(0, 0, 1920, 1032)
    assert client_crop(bounds, Rect(5000, 0, 10, 10), 1920, 1032) is None


def test_frame_timestamp_uses_monotonic_clock() -> None:
    now = time.monotonic()
    assert frame_timestamp(round(now * 1e7)) == pytest.approx(now, abs=1e-6)
    assert frame_timestamp(0) == pytest.approx(time.monotonic(), abs=0.1)  # implausible -> now


def _window(exe: str, title: str, hwnd: int) -> win32.WindowInfo:
    return win32.WindowInfo(hwnd, title, 1, exe, Rect(0, 0, 640, 360), False)


@pytest.fixture
def fake_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    windows = [_window("chrome.exe", "Browser", 0x10), _window("GTA5.exe", "GTA V", 0x20)]
    monkeypatch.setattr(win32, "list_windows", lambda exclude_pid=None: windows)
    monkeypatch.setattr(win32, "window_title", lambda hwnd: f"title {hwnd:#x}")


@pytest.mark.usefixtures("fake_windows")
def test_create_window_source() -> None:
    spec = WindowSource(exe_name="gta5.exe", title="old title")
    source = factory.create_source(spec)
    assert isinstance(source, WgcWindowSource)
    assert source.hwnd == 0x20
    assert isinstance(factory.create_source(spec, backend="mss"), MssSource)


@pytest.mark.usefixtures("fake_windows")
def test_missing_window_is_unavailable() -> None:
    with pytest.raises(SourceUnavailableError):
        factory.create_source(WindowSource(exe_name="missing.exe", title="Missing"))


def test_create_monitor_source() -> None:
    source = factory.create_source(MonitorSource(device_name=r"\\.\DISPLAY9"))
    assert isinstance(source, WgcMonitorSource)
    with pytest.raises(SourceUnavailableError):
        source.open()  # not connected


def test_video_device_needs_the_device_host() -> None:
    spec = VideoDeviceSource(device_name="USB Video")
    with pytest.raises(SourceUnavailableError):
        factory.create_source(spec)  # the card is open only in the device host
    with pytest.raises(SourceUnavailableError):
        factory.open_source(spec, shared_frames="livesubs-frames-missing")  # no mss fallback


def test_video_device_reads_shared_frames() -> None:
    writer = SharedFrameWriter(8, 4)
    try:
        writer.write(np.full((4, 8, 3), 9, np.uint8), time.monotonic())
        spec = VideoDeviceSource(device_name="USB Video", fps=60)
        source = factory.open_source(spec, shared_frames=writer.name)
        try:
            assert isinstance(source, SharedFrameSource)
            assert source.info.name == "USB Video"
            frame = source.latest_frame()
            assert frame is not None and int(frame.image[0, 0, 0]) == 9
        finally:
            source.close()
    finally:
        writer.close()
