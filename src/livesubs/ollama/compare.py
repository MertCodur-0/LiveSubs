"""Translate the same lines with several models, one model at a time, and time them.

Models run one after the other, not side by side: two models loaded at once would need
``OLLAMA_MAX_LOADED_MODELS >= 2`` and, on an 8 GB card, would push each other out of VRAM and
distort the timings. Each model is warmed up first (its load time is reported apart), then every
line goes through the same prompt as live translation (game context, glossary, previous lines).
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from livesubs.core.config import OllamaSettings
from livesubs.engine.translate.base import (
    CancelToken,
    ContextLine,
    TranslationCancelledError,
    TranslationError,
    TranslationRequest,
    TranslationResult,
)
from livesubs.engine.translate.ollama import OllamaBackend
from livesubs.ollama import catalog
from livesubs.ollama.client import loaded_models
from livesubs.ollama.vram import gpu_share

# Game-style lines of our own (never from tests/samples, which are game footage): slang,
# swearing, names and places, as the prompt is tuned for.
TEST_LINES: tuple[str, ...] = (
    "Hey, you got a light? My lighter's busted.",
    "Relax, man. Nobody's gonna find the body out here.",
    "We hit the bank at noon. Ricky drives, you watch the door.",
    "Don't give me that crap, you owe me two grand!",
    "Meet me at the docks tonight, and come alone.",
    "This town's gone to hell since the Colombians moved in.",
)
WARM_UP_TEXT = "Hi."


class Backend(Protocol):
    def translate(self, request: TranslationRequest, cancel: CancelToken) -> TranslationResult: ...
    def unload(self) -> None: ...
    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class ModelRun:
    model: str
    warm_up_ms: float = 0.0
    gpu_share: float | None = None  # from /api/ps after loading; None = unknown
    error: str = ""


@dataclass(frozen=True, slots=True)
class LineResult:
    model: str
    index: int
    text: str
    elapsed_ms: float
    error: str = ""


def _backend(model: str, settings: OllamaSettings) -> OllamaBackend:
    return OllamaBackend(
        model,
        url=settings.url,
        num_ctx=settings.num_ctx,
        num_gpu=settings.gpu_num_gpu,
        keep_alive=settings.keep_alive,
    )


def _share(url: str, model: str) -> float | None:
    loaded = loaded_models(url) or []
    entry = next((m for m in loaded if catalog.same_model(m.name, model)), None)
    return gpu_share(entry.size_bytes, entry.vram_bytes) if entry else None


def run_comparison(
    models: Sequence[str],
    lines: Sequence[str],
    settings: OllamaSettings,
    *,
    on_model: Callable[[ModelRun], None],
    on_line: Callable[[LineResult], None],
    cancel: CancelToken,
    game_context: str = "",
    glossary: Mapping[str, str] | None = None,
    keep_loaded: str = "",
    backend_factory: Callable[[str, OllamaSettings], Backend] = _backend,
    share_of: Callable[[str, str], float | None] = _share,
) -> None:
    """Blocks until every model has translated every line, or ``cancel`` fires.

    Each model except ``keep_loaded`` (the one live translation uses) is unloaded afterwards, so
    the comparison leaves the memory as it found it.
    """
    glossary = dict(glossary or {})
    for model in models:
        if cancel.cancelled:
            return
        backend = backend_factory(model, settings)
        try:
            try:
                warm = backend.translate(TranslationRequest(WARM_UP_TEXT), cancel)
            except TranslationCancelledError:
                return
            except TranslationError as exc:
                on_model(ModelRun(model, error=str(exc)))
                continue
            on_model(ModelRun(model, warm.elapsed_ms, share_of(settings.url, model)))
            history: list[ContextLine] = []
            for index, text in enumerate(lines):
                request = TranslationRequest(
                    text,
                    history=tuple(history[-settings.context_lines :])
                    if settings.context_lines
                    else (),
                    game_context=game_context,
                    glossary=glossary,
                )
                try:
                    result = backend.translate(request, cancel)
                except TranslationCancelledError:
                    return
                except TranslationError as exc:
                    on_line(LineResult(model, index, "", 0.0, str(exc)))
                    continue
                history.append(ContextLine(text, result.text))
                on_line(LineResult(model, index, result.text, result.elapsed_ms))
        finally:
            if not catalog.same_model(model, keep_loaded):
                with contextlib.suppress(TranslationError):  # Ollama went away: nothing to free
                    backend.unload()
            backend.close()
