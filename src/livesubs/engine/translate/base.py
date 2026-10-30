"""Backend-independent translation types.

Any engine that can turn an English line into Turkish implements ``TranslationBackend``: Ollama
today; llama.cpp or a fine-tuned model of our own later.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol


class Lane(StrEnum):
    GPU = "gpu"  # fast, always on
    CPU = "cpu"  # optional quality pass that replaces the GPU result


@dataclass(frozen=True, slots=True)
class ContextLine:
    source: str  # English, as shown
    translation: str  # Turkish, as shown


@dataclass(frozen=True, slots=True)
class TranslationRequest:
    text: str  # the cleaned English line
    history: Sequence[ContextLine] = ()  # previous lines, oldest first, for the dialogue flow
    game_context: str = ""  # the profile's description of the game
    glossary: Mapping[str, str] = field(default_factory=dict)  # fixed renderings, names
    # The profile's example translations (English, Turkish); empty = the prompt's own.
    examples: Sequence[tuple[str, str]] = ()


@dataclass(frozen=True, slots=True)
class TranslationResult:
    text: str
    model: str
    lane: Lane
    elapsed_ms: float
    cached: bool = False
    prompt_tokens: int | None = None  # the whole prompt, as the backend counted it


class TranslationError(Exception):
    """The backend failed to translate (bad response, server error)."""


class BackendUnavailableError(TranslationError):
    """The backend cannot be reached (Ollama not running)."""


class ModelNotFoundError(TranslationError):
    """The configured model is not installed."""


class TranslationCancelledError(TranslationError):
    """The request was cancelled before it finished (a newer line replaced it)."""


class CancelToken:
    """Thread-safe cancellation: a flag checked between output chunks, plus callbacks that abort
    a blocking wait (e.g. close the HTTP stream while the model is still loading)."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._callbacks: list[Callable[[], object]] = []

    def cancel(self) -> None:
        with self._lock:
            if self._event.is_set():
                return
            self._event.set()
            callbacks, self._callbacks = self._callbacks, []
        for callback in callbacks:
            callback()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def on_cancel(self, callback: Callable[[], object]) -> None:
        """Run ``callback`` on cancellation (at once if already cancelled)."""
        with self._lock:
            if not self._event.is_set():
                self._callbacks.append(callback)
                return
        callback()


class TranslationBackend(Protocol):
    @property
    def model(self) -> str: ...

    @property
    def lane(self) -> Lane: ...

    def translate(self, request: TranslationRequest, cancel: CancelToken) -> TranslationResult:
        """Blocking; raises ``TranslationCancelledError`` soon after ``cancel`` is set."""
        ...
