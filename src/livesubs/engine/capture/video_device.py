"""Capture cards (and other video devices) through OpenCV's Media Foundation backend.

Frames are read on a thread of our own at the device's rate and stored newest-wins. Each
``read()`` returns a new array, so frames stay valid after newer ones arrive (same contract as
WGC frames). Timestamps are taken when ``read()`` returns.

Measured on a USB UVC card ("V-Z632"): 1920x1080 YUY2 at a steady 60 fps, open ~0.3 s (with
``OPENCV_VIDEOIO_MSMF_ENABLE_HW_TRANSFORMS=0``, set in ``livesubs/__init__``), ~30 % of one
core for the YUY2 -> BGR conversion. A card held by another app (OBS) opens without an error
but never delivers a frame: that is how ``SourceBusyError`` is detected. Windows does not
share the card between apps, so only one process of ours opens it (the device host).
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Protocol

import cv2
import numpy as np

from livesubs.core.geometry import Rect
from livesubs.engine.capture.base import (
    BaseFrameSource,
    Frame,
    SourceBusyError,
    SourceInfo,
    SourceKind,
    SourceLostError,
    SourceUnavailableError,
)
from livesubs.platform.video_devices import VideoDevice, find_video_device

log = logging.getLogger(__name__)

BACKEND = "msmf"
DEFAULT_WIDTH, DEFAULT_HEIGHT, DEFAULT_FPS = 1920, 1080, 60.0
# YUY2 is uncompressed: no MJPEG decoding, so less CPU and no decoder delay.
PIXEL_FORMAT = "YUY2"
FIRST_FRAME_TIMEOUT_S = 3.0
LOST_AFTER_S = 2.0
_RETRY_S = 0.01


class Capture(Protocol):
    """The part of ``cv2.VideoCapture`` used here (tests pass fakes)."""

    def read(self) -> tuple[bool, np.ndarray | None]: ...

    def release(self) -> None: ...


CaptureOpener = Callable[[int, int, int, float], Capture]
DeviceFinder = Callable[[str], VideoDevice | None]


def open_msmf(index: int, width: int, height: int, fps: float) -> Capture:
    """Open device ``index`` with Media Foundation and ask for the given mode."""
    capture = cv2.VideoCapture(
        index, cv2.CAP_MSMF, [cv2.CAP_PROP_HW_ACCELERATION, cv2.VIDEO_ACCELERATION_NONE]
    )
    if not capture.isOpened():
        raise SourceUnavailableError(f"Media Foundation cannot open video device {index}")
    capture.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*PIXEL_FORMAT))
    capture.set(cv2.CAP_PROP_FRAME_WIDTH, width)
    capture.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
    capture.set(cv2.CAP_PROP_FPS, fps)
    return capture


class VideoDeviceCapture(BaseFrameSource):
    """A video device found by its friendly name, read at its own frame rate."""

    first_frame_timeout_s = FIRST_FRAME_TIMEOUT_S
    lost_after_s = LOST_AFTER_S

    def __init__(
        self,
        device_name: str,
        width: int | None = None,
        height: int | None = None,
        fps: float | None = None,
        *,
        open_capture: CaptureOpener = open_msmf,
        find_device: DeviceFinder = find_video_device,
    ) -> None:
        super().__init__()
        self.device_name = device_name
        self._mode = (width or DEFAULT_WIDTH, height or DEFAULT_HEIGHT, fps or DEFAULT_FPS)
        self._open_capture = open_capture
        self._find_device = find_device
        self._info = SourceInfo(SourceKind.VIDEO_DEVICE, device_name, BACKEND, self._mode[2])
        self._capture: Capture | None = None
        self._thread: threading.Thread | None = None
        self._stopping = threading.Event()

    @property
    def info(self) -> SourceInfo:
        return self._info

    def screen_rect(self) -> Rect | None:
        return None

    def _start(self) -> None:
        device = self._find_device(self.device_name)
        if device is None:
            raise SourceUnavailableError(f"video device {self.device_name!r} is not connected")
        capture = self._open_capture(device.index, *self._mode)
        first = self._first_frame(capture)
        if first is None:
            capture.release()
            raise SourceBusyError(f"{self.device_name} delivers no frames (in use by another app?)")
        self._slot.put(Frame(first, time.monotonic()))
        self._capture = capture
        self._stopping.clear()
        self._thread = threading.Thread(
            target=self._run, args=(capture,), name="video-device", daemon=True
        )
        self._thread.start()

    def _first_frame(self, capture: Capture) -> np.ndarray | None:
        deadline = time.monotonic() + self.first_frame_timeout_s
        while time.monotonic() < deadline:
            ok, image = capture.read()
            if ok and image is not None:
                return image
            time.sleep(_RETRY_S)
        return None

    def _stop(self) -> None:
        self._stopping.set()
        if self._thread is not None:
            self._thread.join(2.0)
            self._thread = None
        if self._capture is not None:
            self._capture.release()
            self._capture = None

    def _run(self, capture: Capture) -> None:
        failing_since: float | None = None
        while not self._stopping.is_set():
            try:
                ok, image = capture.read()
            except cv2.error as exc:
                log.warning("Reading %s failed: %s", self.device_name, exc)
                ok, image = False, None
            now = time.monotonic()
            if ok and image is not None:
                failing_since = None
                self._slot.put(Frame(image, now))
                continue
            if failing_since is None:
                failing_since = now
            elif now - failing_since >= self.lost_after_s:
                self._slot.fail(SourceLostError(f"{self.device_name} stopped delivering frames"))
                return
            time.sleep(_RETRY_S)
