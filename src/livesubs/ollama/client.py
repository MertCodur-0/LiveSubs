"""Ollama client for the UI: is it installed and running, which models it has and which are
loaded; start it, pull, inspect, copy and delete models.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

DEFAULT_URL = "http://127.0.0.1:11434"


@dataclass(frozen=True, slots=True)
class InstalledModel:
    name: str
    size_bytes: int
    parameter_size: str
    quantization: str
    digest: str = ""


@dataclass(frozen=True, slots=True)
class LoadedModel:
    name: str
    size_bytes: int  # all of the loaded model
    vram_bytes: int  # the part on the GPU


class OllamaError(Exception):
    """Ollama refused a request; the message is Ollama's own."""


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
                digest=entry.get("digest", ""),
            )
        )
    return sorted(models, key=lambda m: m.name)


def loaded_models(url: str = DEFAULT_URL, timeout: float = 3.0) -> list[LoadedModel] | None:
    """Models in memory right now (``/api/ps``), or None when Ollama does not answer."""
    try:
        response = httpx.get(f"{url}/api/ps", timeout=timeout)
        response.raise_for_status()
    except httpx.HTTPError:
        return None
    return [
        LoadedModel(entry["name"], entry.get("size", 0), entry.get("size_vram", 0))
        for entry in response.json().get("models", [])
    ]


def model_info(name: str, url: str = DEFAULT_URL, timeout: float = 10.0) -> dict[str, Any]:
    """``/api/show``'s ``model_info``: architecture, layer and attention sizes, experts."""
    try:
        response = httpx.post(f"{url}/api/show", json={"model": name}, timeout=timeout)
    except httpx.HTTPError as exc:
        raise OllamaError(str(exc) or type(exc).__name__) from exc
    if response.status_code != 200:
        raise OllamaError(_error_text(response.text) or f"HTTP {response.status_code}")
    return response.json().get("model_info", {})


def delete_model(name: str, url: str = DEFAULT_URL, timeout: float = 30.0) -> None:
    try:
        response = httpx.request(
            "DELETE", f"{url}/api/delete", json={"model": name}, timeout=timeout
        )
    except httpx.HTTPError as exc:
        raise OllamaError(str(exc) or type(exc).__name__) from exc
    if response.status_code != 200:
        raise OllamaError(_error_text(response.text) or f"HTTP {response.status_code}")


MODEL_LAYER = "application/vnd.ollama.image.model"


def create_text_only(name: str, new_name: str, url: str = DEFAULT_URL) -> None:
    """Make ``new_name``: ``name`` without its vision encoder (projector).

    The copy points at the same weights file (Ollama copies nothing) and keeps the chat format
    and parameters. The unused encoder held ~1.2 GB of VRAM for gemma4:26b; without it Ollama
    puts more experts on the GPU and a line translates ~12 % faster (measured, see CLAUDE.md).
    """
    digest = _layer_digest(name, MODEL_LAYER)
    try:
        show = httpx.post(f"{url}/api/show", json={"model": name}, timeout=30.0)
        if show.status_code != 200:
            raise OllamaError(_error_text(show.text) or f"HTTP {show.status_code}")
        info = show.json()
        request: dict[str, Any] = {
            "model": new_name,
            "files": {"model.gguf": digest},
            "parameters": parse_parameters(info.get("parameters", "")),
            "stream": False,
        }
        for key in ("template", "renderer", "parser"):
            if info.get(key):
                request[key] = info[key]
        response = httpx.post(f"{url}/api/create", json=request, timeout=120.0)
    except httpx.HTTPError as exc:
        raise OllamaError(str(exc) or type(exc).__name__) from exc
    if response.status_code != 200:
        raise OllamaError(_error_text(response.text) or f"HTTP {response.status_code}")


def parse_parameters(text: str) -> dict[str, Any]:
    """``/api/show``'s parameter lines ("top_k   64") as ``/api/create`` takes them."""
    parameters: dict[str, Any] = {}
    for line in text.splitlines():
        key, _, raw = line.strip().partition(" ")
        if not key:
            continue
        raw = raw.strip()
        try:
            value: Any = json.loads(raw)
        except ValueError:
            value = raw
        if key in parameters:  # repeated keys ("stop") form a list
            previous = parameters[key]
            parameters[key] = (
                [*previous, value] if isinstance(previous, list) else [previous, value]
            )
        else:
            parameters[key] = value
    return parameters


def _layer_digest(name: str, media_type: str) -> str:
    """The digest of a model's layer, read from its manifest in Ollama's model folder."""
    model, _, tag = name.partition(":")
    path = models_dir() / "manifests" / "registry.ollama.ai"
    path = path / (model if "/" in model else f"library/{model}") / (tag or "latest")
    try:
        layers = json.loads(path.read_text(encoding="utf-8")).get("layers", [])
    except (OSError, ValueError) as exc:
        raise OllamaError(f"manifest of {name} not readable: {exc}") from exc
    digest = next((layer["digest"] for layer in layers if layer.get("mediaType") == media_type), "")
    if not digest:
        raise OllamaError(f"{name} has no {media_type.rsplit('.', 1)[-1]} layer")
    return digest


def _install_dir() -> Path | None:
    local = os.environ.get("LOCALAPPDATA")
    return Path(local) / "Programs" / "Ollama" if local else None


def ollama_app_path() -> Path | None:
    """The tray app installed by the Ollama installer (it starts the server)."""
    folder = _install_dir()
    path = folder / "ollama app.exe" if folder else None
    return path if path is not None and path.is_file() else None


def is_installed() -> bool:
    return ollama_app_path() is not None


def models_dir() -> Path:
    """Where Ollama stores model files (``OLLAMA_MODELS`` moves them)."""
    if custom := os.environ.get("OLLAMA_MODELS"):
        return Path(custom)
    return Path.home() / ".ollama" / "models"


def start_ollama() -> bool:
    """Launch Ollama detached from LiveSubs; False when it is not installed."""
    app = ollama_app_path()
    if app is None:
        return False
    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen([str(app)], creationflags=flags, close_fds=True)
    return True


def wait_until_running(url: str = DEFAULT_URL, timeout: float = 90.0) -> bool:
    """Wait for a starting server. Its GPU discovery alone took ~40 s once on the dev machine."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if list_models(url, timeout=2.0) is not None:
            return True
        time.sleep(1.0)
    return False


class PullError(Exception):
    """The download failed (unknown model name, network, disk full); the message says why."""


class PullCancelledError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class PullProgress:
    status: str  # Ollama's status text, e.g. "pulling manifest", "verifying sha256 digest"
    completed: int  # bytes, summed over the model's files
    total: int  # 0 until the sizes are known


def pull_model(
    name: str,
    on_progress: Callable[[PullProgress], None],
    cancel: threading.Event,
    url: str = DEFAULT_URL,
) -> None:
    """Download ``name`` through ``/api/pull``. Blocks; ``cancel`` stops it between updates.

    Ollama keeps partial downloads, so pulling the same model again resumes it.
    """
    files: dict[str, tuple[int, int]] = {}  # digest -> (completed, total)
    try:
        with httpx.stream(
            "POST",
            f"{url}/api/pull",
            json={"model": name, "stream": True},
            timeout=httpx.Timeout(10.0, read=120.0),
        ) as response:
            if response.status_code != 200:
                response.read()
                raise PullError(_error_text(response.text) or f"HTTP {response.status_code}")
            for line in response.iter_lines():
                if cancel.is_set():
                    raise PullCancelledError
                if not line.strip():
                    continue
                update = json.loads(line)
                if "error" in update:
                    raise PullError(update["error"])
                if digest := update.get("digest"):
                    files[digest] = (update.get("completed", 0), update.get("total", 0))
                completed = sum(done for done, _ in files.values())
                total = sum(size for _, size in files.values())
                on_progress(PullProgress(update.get("status", ""), completed, total))
                if update.get("status") == "success":
                    return
    except httpx.HTTPError as exc:
        raise PullError(str(exc) or type(exc).__name__) from exc
    raise PullError("download ended without success")


def _error_text(body: str) -> str:
    try:
        return json.loads(body).get("error", "")
    except (ValueError, AttributeError):
        return body.strip()
