"""Messages between the UI process and the engine process (sent through multiprocessing queues).

Plain frozen dataclasses: picklable, typed, and cheap. Commands go UI -> engine, events go
engine -> UI. Debug frames (calibration mode only) carry small images; nothing else carries pixels.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

import numpy as np

from livesubs.core.config import OllamaSettings
from livesubs.core.geometry import NormRect
from livesubs.core.profiles import Profile, VisionSettings

# --- Commands (UI -> engine) ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StartPipeline:
    profile: Profile
    ollama: OllamaSettings
    profile_id: str = ""


@dataclass(frozen=True, slots=True)
class StopPipeline:
    pass


@dataclass(frozen=True, slots=True)
class SetRegion:
    region: NormRect


@dataclass(frozen=True, slots=True)
class SetPaused:
    paused: bool


@dataclass(frozen=True, slots=True)
class SetDebug:
    """Calibration: send DebugFrame events (at most DEBUG_FPS) while enabled."""

    enabled: bool


@dataclass(frozen=True, slots=True)
class SetVision:
    """New preprocessing settings, applied from the next frame (calibration)."""

    vision: VisionSettings


@dataclass(frozen=True, slots=True)
class Shutdown:
    pass


Command = StartPipeline | StopPipeline | SetRegion | SetPaused | SetDebug | SetVision | Shutdown

# --- Events (engine -> UI) -----------------------------------------------------------------------


class EngineState(StrEnum):
    IDLE = "idle"
    STARTING = "starting"  # opening the source, loading OCR
    WARMING_UP = "warming_up"  # loading the translation model
    RUNNING = "running"
    PAUSED = "paused"  # source open, nothing read or translated
    STOPPED = "stopped"


@dataclass(frozen=True, slots=True)
class StateChanged:
    state: EngineState
    detail: str = ""


class ErrorKind(StrEnum):
    SOURCE_UNAVAILABLE = "source_unavailable"
    SOURCE_LOST = "source_lost"
    OCR_UNAVAILABLE = "ocr_unavailable"
    OLLAMA_UNAVAILABLE = "ollama_unavailable"
    MODEL_NOT_FOUND = "model_not_found"
    TRANSLATION_FAILED = "translation_failed"
    INTERNAL = "internal"


@dataclass(frozen=True, slots=True)
class EngineError:
    kind: ErrorKind
    message: str  # technical detail (English); the UI shows its own Turkish text per kind
    fatal: bool = False  # the pipeline stopped because of it


@dataclass(frozen=True, slots=True)
class SubtitleRead:
    """A confirmed (or upgraded) English line; its translation follows as ``SubtitleTranslated``."""

    line_id: int
    text: str
    ocr_ms: float
    repeat: bool = False  # same line as before: keep showing its translation
    upgrade: bool = False  # a more complete reading of line ``line_id``


@dataclass(frozen=True, slots=True)
class SubtitleTranslated:
    line_id: int
    source: str
    translation: str
    model: str
    lane: str
    ocr_ms: float
    translate_ms: float
    cached: bool


@dataclass(frozen=True, slots=True)
class SubtitleCleared:
    pass


@dataclass(frozen=True, slots=True)
class FrameInfo:
    """Sent once per opened source: what the frame looks like, for region selection."""

    width: int
    height: int
    screen_rect: tuple[int, int, int, int] | None  # x, y, w, h on the desktop; None for devices


DEBUG_FPS = 5
DEBUG_MAX_WIDTH = 720  # debug images are shrunk to this width before crossing processes


@dataclass(frozen=True, slots=True)
class DebugFrame:
    """What the pipeline sees, for the calibration Inspector (only while ``SetDebug`` is on)."""

    region: np.ndarray  # the subtitle region as captured, BGR, at most DEBUG_MAX_WIDTH wide
    ocr_input: np.ndarray  # after preprocessing, gray (black text on white)
    text_fraction: float  # share of the region that looks like subtitle text
    change: str  # the change detector's last verdict
    last_read: str  # the last OCR result, cleaned
    rejected: str  # why the filters dropped ``last_read``; "" when it passed


Event = (
    StateChanged
    | EngineError
    | SubtitleRead
    | SubtitleTranslated
    | SubtitleCleared
    | FrameInfo
    | DebugFrame
)
