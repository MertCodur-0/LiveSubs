"""Application-wide settings (``config.json``). Per-game settings live in profiles."""

import logging
from pathlib import Path

from pydantic import Field, field_validator

from livesubs.core import paths
from livesubs.core.jsonio import SettingsModel, StorageError, read_model, write_model

log = logging.getLogger(__name__)

CONFIG_SCHEMA_VERSION = 1
# "system" follows Windows' light/dark app mode (see ui.theme).
THEME_CHOICES = ("system", "dark", "light")


class OllamaSettings(SettingsModel):
    url: str = "http://127.0.0.1:11434"
    gpu_model: str = "gemma4:12b-it-qat"
    # Layers to offload to the GPU; None lets Ollama decide (all that fit).
    gpu_num_gpu: int | None = Field(None, ge=0)
    num_ctx: int = Field(2048, ge=512, le=32768)
    # Keep the GPU model resident between lines; reloading costs seconds.
    keep_alive: str = "30m"
    context_lines: int = Field(3, ge=0, le=10)


class CpuLaneSettings(SettingsModel):
    """Optional second pass on the CPU that replaces the GPU translation with a better one."""

    enabled: bool = False
    model: str = "qwen3:30b-a3b"
    # 0 keeps the model entirely on the CPU so it never competes with the GPU model.
    num_gpu: int = Field(0, ge=0)


class HotkeySettings(SettingsModel):
    calibration: str = "F8"
    toggle_overlay: str = "F9"
    pause: str = "F10"


class WebSettings(SettingsModel):
    enabled: bool = False
    port: int = Field(8765, ge=1024, le=65535)


class PreviewSettings(SettingsModel):
    """Full-screen picture of the capture card on a PC monitor (e.g. for a TV light strip app).

    App-wide, not per profile: it belongs to the PC's setup and runs while translation is off.
    """

    enabled: bool = False
    device: str = ""  # the card's name; "" = the card of the active profile
    monitor: str = ""  # display device name; "" = the primary monitor


class AppConfig(SettingsModel):
    schema_version: int = CONFIG_SCHEMA_VERSION
    last_profile: str | None = None
    ui_language: str = "tr"
    theme: str = "system"
    minimize_to_tray: bool = True
    ollama: OllamaSettings = Field(default_factory=OllamaSettings)
    cpu_lane: CpuLaneSettings = Field(default_factory=CpuLaneSettings)
    hotkeys: HotkeySettings = Field(default_factory=HotkeySettings)
    web: WebSettings = Field(default_factory=WebSettings)
    preview: PreviewSettings = Field(default_factory=PreviewSettings)
    # Lets OBS and screenshots see the overlay (for recordings); OCR may then read it back.
    recording_mode: bool = False
    # None = detect the GPU's VRAM; set it when detection is wrong.
    total_vram_gb: float | None = Field(None, gt=0, le=256)

    @field_validator("theme")
    @classmethod
    def _known_theme(cls, value: str) -> str:
        # Removed themes ("vice" before 0.5) and typos follow Windows instead of failing.
        return value if value in THEME_CHOICES else "system"


def load_config(path: Path | None = None) -> AppConfig:
    """Load settings, falling back to defaults when the file is missing or unusable.

    An unusable file is kept as ``config.json.bad`` so the user's settings are not silently lost.
    """
    path = path or paths.config_file()
    try:
        return read_model(path, AppConfig, CONFIG_SCHEMA_VERSION)
    except FileNotFoundError:
        return AppConfig()
    except StorageError as exc:
        log.warning("Ignoring settings file, using defaults: %s", exc)
        path.replace(path.with_name(path.name + ".bad"))
        return AppConfig()


def save_config(config: AppConfig, path: Path | None = None) -> None:
    write_model(path or paths.config_file(), config)
