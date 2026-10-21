"""Minimal Ollama client for the UI: is it running, which models are installed, start it.

The full model manager (pull, delete, VRAM estimates) comes with the Models tab.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

import httpx

DEFAULT_URL = "http://127.0.0.1:11434"


@dataclass(frozen=True, slots=True)
class InstalledModel:
    name: str
    size_bytes: int
    parameter_size: str
    quantization: str


def list_models(url: str = DEFAULT_URL, timeout: float = 3.0) -> list[InstalledModel] | None:
    """Installed models, or None when Ollama does not answer."""
    try:
        response = httpx.get(f"{url}/api/tags", timeout=timeout)
        response.raise_for_status()
    except httpx.HTTPError:
        return None
    models = []
    for entry in response.json().get("models", []):
        details = entry.get("details", {})
        models.append(
            InstalledModel(
                name=entry["name"],
                size_bytes=entry.get("size", 0),
                parameter_size=details.get("parameter_size", ""),
                quantization=details.get("quantization_level", ""),
            )
        )
    return sorted(models, key=lambda m: m.name)


def ollama_app_path() -> Path | None:
    """The tray app installed by the Ollama installer (it starts the server)."""
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        return None
    path = Path(local) / "Programs" / "Ollama" / "ollama app.exe"
    return path if path.is_file() else None


def start_ollama() -> bool:
    """Launch Ollama detached from LiveSubs; False when it is not installed."""
    app = ollama_app_path()
    if app is None:
        return False
    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen([str(app)], creationflags=flags, close_fds=True)
    return True
