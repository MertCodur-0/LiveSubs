"""Where the overlay goes, in physical desktop pixels."""

from __future__ import annotations

from collections.abc import Sequence

from livesubs.core.geometry import NormRect, Rect
from livesubs.core.profiles import OverlayPlacement
from livesubs.platform.win32 import MonitorInfo

GAP_PX = 6


def monitor_at(monitors: Sequence[MonitorInfo], x: float, y: float) -> MonitorInfo:
    """The monitor containing a point, else the nearest one."""
    for monitor in monitors:
        r = monitor.rect
        if r.x <= x < r.right and r.y <= y < r.bottom:
            return monitor

    def distance(m: MonitorInfo) -> float:
        cx, cy = m.rect.x + m.rect.width / 2, m.rect.y + m.rect.height / 2
        return (cx - x) ** 2 + (cy - y) ** 2

    return min(monitors, key=distance)


def overlay_geometry(placement: OverlayPlacement, region: Rect, screen: Rect, height: int) -> Rect:
    """The overlay for a subtitle region: as wide as the region, ``height`` tall (taller for OVER
    when the region is), above / on / below it. Flips to the other side when there is no room on
    the monitor. CUSTOM is handled by ``custom_geometry``."""
    match placement:
        case OverlayPlacement.BELOW:
            y = region.bottom + GAP_PX
            if y + height > screen.bottom:
                y = region.y - height - GAP_PX
        case OverlayPlacement.OVER:
            height = max(height, region.height)
            y = region.y + (region.height - height) // 2
        case _:  # ABOVE
            y = region.y - height - GAP_PX
            if y < screen.y:
                y = region.bottom + GAP_PX
    return Rect(region.x, y, region.width, height)


def custom_geometry(rect: NormRect, monitor: Rect) -> Rect:
    return rect.to_pixels(monitor.width, monitor.height).translated(monitor.x, monitor.y)
