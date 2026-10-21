"""From a rectangle drawn anywhere on the desktop to a source and a region in its frame.

The user marks the subtitle where they see it; the source is the window under the mark (the
front-most one containing its center), or the monitor when no window is there. The region is then
normalized to that source's frame: the window's visible client area, or the monitor.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from livesubs.core.geometry import NormRect, Rect
from livesubs.core.profiles import MonitorSource, Source, WindowSource
from livesubs.platform.win32 import MonitorInfo, WindowInfo

# Top-level windows that are part of the desktop itself, never a subtitle source.
_DESKTOP_TITLES = frozenset({"Program Manager"})


@dataclass(frozen=True, slots=True)
class Pick:
    source: Source
    area: Rect  # the source's frame on the desktop, physical pixels
    region: NormRect
    label: str  # window title or monitor name, for the UI


def _contains(rect: Rect, x: float, y: float) -> bool:
    return rect.x <= x < rect.right and rect.y <= y < rect.bottom


def resolve(
    selection: Rect,
    windows: Iterable[tuple[WindowInfo, Rect]],
    monitors: Iterable[MonitorInfo],
) -> Pick | None:
    """``selection``: physical pixels. ``windows``: front to back, each with its visible client
    area. None when the selection is on no monitor at all."""
    cx, cy = selection.x + selection.width / 2, selection.y + selection.height / 2
    for window, area in windows:
        if window.minimized or window.title in _DESKTOP_TITLES or not _contains(area, cx, cy):
            continue
        region = _normalize(selection, area)
        if region is not None:
            spec = WindowSource(exe_name=window.exe_name, title=window.title)
            return Pick(spec, area, region, window.title)
    for monitor in monitors:
        if _contains(monitor.rect, cx, cy):
            region = _normalize(selection, monitor.rect)
            if region is not None:
                name = monitor.device_name.removeprefix("\\\\.\\")
                return Pick(
                    MonitorSource(device_name=monitor.device_name), monitor.rect, region, name
                )
    return None


def _normalize(selection: Rect, area: Rect) -> NormRect | None:
    local = selection.translated(-area.x, -area.y)
    try:
        return NormRect.from_pixels(local, area.width, area.height)
    except ValueError:
        return None
