"""Windows Graphics Capture sources (window and monitor) via ``windows-capture``.

Frames arrive on the library's native thread, only when the content changes and at most every
``1 / max_fps`` seconds. They are stored without copying: each frame owns its buffer, which the
library never reuses (verified on 2.0.1; see CLAUDE.md decision log).

windows-capture 2.0.1 crashes (access violation) when a later session starts in two situations,
both reproduced and guarded against here:

- a window of the capturing process was captured before -> such windows are refused;
- a WMI query ran after a session -> ``platform`` is kept off WMI (``keep_platform_off_wmi``).
"""

from __future__ import annotations

import logging
import os
import platform
import time
from abc import abstractmethod

from windows_capture import CaptureControl, WindowsCapture

from livesubs.core.geometry import Rect
from livesubs.engine.capture.base import (
    BaseFrameSource,
    Frame,
    SourceInfo,
    SourceKind,
    SourceLostError,
    SourceUnavailableError,
)
from livesubs.platform import win32

log = logging.getLogger(__name__)

BACKEND = "wgc"
DEFAULT_MAX_FPS = 30.0
_MAX_CLOCK_SKEW_S = 1.0


def keep_platform_off_wmi() -> None:
    """Stop the stdlib ``platform`` module from querying WMI in this process.

    Since Python 3.12, ``platform.system()``, ``uname()``, ``win32_ver()`` etc. ask WMI (COM).
    A WMI query made after a WGC session makes the next session crash in
    ``start_free_threaded`` — and ``mss`` calls ``platform.system()`` on every construction.
    Without ``_wmi`` the module uses its pre-3.12 code paths, which return the same answers.
    Any other WMI user must stay out of the capture process too.
    """
    if getattr(platform, "_wmi", None) is not None:
        platform._wmi = None


def frame_timestamp(timespan: int) -> float:
    """Convert WGC's SystemRelativeTime (100 ns QPC ticks) to ``time.monotonic()`` seconds.

    On Python 3.13+ both use QueryPerformanceCounter, so this is the presentation time; anything
    implausible falls back to "now".
    """
    now = time.monotonic()
    timestamp = timespan / 1e7
    return timestamp if abs(now - timestamp) < _MAX_CLOCK_SKEW_S else now


def client_crop(
    frame_bounds: Rect, client: Rect, frame_width: int, frame_height: int
) -> Rect | None:
    """The part of a WGC window frame that shows the client area, in frame pixels.

    WGC captures the whole visible window (title bar and borders included); regions are relative
    to the client area. None when they do not overlap.
    """
    in_frame = client.translated(-frame_bounds.x, -frame_bounds.y)
    return in_frame.intersection(Rect(0, 0, frame_width, frame_height))


class _WgcSource(BaseFrameSource):
    def __init__(self, max_fps: float | None) -> None:
        super().__init__()
        self._max_fps = max_fps
        self._capture: WindowsCapture | None = None
        self._control: CaptureControl | None = None

    @abstractmethod
    def _target(self) -> dict[str, int]:
        """``WindowsCapture`` target arguments; raise ``SourceUnavailableError`` if impossible."""

    def _crop(self, frame_width: int, frame_height: int) -> Rect | None:
        """Sub-rectangle of each frame to deliver; None for the whole frame."""
        return None

    def _start(self) -> None:
        keep_platform_off_wmi()
        win32.enable_per_monitor_dpi_awareness()
        interval_ms = round(1000 / self._max_fps) if self._max_fps else None
        try:
            capture = WindowsCapture(
                cursor_capture=False,  # the cursor over a subtitle would end up in the OCR input
                draw_border=False,
                minimum_update_interval=interval_ms,
                **self._target(),
            )
            # Assigned directly: the library's @event decorator matches on function names.
            capture.frame_handler = self._on_frame_arrived
            capture.closed_handler = self._on_closed
            self._control = capture.start_free_threaded()
        except SourceUnavailableError:
            raise
        except Exception as exc:  # windows-capture only raises bare Exception
            raise SourceUnavailableError(f"{self.info.name}: {exc}") from exc
        self._capture = capture

    def _stop(self) -> None:
        if self._control is not None:
            self._control.stop()
            self._control.wait()
        self._control = None
        self._capture = None

    def _on_frame_arrived(self, frame: object, _control: object) -> None:
        # Runs on the native capture thread; an exception here would end the session silently.
        try:
            image = frame.frame_buffer
            crop = self._crop(frame.width, frame.height)
            if crop is not None:
                image = image[crop.y : crop.bottom, crop.x : crop.right]
            if image.size:
                self._slot.put(Frame(image, frame_timestamp(frame.timespan)))
        except Exception:
            log.exception("Dropping a frame from %s", self.info.name)

    def _on_closed(self) -> None:
        if self._opened:
            self._slot.fail(SourceLostError(f"{self.info.name} is no longer available"))


class WgcWindowSource(_WgcSource):
    """A window, cropped to its client area. Works while covered; not while minimized."""

    def __init__(self, hwnd: int, max_fps: float | None = DEFAULT_MAX_FPS) -> None:
        super().__init__(max_fps)
        self.hwnd = hwnd
        self._info = SourceInfo(SourceKind.WINDOW, win32.window_title(hwnd), BACKEND, max_fps)

    @property
    def info(self) -> SourceInfo:
        return self._info

    def _target(self) -> dict[str, int]:
        if not win32.is_window(self.hwnd):
            raise SourceUnavailableError(f"window {self.hwnd:#x} does not exist")
        if win32.window_pid(self.hwnd) == os.getpid():
            raise SourceUnavailableError("refusing to capture a window of this process")
        return {"window_hwnd": self.hwnd}

    def _crop(self, frame_width: int, frame_height: int) -> Rect | None:
        bounds = win32.window_frame_bounds(self.hwnd)
        client = win32.client_rect(self.hwnd)
        if bounds is None or client is None:
            return None
        return client_crop(bounds, client, frame_width, frame_height)

    def screen_rect(self) -> Rect | None:
        bounds = win32.window_frame_bounds(self.hwnd)
        client = win32.client_rect(self.hwnd)
        if bounds is None or client is None:
            return None
        return client.intersection(bounds)


class WgcMonitorSource(_WgcSource):
    def __init__(self, device_name: str, max_fps: float | None = DEFAULT_MAX_FPS) -> None:
        super().__init__(max_fps)
        self.device_name = device_name
        self._info = SourceInfo(SourceKind.MONITOR, device_name, BACKEND, max_fps)

    @property
    def info(self) -> SourceInfo:
        return self._info

    def _target(self) -> dict[str, int]:
        # windows-capture numbers monitors from 1 in EnumDisplayMonitors order, as
        # list_monitors() does (verified on a two-monitor machine).
        for index, monitor in enumerate(win32.list_monitors(), start=1):
            if monitor.device_name == self.device_name:
                return {"monitor_index": index}
        raise SourceUnavailableError(f"monitor {self.device_name} is not connected")

    def screen_rect(self) -> Rect | None:
        monitor = next(
            (m for m in win32.list_monitors() if m.device_name == self.device_name), None
        )
        return monitor.rect if monitor else None
