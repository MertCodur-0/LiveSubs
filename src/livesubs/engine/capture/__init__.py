"""Frame sources: windows and monitors (WGC, mss fallback); video devices in phase 2."""

from livesubs.engine.capture.base import (
    CaptureError,
    Frame,
    FrameSource,
    SourceBusyError,
    SourceInfo,
    SourceKind,
    SourceLostError,
    SourceUnavailableError,
    is_black_frame,
)
from livesubs.engine.capture.factory import create_source, open_source

__all__ = [
    "CaptureError",
    "Frame",
    "FrameSource",
    "SourceBusyError",
    "SourceInfo",
    "SourceKind",
    "SourceLostError",
    "SourceUnavailableError",
    "create_source",
    "is_black_frame",
    "open_source",
]
