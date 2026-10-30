import os
import threading
import time

import numpy as np
import pytest

from livesubs.core.profiles import SourceKind
from livesubs.engine.capture.base import SourceBusyError, SourceLostError, SourceUnavailableError
from livesubs.engine.capture.video_device import VideoDeviceCapture
from livesubs.platform.video_devices import VideoDevice, find_video_device, list_video_devices

CARD = VideoDevice(1, "Test Card", r"\\?\usb#test")


class FakeCapture:
    """Delivers ``frames`` frames, then fails (``None``: never stops)."""

    def __init__(self, frames: int | None, *, after: int = 0) -> None:
        self.frames = frames
        self.after = after  # failed reads before the first frame
        self.reads = 0
        self.released = threading.Event()

    def read(self) -> tuple[bool, np.ndarray | None]:
        self.reads += 1
        time.sleep(0.002)
        if self.reads <= self.after:
            return False, None
        if self.frames is not None and self.reads > self.after + self.frames:
            return False, None
        return True, np.full((4, 6, 3), self.reads % 256, np.uint8)

    def release(self) -> None:
        self.released.set()


def _source(capture: FakeCapture, **kwargs: object) -> VideoDeviceCapture:
    opened: list[tuple] = []

    def open_capture(index: int, width: int, height: int, fps: float) -> FakeCapture:
        opened.append((index, width, height, fps))
        return capture

    source = VideoDeviceCapture(
        "test card",
        open_capture=open_capture,
        find_device=lambda name: CARD if name.casefold() == "test card" else None,
        **kwargs,
    )
    source.opened_with = opened  # type: ignore[attr-defined]
    return source


def test_opens_by_name_with_the_default_mode() -> None:
    capture = FakeCapture(None)
    with _source(capture) as source:
        assert source.opened_with == [(1, 1920, 1080, 60.0)]  # type: ignore[attr-defined]
        assert source.info.kind is SourceKind.VIDEO_DEVICE
        assert source.screen_rect() is None
        first = source.latest_frame()
        assert first is not None
        newer = source.wait_for_frame(first.timestamp, timeout=1.0)
        assert newer is not None and newer.timestamp > first.timestamp
    assert capture.released.is_set()


def test_requested_mode_is_passed_on() -> None:
    with _source(FakeCapture(None), width=1280, height=720, fps=30) as source:
        assert source.opened_with == [(1, 1280, 720, 30)]  # type: ignore[attr-defined]


def test_frames_stay_valid_after_newer_ones() -> None:
    with _source(FakeCapture(None)) as source:
        first = source.latest_frame()
        assert first is not None
        value = int(first.image[0, 0, 0])
        source.wait_for_frame(first.timestamp, timeout=1.0)
        assert int(first.image[0, 0, 0]) == value


def test_slow_first_frame_is_waited_for() -> None:
    with _source(FakeCapture(None, after=5)) as source:
        assert source.latest_frame() is not None


def test_unknown_device_is_unavailable() -> None:
    source = VideoDeviceCapture("missing", find_device=lambda _name: None)
    with pytest.raises(SourceUnavailableError):
        source.open()


def test_device_without_frames_is_busy() -> None:
    capture = FakeCapture(0)
    source = _source(capture)
    source.first_frame_timeout_s = 0.1
    with pytest.raises(SourceBusyError):
        source.open()
    assert capture.released.is_set()


def test_device_that_stops_delivering_is_lost() -> None:
    source = _source(FakeCapture(3))
    source.lost_after_s = 0.1
    with source:
        deadline = time.monotonic() + 2.0
        with pytest.raises(SourceLostError):
            while time.monotonic() < deadline:
                source.wait_for_frame(time.monotonic(), timeout=0.05)
            pytest.fail("the source was never reported lost")


def test_listing_devices_never_fails() -> None:
    devices = list_video_devices()
    assert [d.index for d in devices] == list(range(len(devices)))
    assert find_video_device("no such device, surely") is None


# --- with a real capture card (set LIVESUBS_TEST_VIDEO_DEVICE to its name) ---------------------

REAL_DEVICE = os.environ.get("LIVESUBS_TEST_VIDEO_DEVICE", "")


@pytest.mark.capture
@pytest.mark.skipif(not REAL_DEVICE, reason="LIVESUBS_TEST_VIDEO_DEVICE is not set")
def test_real_device_delivers_full_hd_frames() -> None:
    if find_video_device(REAL_DEVICE) is None:
        pytest.skip(f"{REAL_DEVICE} is not connected")
    started = time.monotonic()
    with VideoDeviceCapture(REAL_DEVICE) as source:
        opened_s = time.monotonic() - started
        frame = source.latest_frame()
        assert frame is not None
        assert frame.image.shape == (1080, 1920, 3)
        count, last = 0, frame.timestamp
        end = time.monotonic() + 1.0
        while time.monotonic() < end:
            newer = source.wait_for_frame(last, timeout=0.5)
            assert newer is not None
            count, last = count + 1, newer.timestamp
    assert opened_s < 3.0  # ~26 s without OPENCV_VIDEOIO_MSMF_ENABLE_HW_TRANSFORMS=0
    assert count >= 50
