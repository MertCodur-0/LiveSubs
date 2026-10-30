"""UI side of the capture card: which card the device host keeps open, and its preview.

The card is wanted while a pipeline reads it, while a region is being picked on its picture,
or while the preview is on; otherwise it is released so other apps (OBS) can open it. The host
process is started on first need and replaced when it dies, then told again what is wanted.
"""

from __future__ import annotations

import logging
from typing import Protocol

import numpy as np
from PySide6.QtCore import QObject, QTimer, Signal

from livesubs.core.config import PreviewSettings
from livesubs.core.geometry import Rect
from livesubs.core.profiles import VideoDeviceSource
from livesubs.device.host import DeviceHost
from livesubs.device.messages import (
    CloseDevice,
    DeviceClosed,
    DeviceCommand,
    DeviceEvent,
    DeviceFailed,
    DeviceOpened,
    OpenDevice,
    PreviewChanged,
    ShowPreview,
    SignalChanged,
)
from livesubs.engine.capture.base import SourceUnavailableError
from livesubs.engine.capture.shared_frames import SharedFrameReader
from livesubs.platform import win32

log = logging.getLogger(__name__)

POLL_MS = 30
WATCH_MS = 1000


class Host(Protocol):
    @property
    def started(self) -> bool: ...
    @property
    def alive(self) -> bool: ...
    @property
    def pid(self) -> int | None: ...
    def start(self) -> None: ...
    def restart(self) -> None: ...
    def send(self, command: DeviceCommand) -> None: ...
    def poll(self, max_events: int = 100) -> list[DeviceEvent]: ...
    def shutdown(self, timeout: float = 5.0) -> None: ...


def preview_monitor(settings: PreviewSettings) -> win32.MonitorInfo | None:
    """The monitor the preview goes to: the chosen one, else the primary one."""
    monitors = win32.list_monitors()
    chosen = next((m for m in monitors if m.device_name == settings.monitor), None)
    return chosen or next((m for m in monitors if m.is_primary), None)


class CaptureCard(QObject):
    opened = Signal(str)  # shared frames name
    closed = Signal()
    failed = Signal(str, str)  # DeviceErrorKind (or "crashed"), card name
    signal_changed = Signal(bool)  # True: the picture is black (HDCP, console off)
    preview_changed = Signal(bool)  # True: closed by the user on the preview itself

    def __init__(self, settings: PreviewSettings, host: Host | None = None) -> None:
        super().__init__()
        self.settings = settings
        self._host = host or DeviceHost()
        self._for_pipeline: VideoDeviceSource | None = None
        self._for_snapshot: VideoDeviceSource | None = None
        self._profile_card = ""  # the active profile's card, when it has one
        self._sent_device: VideoDeviceSource | None = None
        self._sent_preview: str | None = None
        self.device_name = ""  # the card that is open, "" while none is
        self.shared_frames = ""
        self.preview_shown_on: str | None = None
        self._poll_timer = QTimer(self, interval=POLL_MS, timeout=self.poll)
        self._watch_timer = QTimer(self, interval=WATCH_MS, timeout=self._watch)

    # --- what is wanted ------------------------------------------------------------------------

    def set_pipeline_device(self, device: VideoDeviceSource | None) -> None:
        self._for_pipeline = device
        self.apply()

    def set_snapshot_device(self, device: VideoDeviceSource | None) -> None:
        self._for_snapshot = device
        self.apply()

    def set_profile_card(self, name: str) -> None:
        """The active profile's card: what the preview shows when no card is chosen for it."""
        self._profile_card = name
        self.apply()

    @property
    def preview_device(self) -> str:
        return self.settings.device or self._profile_card

    def wanted(self) -> VideoDeviceSource | None:
        if self._for_pipeline is not None:
            return self._for_pipeline
        if self._for_snapshot is not None:
            return self._for_snapshot
        if self.settings.enabled and self.preview_device:
            return VideoDeviceSource(device_name=self.preview_device)
        return None

    def apply(self) -> None:
        """Tell the host what is wanted now (only what changed)."""
        device = self.wanted()
        preview = None
        if self.settings.enabled and device is not None:
            monitor = preview_monitor(self.settings)
            preview = monitor.device_name if monitor else None
        if device == self._sent_device and preview == self._sent_preview:
            return
        if not self._host.started:
            if device is None and preview is None:
                return
            self._host.start()
            self._poll_timer.start()
            self._watch_timer.start()
        if device != self._sent_device:
            self._host.send(OpenDevice(device) if device is not None else CloseDevice())
            self._sent_device = device
        if preview != self._sent_preview:
            self._host.send(ShowPreview(preview))
            self._sent_preview = preview

    def is_open(self, device_name: str) -> bool:
        return bool(self.shared_frames) and self.device_name.casefold() == device_name.casefold()

    def preview_rect(self) -> Rect | None:
        """Where the card's picture is on the desktop (physical): the preview's monitor."""
        if self.preview_shown_on is None:
            return None
        monitor = next(
            (m for m in win32.list_monitors() if m.device_name == self.preview_shown_on), None
        )
        return monitor.rect if monitor else None

    def snapshot(self) -> np.ndarray | None:
        """A copy of the card's newest frame (BGR), or None while it is not open."""
        if not self.shared_frames:
            return None
        try:
            reader = SharedFrameReader(self.shared_frames)
        except SourceUnavailableError:
            return None
        try:
            result = reader.read()
            return result[1].image if result is not None else None
        finally:
            reader.close()

    @property
    def host_pid(self) -> int | None:
        return self._host.pid if self._host.started else None

    # --- host events ---------------------------------------------------------------------------

    def poll(self) -> None:
        for event in self._host.poll():
            self.handle(event)

    def handle(self, event: DeviceEvent) -> None:
        match event:
            case DeviceOpened(device_name=name, shared_frames=frames):
                self.device_name, self.shared_frames = name, frames
                self.opened.emit(frames)
            case DeviceClosed():
                self.device_name, self.shared_frames = "", ""
                self.closed.emit()
            case DeviceFailed(device_name=name, kind=kind):
                self.failed.emit(str(kind), name)
            case SignalChanged(black=black):
                self.signal_changed.emit(black)
            case PreviewChanged(monitor=monitor, by_user=by_user):
                self.preview_shown_on = monitor
                if by_user:
                    self.settings.enabled = False
                    self._sent_preview = None
                    self.apply()  # the card may no longer be wanted
                self.preview_changed.emit(by_user)

    def _watch(self) -> None:
        if not self._host.started or self._host.alive:
            return
        log.error("The device host died; starting a new one")
        self._host.restart()
        self.device_name, self.shared_frames, self.preview_shown_on = "", "", None
        self._sent_device, self._sent_preview = None, None
        self.closed.emit()
        self.failed.emit("crashed", self.preview_device)
        self.apply()

    def shutdown(self) -> None:
        self._poll_timer.stop()
        self._watch_timer.stop()
        if self._host.started:
            self._host.shutdown()
