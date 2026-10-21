"""Turn a profile's source spec into an open ``FrameSource``."""

from __future__ import annotations

import logging
import os
from typing import Literal

from livesubs.core.profiles import MonitorSource, Source, VideoDeviceSource, WindowSource
from livesubs.engine.capture.base import FrameSource, SourceUnavailableError
from livesubs.engine.capture.mss_fallback import MssSource
from livesubs.engine.capture.wgc import DEFAULT_MAX_FPS, WgcMonitorSource, WgcWindowSource
from livesubs.platform import win32

log = logging.getLogger(__name__)

Backend = Literal["wgc", "mss"]


def find_window(spec: WindowSource) -> int:
    """HWND of the profile's window among other processes' windows."""
    windows = win32.list_windows(exclude_pid=os.getpid())
    match = win32.best_window_match(windows, spec.exe_name, spec.title)
    if match is None:
        raise SourceUnavailableError(f"no window for {spec.exe_name or spec.title!r}")
    return match.hwnd


def create_source(
    spec: Source, *, max_fps: float = DEFAULT_MAX_FPS, backend: Backend = "wgc"
) -> FrameSource:
    """An unopened source for ``spec``. Raises ``SourceUnavailableError``."""
    match spec:
        case WindowSource():
            hwnd = find_window(spec)
            if backend == "wgc":
                return WgcWindowSource(hwnd, max_fps)
            return MssSource.for_window(hwnd, max_fps)
        case MonitorSource(device_name=device):
            if backend == "wgc":
                return WgcMonitorSource(device, max_fps)
            return MssSource.for_monitor(device, max_fps)
        case VideoDeviceSource():
            raise SourceUnavailableError("video device sources are not supported yet")


def open_source(spec: Source, *, max_fps: float = DEFAULT_MAX_FPS) -> FrameSource:
    """Open ``spec`` with WGC, falling back to mss when Windows refuses WGC."""
    source = create_source(spec, max_fps=max_fps)
    try:
        source.open()
    except SourceUnavailableError as exc:
        log.warning("WGC capture unavailable (%s); falling back to mss", exc)
        source = create_source(spec, max_fps=max_fps, backend="mss")
        source.open()
    return source
