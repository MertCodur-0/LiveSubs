"""GDI screen-grab fallback (``mss``) for systems where WGC is unavailable.

It copies screen pixels, so a window source only works while the window is unobscured, and it
polls on its own thread at ``max_fps``. Our own windows stay invisible to it as well
(``WDA_EXCLUDEFROMCAPTURE`` covers GDI capture).
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

import mss
import numpy as np

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

BACKEND = "mss"
DEFAULT_MAX_FPS = 10.0


class MssSource(BaseFrameSource):
    def __init__(
        self,
        kind: SourceKind,
        name: str,
        rect_provider: Callable[[], Rect | None],
        max_fps: float = DEFAULT_MAX_FPS,
    ) -> None:
        """``rect_provider`` returns the area to grab each time, or None once it is gone."""
        super().__init__()
        self._info = SourceInfo(kind, name, BACKEND, max_fps)
        self._rect_provider = rect_provider
        self._interval = 1.0 / max_fps
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    @classmethod
    def for_window(cls, hwnd: int, max_fps: float = DEFAULT_MAX_FPS) -> MssSource:
        def client_area() -> Rect | None:
            return win32.client_rect(hwnd) if win32.is_window(hwnd) else None

        return cls(SourceKind.WINDOW, win32.window_title(hwnd), client_area, max_fps)

    @classmethod
    def for_monitor(cls, device_name: str, max_fps: float = DEFAULT_MAX_FPS) -> MssSource:
        def monitor_area() -> Rect | None:
            monitor = next((m for m in win32.list_monitors() if m.device_name == device_name), None)
            return monitor.rect if monitor else None

        return cls(SourceKind.MONITOR, device_name, monitor_area, max_fps)

    @property
    def info(self) -> SourceInfo:
        return self._info

    def screen_rect(self) -> Rect | None:
        return self._rect_provider()

    def _start(self) -> None:
        win32.enable_per_monitor_dpi_awareness()
        if self._rect_provider() is None:
            raise SourceUnavailableError(f"{self._info.name} is not available")
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="mss-capture", daemon=True)
        self._thread.start()

    def _stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self._thread = None

    def _run(self) -> None:
        # mss handles are per thread, so the grabber lives entirely on this one.
        with mss.MSS() as grabber:
            while not self._stop_event.is_set():
                started = time.monotonic()
                rect = self._rect_provider()
                if rect is None:
                    self._slot.fail(SourceLostError(f"{self._info.name} is no longer available"))
                    return
                if not rect.is_empty:  # minimized windows have no area
                    try:
                        shot = grabber.grab(
                            {
                                "left": rect.x,
                                "top": rect.y,
                                "width": rect.width,
                                "height": rect.height,
                            }
                        )
                    except mss.ScreenShotError as exc:
                        self._slot.fail(SourceLostError(f"{self._info.name}: {exc}"))
                        return
                    # Stamped with the grab's start: the pixels are at least that old.
                    self._slot.put(Frame(np.asarray(shot), started))
                self._stop_event.wait(max(0.0, self._interval - (time.monotonic() - started)))
