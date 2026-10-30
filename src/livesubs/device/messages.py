"""Messages between the UI process and the device host (through multiprocessing queues).

Like ``core.messages``: frozen dataclasses, no pixels (frames go through shared memory).
``core.messages.Shutdown`` ends the host too.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from livesubs.core.messages import Shutdown
from livesubs.core.profiles import VideoDeviceSource

# --- Commands (UI -> host) -----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OpenDevice:
    """Keep this card open (reopened by itself after a loss, retried while busy)."""

    device: VideoDeviceSource


@dataclass(frozen=True, slots=True)
class CloseDevice:
    """Release the card (other apps such as OBS can open it again)."""


@dataclass(frozen=True, slots=True)
class ShowPreview:
    """Show the card's picture full screen on ``monitor`` (a device name); None hides it."""

    monitor: str | None


DeviceCommand = OpenDevice | CloseDevice | ShowPreview | Shutdown

# --- Events (host -> UI) -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DeviceOpened:
    device_name: str
    shared_frames: str  # name of the shared frame buffer (new on every open)
    width: int
    height: int


@dataclass(frozen=True, slots=True)
class DeviceClosed:
    device_name: str


class DeviceErrorKind(StrEnum):
    UNAVAILABLE = "unavailable"  # not connected / cannot be opened
    BUSY = "busy"  # held by another app (OBS)
    LOST = "lost"  # stopped delivering frames (unplugged)


@dataclass(frozen=True, slots=True)
class DeviceFailed:
    """Sent once per failure kind; the host keeps retrying while the card is wanted."""

    device_name: str
    kind: DeviceErrorKind
    message: str  # technical detail (English); the UI shows Turkish text per kind


@dataclass(frozen=True, slots=True)
class SignalChanged:
    """The picture has been black for a while (HDCP, console off) or is back."""

    black: bool


@dataclass(frozen=True, slots=True)
class PreviewChanged:
    monitor: str | None  # where it is shown; None = hidden
    by_user: bool = False  # closed from the preview window itself (Esc, double-click)


DeviceEvent = DeviceOpened | DeviceClosed | DeviceFailed | SignalChanged | PreviewChanged
