"""The device host: the one process that owns the capture card.

Windows lets only one app read a card at a time, and the preview must keep running while
translation is stopped or the engine restarts. So this process opens the card, writes every
frame to shared memory (the engine reads the newest one at its own pace) and shows the full
screen preview. It runs a Qt event loop for the preview; the card itself is handled on a device
thread: open (retried every ``RETRY_S`` while busy or missing), pump frames, report a black
picture, reopen after a loss.
"""

from __future__ import annotations

import logging
import os
import queue
import threading
from collections.abc import Callable
from multiprocessing.queues import Queue

from livesubs.core.child_process import ChildProcess, exit_with_parent
from livesubs.core.logs import setup_file_logging
from livesubs.core.messages import Shutdown
from livesubs.core.profiles import VideoDeviceSource
from livesubs.device.messages import (
    CloseDevice,
    DeviceClosed,
    DeviceCommand,
    DeviceErrorKind,
    DeviceEvent,
    DeviceFailed,
    DeviceOpened,
    OpenDevice,
    PreviewChanged,
    ShowPreview,
    SignalChanged,
)
from livesubs.engine.capture.base import (
    Frame,
    FrameSource,
    SourceBusyError,
    SourceLostError,
    SourceUnavailableError,
    is_black_frame,
)
from livesubs.engine.capture.shared_frames import SharedFrameWriter
from livesubs.engine.capture.video_device import VideoDeviceCapture

log = logging.getLogger(__name__)

RETRY_S = 2.0
BLACK_AFTER_S = 3.0  # fades to black are shorter; HDCP or a console in standby are not
BLACK_CHECK_S = 0.5
COMMAND_POLL_MS = 20

DeviceOpener = Callable[[VideoDeviceSource], FrameSource]


def open_capture_card(spec: VideoDeviceSource) -> FrameSource:
    source = VideoDeviceCapture(spec.device_name, spec.width, spec.height, spec.fps)
    source.open()
    return source


class DeviceWorker:
    """The device thread: keeps the wanted card open and its frames flowing.

    ``on_frame`` gets every frame (the preview); ``emit`` gets ``DeviceEvent``s.
    """

    def __init__(
        self,
        emit: Callable[[DeviceEvent], None],
        on_frame: Callable[[Frame | None], None],
        *,
        open_device: DeviceOpener = open_capture_card,
        retry_s: float = RETRY_S,
        black_after_s: float = BLACK_AFTER_S,
    ) -> None:
        self._emit = emit
        self._on_frame = on_frame
        self._open_device = open_device
        self._retry_s = retry_s
        self._black_after_s = black_after_s
        self._wanted: VideoDeviceSource | None = None
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="device", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        self._thread.join(timeout)

    def want(self, device: VideoDeviceSource | None) -> None:
        """The card to keep open (None: close it)."""
        self._wanted = device
        self._wake.set()

    # --- thread ----------------------------------------------------------------------------------

    def _run(self) -> None:
        last_failure: DeviceErrorKind | None = None
        while not self._stop.is_set():
            spec = self._wanted
            if spec is None:
                self._wake.wait()
                self._wake.clear()
                continue
            try:
                source = self._open_device(spec)
            except (SourceBusyError, SourceUnavailableError) as exc:
                kind = (
                    DeviceErrorKind.BUSY
                    if isinstance(exc, SourceBusyError)
                    else DeviceErrorKind.UNAVAILABLE
                )
                if kind != last_failure:
                    log.warning("Cannot open %s: %s", spec.device_name, exc)
                    self._emit(DeviceFailed(spec.device_name, kind, str(exc)))
                    last_failure = kind
                self._wake.wait(self._retry_s)
                self._wake.clear()
                continue
            last_failure = None
            lost = self._pump(spec, source)
            if lost is not None:
                log.warning("%s lost: %s", spec.device_name, lost)
                self._emit(DeviceFailed(spec.device_name, DeviceErrorKind.LOST, str(lost)))
                last_failure = DeviceErrorKind.LOST
                self._wake.wait(self._retry_s)
                self._wake.clear()

    def _pump(self, spec: VideoDeviceSource, source: FrameSource) -> SourceLostError | None:
        """Frames from ``source`` to shared memory and the preview until it is no longer wanted
        (returns None) or lost (returns the error)."""
        writer: SharedFrameWriter | None = None
        black_since: float | None = None
        black = False
        last_check = 0.0
        try:
            frame = source.latest_frame() or source.wait_for_frame(0.0, timeout=RETRY_S)
            if frame is None:
                raise SourceLostError(f"{spec.device_name} delivered no first frame")
            height, width = frame.image.shape[:2]
            writer = SharedFrameWriter(width, height, frame.image.shape[2])
            writer.write(frame.image, frame.timestamp)
            self._on_frame(frame)
            log.info("Opened %s: %dx%d -> %s", spec.device_name, width, height, writer.name)
            self._emit(DeviceOpened(spec.device_name, writer.name, width, height))
            while not self._stop.is_set() and self._wanted == spec:
                newer = source.wait_for_frame(frame.timestamp, timeout=0.2)
                if newer is None:
                    continue
                frame = newer
                if frame.image.shape[:2] != (height, width):
                    continue  # the requested mode is fixed; never seen, but never write it
                writer.write(frame.image, frame.timestamp)
                self._on_frame(frame)
                if frame.timestamp - last_check >= BLACK_CHECK_S:
                    last_check = frame.timestamp
                    if not is_black_frame(frame.image):
                        black_since = None
                        if black:
                            black = False
                            self._emit(SignalChanged(black=False))
                    elif black_since is None:
                        black_since = frame.timestamp
                    elif not black and frame.timestamp - black_since >= self._black_after_s:
                        black = True
                        self._emit(SignalChanged(black=True))
            return None
        except SourceLostError as exc:
            return exc
        finally:
            source.close()
            if writer is not None:
                writer.close()  # readers (the engine) see "closed" and stop
            self._on_frame(None)
            if black:
                self._emit(SignalChanged(black=False))
            self._emit(DeviceClosed(spec.device_name))


def device_main(commands: Queue, events: Queue) -> None:
    """Entry point of the device host process."""
    setup_file_logging("device.log")
    exit_with_parent()
    log.info("Device host started (pid %d)", os.getpid())

    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    from livesubs import APP_NAME
    from livesubs.device.preview import PreviewWindow
    from livesubs.platform import win32
    from livesubs.ui.icon import app_icon

    win32.set_app_user_model_id(f"{APP_NAME}.App")
    app = QApplication([])
    app.setQuitOnLastWindowClosed(False)
    app.setWindowIcon(app_icon())
    preview = PreviewWindow()
    preview.closed_by_user.connect(lambda: events.put(PreviewChanged(None, by_user=True)))
    worker = DeviceWorker(events.put, preview.show_frame)
    worker.start()

    def handle(command: DeviceCommand) -> None:
        log.info("Command: %s", command)
        match command:
            case OpenDevice(device=device):
                worker.want(device)
            case CloseDevice():
                worker.want(None)
            case ShowPreview(monitor=None):
                preview.hide_preview()
                events.put(PreviewChanged(None))
            case ShowPreview(monitor=monitor):
                shown = preview.show_on(monitor)
                events.put(PreviewChanged(monitor if shown else None))
            case Shutdown():
                worker.stop()
                app.quit()

    def poll() -> None:
        while True:
            try:
                command = commands.get_nowait()
            except queue.Empty:
                return
            try:
                handle(command)
            except Exception:
                log.exception("Device command failed: %r", command)

    timer = QTimer(interval=COMMAND_POLL_MS, timeout=poll)
    timer.start()
    app.exec()
    win32.keep_display_awake(False)
    log.info("Device host stopped")


class DeviceHost(ChildProcess[DeviceCommand, DeviceEvent]):
    """UI-side handle of the device host process (started on first use)."""

    def __init__(self) -> None:
        super().__init__(device_main, "livesubs-device")

    def ensure_started(self) -> None:
        if not self.started:
            self.start()
