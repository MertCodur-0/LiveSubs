"""Per-game profiles (``profiles/*.json``): source, region, vision, overlay and translation."""

import logging
import re
import unicodedata
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal

from pydantic import AfterValidator, Field, StringConstraints

from livesubs.core import paths
from livesubs.core.geometry import NormRect
from livesubs.core.jsonio import SettingsModel, StorageError, read_model, write_model

log = logging.getLogger(__name__)

PROFILE_SCHEMA_VERSION = 1

# VRAM to leave free for the game. A game running on this PC needs its own VRAM; a streamed or
# captured one (Remote Play, capture card) only costs a video decoder.
GAME_VRAM_RESERVE_LOCAL_GB = 2.0
GAME_VRAM_RESERVE_REMOTE_GB = 0.5

# Executables of game-streaming clients: a window from one of these shows a game that runs on
# another device. A heuristic only; the profile can override it.
STREAMING_CLIENT_EXES = frozenset(
    {"remoteplay.exe", "chiaki.exe", "chiaki-ng.exe", "moonlight.exe", "parsecd.exe"}
)


class SourceKind(StrEnum):
    WINDOW = "window"
    MONITOR = "monitor"
    VIDEO_DEVICE = "video_device"


class WindowSource(SettingsModel):
    kind: Literal[SourceKind.WINDOW] = SourceKind.WINDOW
    # The executable is the stable identifier; titles often change (FPS counters, chapters).
    exe_name: str = ""
    title: str = ""


class MonitorSource(SettingsModel):
    kind: Literal[SourceKind.MONITOR] = SourceKind.MONITOR
    device_name: str  # e.g. r"\\.\DISPLAY1"


class VideoDeviceSource(SettingsModel):
    kind: Literal[SourceKind.VIDEO_DEVICE] = SourceKind.VIDEO_DEVICE
    device_name: str
    width: int | None = Field(None, gt=0)
    height: int | None = Field(None, gt=0)
    fps: float | None = Field(None, gt=0)


Source = Annotated[WindowSource | MonitorSource | VideoDeviceSource, Field(discriminator="kind")]

HexColor = Annotated[
    str,
    StringConstraints(pattern=r"^#[0-9A-Fa-f]{6}$"),
    AfterValidator(str.upper),
]


class CaptureSettings(SettingsModel):
    fps: int = Field(10, ge=1, le=60)


class VisionSettings(SettingsModel):
    """Preprocessing steps; each can be switched off while calibrating."""

    text_colors: list[HexColor] = Field(default_factory=lambda: ["#FFFFFF"], min_length=1)
    # Max per-channel distance from a subtitle color that still counts as text. 75 keeps glyphs
    # dimmed by ~25 % (fades, translucent overlays); 85+ starts letting scenery in.
    color_tolerance: int = Field(75, ge=0, le=255)
    upscale: bool = True
    color_mask: bool = True
    # Keep only thin structures that stand out from their surroundings (white top-hat of the
    # closeness to the subtitle color): rejects wide bright scenery, needs no dark outline/box.
    local_contrast: bool = True
    min_local_contrast: int = Field(60, ge=1, le=255)
    # Strokes thinner than this survive; raise it for very large or bold subtitle fonts.
    local_contrast_kernel_px: int = Field(15, ge=3, le=99)
    morphology: bool = True
    component_filter: bool = True
    # Component limits at native resolution: specks below, scenery blobs above.
    min_component_px: int = Field(4, ge=1)
    max_component_fraction: float = Field(0.02, gt=0, le=1)


class OverlayDisplay(StrEnum):
    AUTO = "auto"  # the screen showing the source; the primary monitor for video devices
    MONITOR = "monitor"
    WEB_ONLY = "web_only"


class OverlayStyle(StrEnum):
    CLASSIC = "classic"
    VICE = "vice"
    BOX = "box"


class OverlayPlacement(StrEnum):
    ABOVE = "above"  # right above the subtitle region
    OVER = "over"  # on the original subtitle (hidden from capture, so OCR still reads it)
    BELOW = "below"  # right below the subtitle region
    CUSTOM = "custom"  # where the user put it in calibration mode: ``rect`` on ``monitor_device``


class OverlaySettings(SettingsModel):
    display: OverlayDisplay = OverlayDisplay.AUTO
    placement: OverlayPlacement = OverlayPlacement.ABOVE
    monitor_device: str | None = None
    # CUSTOM placement (and video devices): normalized to ``monitor_device`` (else the primary).
    rect: NormRect = NormRect(x=0.1, y=0.62, width=0.8, height=0.14)
    style: OverlayStyle = OverlayStyle.CLASSIC
    font_family: str = "Segoe UI"
    font_size_pt: int = Field(26, ge=8, le=96)
    text_color: HexColor = "#FFFFFF"
    outline_color: HexColor = "#000000"
    outline_px: float = Field(3.0, ge=0, le=12)
    shadow: bool = True
    background: bool = True  # a dark box behind the text
    background_opacity: float = Field(0.55, ge=0, le=1)
    max_lines: int = Field(2, ge=1, le=6)
    show_original: bool = False
    # How long a translation stays up after the subtitle disappears.
    # User feedback: 0.8 s vanished before it was read, 3.5 s lingered too long; 2 s it is.
    clear_delay_ms: int = Field(2000, ge=0, le=10_000)


class TranslationSettings(SettingsModel):
    context: str = ""  # e.g. "Gritty crime story set in 1980s Miami."
    glossary: dict[str, str] = Field(default_factory=dict)


class VramSettings(SettingsModel):
    # None = infer from the source (see ``infer_game_on_this_pc``).
    game_on_this_pc: bool | None = None
    # None = the default for where the game runs.
    game_reserve_gb: float | None = Field(None, ge=0, le=64)


class Profile(SettingsModel):
    schema_version: int = PROFILE_SCHEMA_VERSION
    name: str = Field(min_length=1, max_length=80)
    source: Source | None = None
    region: NormRect | None = None
    capture: CaptureSettings = Field(default_factory=CaptureSettings)
    vision: VisionSettings = Field(default_factory=VisionSettings)
    overlay: OverlaySettings = Field(default_factory=OverlaySettings)
    translation: TranslationSettings = Field(default_factory=TranslationSettings)
    vram: VramSettings = Field(default_factory=VramSettings)

    def game_on_this_pc(self) -> bool:
        if self.vram.game_on_this_pc is not None:
            return self.vram.game_on_this_pc
        return infer_game_on_this_pc(self.source)

    def game_vram_reserve_gb(self) -> float:
        """VRAM to keep free for the game when judging whether a model fits."""
        if self.vram.game_reserve_gb is not None:
            return self.vram.game_reserve_gb
        if self.game_on_this_pc():
            return GAME_VRAM_RESERVE_LOCAL_GB
        return GAME_VRAM_RESERVE_REMOTE_GB


def infer_game_on_this_pc(source: WindowSource | MonitorSource | VideoDeviceSource | None) -> bool:
    match source:
        case VideoDeviceSource():
            return False
        case WindowSource(exe_name=exe):
            return exe.lower() not in STREAMING_CLIENT_EXES
        case _:
            return True


_TURKISH_ASCII = str.maketrans("ıİşŞğĞçÇöÖüÜ", "iIsSgGcCoOuU")


def slugify(name: str) -> str:
    """File-system-safe ASCII id for a profile name, e.g. "Vice City Gece" -> "vice-city-gece"."""
    ascii_name = (
        unicodedata.normalize("NFKD", name.translate(_TURKISH_ASCII))
        .encode("ascii", "ignore")
        .decode()
    )
    return re.sub(r"[^a-z0-9]+", "-", ascii_name.lower()).strip("-") or "profile"


class ProfileStore:
    """Profiles as one JSON file each; the file stem is the profile id."""

    def __init__(self, directory: Path | None = None) -> None:
        self.directory = directory or paths.profiles_dir()

    def _path(self, profile_id: str) -> Path:
        if not profile_id or slugify(profile_id) != profile_id:
            raise ValueError(f"invalid profile id {profile_id!r}")
        return self.directory / f"{profile_id}.json"

    def ids(self) -> list[str]:
        if not self.directory.is_dir():
            return []
        return sorted(p.stem for p in self.directory.glob("*.json"))

    def load(self, profile_id: str) -> Profile:
        """Raises ``FileNotFoundError`` or ``StorageError``."""
        return read_model(self._path(profile_id), Profile, PROFILE_SCHEMA_VERSION)

    def load_all(self) -> dict[str, Profile]:
        """Every usable profile by id; unusable files are logged and skipped."""
        profiles = {}
        for profile_id in self.ids():
            try:
                profiles[profile_id] = self.load(profile_id)
            except (StorageError, ValueError) as exc:
                log.warning("Skipping profile %s: %s", profile_id, exc)
        return profiles

    def new_id(self, name: str) -> str:
        """An unused id derived from ``name``."""
        base = slugify(name)
        existing = set(self.ids())
        candidate, n = base, 2
        while candidate in existing:
            candidate, n = f"{base}-{n}", n + 1
        return candidate

    def save(self, profile: Profile, profile_id: str | None = None) -> str:
        """Save under ``profile_id`` (a new id when omitted) and return the id used."""
        profile_id = profile_id or self.new_id(profile.name)
        write_model(self._path(profile_id), profile)
        return profile_id

    def delete(self, profile_id: str) -> None:
        self._path(profile_id).unlink(missing_ok=True)
