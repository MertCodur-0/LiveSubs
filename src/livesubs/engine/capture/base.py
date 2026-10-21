"""Frame source abstraction shared by every capture backend.

A source delivers whole frames in its own *frame space*: a window's client area, a monitor, or a
video device's frame. Regions are normalized to that space. Consumers poll ``latest_frame()``:
the newest frame wins and stale frames are never queued, so a slow consumer never falls behind.
"""

from __future__ import annotations

import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Protocol, Self

import numpy as np

from livesubs.core.geometry import Rect
from livesubs.core.profiles import SourceKind

__all__ = [
    "BaseFrameSource",
    "CaptureError",
    "Frame",
    "FrameSource",
    "LatestFrameSlot",
    "SourceBusyError",
    "SourceInfo",
    "SourceKind",
    "SourceLostError",
    "SourceUnavailableError",
    "is_black_frame",
]


class CaptureError(Exception):
    """Base class for capture failures."""


class SourceUnavailableError(CaptureError):
    """The source cannot be opened: not found, not supported, or refused by Windows."""


class SourceBusyError(CaptureError):
    """The device is held by another application (e.g. OBS has the capture card open)."""


class SourceLostError(CaptureError):
    """The source went away while capturing (window closed, device unplugged)."""


@dataclass(frozen=True, slots=True)
class Frame:
    image: np.ndarray  # H x W x 3|4, uint8, BGR(A); shared, treat as read-only
    timestamp: float  # time.monotonic() seconds at which the frame was presented

    @property
    def width(self) -> int:
        return self.image.shape[1]

    @property
    def height(self) -> int:
        return self.image.shape[0]


@dataclass(frozen=True, slots=True)
class SourceInfo:
    kind: SourceKind
    name: str  # for the UI: window title, monitor or device name
    backend: str  # "wgc", "mss", ...
    max_fps: float | None  # delivery cap; None = as fast as the source produces


class FrameSource(Protocol):
    @property
    def info(self) -> SourceInfo: ...

    def open(self) -> None: ...

    def close(self) -> None: ...

    def latest_frame(self) -> Frame | None:
        """The newest frame (possibly one already seen), or None before the first one.

        Raises ``SourceLostError`` once the source is gone.
        """
        ...

    def wait_for_frame(self, newer_than: float, timeout: float) -> Frame | None:
        """Block until a frame newer than ``newer_than`` arrives; None on timeout."""
        ...

    def screen_rect(self) -> Rect | None:
        """Where the frame is on the desktop (physical pixels); None for video devices."""
        ...


class LatestFrameSlot:
    """Thread-safe single-frame mailbox: producers overwrite, consumers read or wait."""

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._frame: Frame | None = None
        self._error: CaptureError | None = None

    def put(self, frame: Frame) -> None:
        with self._cond:
            self._frame = frame
            self._cond.notify_all()

    def fail(self, error: CaptureError) -> None:
        with self._cond:
            self._error = error
            self._cond.notify_all()

    def reset(self) -> None:
        with self._cond:
            self._frame = None
            self._error = None

    def get(self) -> Frame | None:
        with self._cond:
            if self._error is not None:
                raise self._error
            return self._frame

    def wait_newer(self, newer_than: float, timeout: float) -> Frame | None:
        deadline = time.monotonic() + timeout
        with self._cond:
            while True:
                if self._error is not None:
                    raise self._error
                if self._frame is not None and self._frame.timestamp > newer_than:
                    return self._frame
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._cond.wait(remaining)


class BaseFrameSource(ABC):
    """Common plumbing: frame slot, idempotent open/close, context manager."""

    def __init__(self) -> None:
        self._slot = LatestFrameSlot()
        self._opened = False

    @property
    @abstractmethod
    def info(self) -> SourceInfo: ...

    @abstractmethod
    def _start(self) -> None:
        """Start producing frames into ``self._slot``; raise ``CaptureError`` on failure."""

    @abstractmethod
    def _stop(self) -> None:
        """Stop producing frames and release native resources."""

    @abstractmethod
    def screen_rect(self) -> Rect | None: ...

    def open(self) -> None:
        if self._opened:
            return
        self._slot.reset()
        self._start()
        self._opened = True

    def close(self) -> None:
        if not self._opened:
            return
        self._opened = False
        self._stop()

    def latest_frame(self) -> Frame | None:
        return self._slot.get()

    def wait_for_frame(self, newer_than: float, timeout: float) -> Frame | None:
        return self._slot.wait_newer(newer_than, timeout)

    def __enter__(self) -> Self:
        self.open()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def is_black_frame(image: np.ndarray, max_level: int = 8, step: int = 8) -> bool:
    """True for (nearly) all-black frames: protected content, HDCP, or a sleeping source.

    Looks at a sparse pixel grid only, so it costs microseconds even on 4K frames.
    """
    return int(image[::step, ::step, :3].max(initial=0)) <= max_level
