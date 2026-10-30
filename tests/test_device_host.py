"""The device host: the device thread with fake cards, and the real process with a real card."""

import os
import threading
import time
from collections.abc import Callable

import numpy as np
import pytest

from livesubs.core.geometry import Rect
from livesubs.core.profiles import SourceKind, VideoDeviceSource
from livesubs.device.host import DeviceHost, DeviceWorker
from livesubs.device.messages import (
    CloseDevice,
    DeviceClosed,
    DeviceErrorKind,
    DeviceFailed,
    DeviceOpened,
    OpenDevice,
    SignalChanged,
)
from livesubs.engine.capture.base import (
    BaseFrameSource,
    Frame,
    SourceBusyError,
    SourceInfo,
    SourceLostError,
    SourceUnavailableError,
)
from livesubs.engine.capture.shared_frames import SharedFrameReader
from livesubs.platform.video_devices import find_video_device

CARD = VideoDeviceSource(device_name="Test Card")


class FakeCard(BaseFrameSource):
    """60 fps of ``image(n)``; lost after ``lose_after`` frames when set."""

    def __init__(self, image: Callable[[int], np.ndarray], lose_after: int | None = None) -> None:
        super().__init__()
        self._image = image
        self._lose_after = lose_after
        self._stopped = threading.Event()
        self.closed = threading.Event()

    @property
    def info(self) -> SourceInfo:
        return SourceInfo(SourceKind.VIDEO_DEVICE, "Test Card", "fake", 60.0)

    def screen_rect(self) -> Rect | None:
        return None

    def _start(self) -> None:
        threading.Thread(target=self._play, daemon=True).start()

    def _stop(self) -> None:
        self._stopped.set()
        self.closed.set()

    def _play(self) -> None:
        n = 0
        while not self._stopped.is_set():
            n += 1
            if self._lose_after is not None and n > self._lose_after:
                self._slot.fail(SourceLostError("unplugged"))
                return
            self._slot.put(Frame(self._image(n), time.monotonic()))
            time.sleep(1 / 60)


def grey(n: int) -> np.ndarray:
    return np.full((9, 16, 3), 100 + n % 100, np.uint8)


class Events:
    def __init__(self) -> None:
        self.items: list = []
        self._cond = threading.Condition()

    def __call__(self, event: object) -> None:
        with self._cond:
            self.items.append(event)
            self._cond.notify_all()

    def wait(self, kind: type, count: int = 1, timeout: float = 5.0) -> list:
        deadline = time.monotonic() + timeout
        with self._cond:
            while len(self.of(kind)) < count:
                remaining = deadline - time.monotonic()
                assert remaining > 0, f"no {count} x {kind.__name__} in {self.items}"
                self._cond.wait(remaining)
            return self.of(kind)

    def of(self, kind: type) -> list:
        return [e for e in self.items if isinstance(e, kind)]


def _worker(opener, **kwargs) -> tuple[DeviceWorker, Events, list]:
    events, frames = Events(), []
    worker = DeviceWorker(events, frames.append, open_device=opener, **kwargs)
    worker.start()
    return worker, events, frames


def _opening(*sources: FakeCard | Exception):
    """An opener that hands out ``sources`` in turn (an exception is raised instead)."""
    queue = list(sources)

    def open_device(spec: VideoDeviceSource) -> FakeCard:
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, Exception):
            raise item
        item.open()
        return item

    return open_device


def test_frames_reach_shared_memory_and_the_preview() -> None:
    card = FakeCard(grey)
    worker, events, frames = _worker(_opening(card))
    try:
        worker.want(CARD)
        (opened,) = events.wait(DeviceOpened)
        assert (opened.device_name, opened.width, opened.height) == ("Test Card", 16, 9)
        reader = SharedFrameReader(opened.shared_frames)
        try:
            first = reader.latest_seq
            time.sleep(0.1)
            assert reader.latest_seq > first
            assert reader.read() is not None
        finally:
            reader.close()
        assert len(frames) > 3

        worker.want(None)  # released: other apps can open the card again
        events.wait(DeviceClosed)
        assert card.closed.is_set()
        assert frames[-1] is None  # the preview goes black
        with pytest.raises(SourceUnavailableError):
            SharedFrameReader(opened.shared_frames)  # gone with its last handle
    finally:
        worker.stop()


def test_busy_card_is_reported_once_and_retried() -> None:
    busy = SourceBusyError("held by OBS")
    worker, events, _ = _worker(_opening(busy, busy, busy, FakeCard(grey)), retry_s=0.02)
    try:
        worker.want(CARD)
        events.wait(DeviceOpened)
        failures = events.of(DeviceFailed)
        assert [f.kind for f in failures] == [DeviceErrorKind.BUSY]
    finally:
        worker.stop()


def test_lost_card_is_reopened_with_a_new_buffer() -> None:
    worker, events, _ = _worker(
        _opening(FakeCard(grey, lose_after=5), FakeCard(grey)), retry_s=0.02
    )
    try:
        worker.want(CARD)
        first, second = events.wait(DeviceOpened, count=2)
        assert first.shared_frames != second.shared_frames
        assert [f.kind for f in events.of(DeviceFailed)] == [DeviceErrorKind.LOST]
        assert len(events.of(DeviceClosed)) == 1
    finally:
        worker.stop()


def test_a_lasting_black_picture_is_reported() -> None:
    def black_then_picture(n: int) -> np.ndarray:
        return np.zeros((9, 16, 3), np.uint8) if n < 50 else grey(n)

    worker, events, _ = _worker(_opening(FakeCard(black_then_picture)), black_after_s=0.2)
    try:
        worker.want(CARD)
        events.wait(SignalChanged, count=2)
        assert [e.black for e in events.of(SignalChanged)] == [True, False]
    finally:
        worker.stop()


# --- the real process with a real card (set LIVESUBS_TEST_VIDEO_DEVICE to its name) ------------

REAL_DEVICE = os.environ.get("LIVESUBS_TEST_VIDEO_DEVICE", "")


def _wait_for(host: DeviceHost, kind: type, timeout: float) -> object:
    deadline = time.monotonic() + timeout
    while (remaining := deadline - time.monotonic()) > 0:
        event = host.wait_event(remaining)
        if isinstance(event, kind):
            return event
    pytest.fail(f"no {kind.__name__} within {timeout} s")


@pytest.mark.capture
@pytest.mark.skipif(not REAL_DEVICE, reason="LIVESUBS_TEST_VIDEO_DEVICE is not set")
def test_real_host_shares_the_card_at_full_rate() -> None:
    if find_video_device(REAL_DEVICE) is None:
        pytest.skip(f"{REAL_DEVICE} is not connected")
    host = DeviceHost()
    host.start()
    try:
        host.send(OpenDevice(VideoDeviceSource(device_name=REAL_DEVICE)))
        opened = _wait_for(host, DeviceOpened, 30)
        assert (opened.width, opened.height) == (1920, 1080)
        reader = SharedFrameReader(opened.shared_frames)
        try:
            first = reader.latest_seq
            time.sleep(1.0)
            assert reader.latest_seq - first >= 50  # ~60 fps
            _seq, frame = reader.read()
            assert time.monotonic() - frame.timestamp < 0.1
        finally:
            reader.close()
        host.send(CloseDevice())
        _wait_for(host, DeviceClosed, 5)
    finally:
        host.shutdown()
    assert not host.alive
