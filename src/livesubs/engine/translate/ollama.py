"""Ollama backend: ``/api/chat`` streaming.

Streaming is what makes cancellation cheap: the token stream is checked between chunks and
closing the connection makes Ollama stop generating, so the GPU is free for the newer line.
"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx

from livesubs.engine.translate.base import (
    BackendUnavailableError,
    CancelToken,
    Lane,
    ModelNotFoundError,
    TranslationCancelledError,
    TranslationError,
    TranslationRequest,
    TranslationResult,
)
from livesubs.engine.translate.prompt import build_messages, clean_output

DEFAULT_URL = "http://127.0.0.1:11434"
# Loading a model from disk can take a while before the first token arrives.
_TIMEOUT = httpx.Timeout(connect=2.0, read=120.0, write=10.0, pool=5.0)
# A subtitle translation is short; this only stops a model that rambles.
MAX_OUTPUT_TOKENS = 200


class OllamaBackend:
    def __init__(
        self,
        model: str,
        *,
        lane: Lane = Lane.GPU,
        url: str = DEFAULT_URL,
        num_ctx: int = 2048,
        num_gpu: int | None = None,
        keep_alive: str | int = "30m",
        temperature: float = 0.0,  # deterministic: the same line always reads the same
        client: httpx.Client | None = None,
    ) -> None:
        self._model = model
        self._lane = lane
        self.num_ctx = num_ctx
        self.num_gpu = num_gpu
        self.keep_alive = keep_alive
        self.temperature = temperature
        self._client = client or httpx.Client(base_url=url, timeout=_TIMEOUT)

    @property
    def model(self) -> str:
        return self._model

    @property
    def lane(self) -> Lane:
        return self._lane

    def _options(self) -> dict[str, Any]:
        options: dict[str, Any] = {
            "num_ctx": self.num_ctx,
            "num_predict": MAX_OUTPUT_TOKENS,
            "temperature": self.temperature,
        }
        if self.num_gpu is not None:
            options["num_gpu"] = self.num_gpu
        return options

    def translate(self, request: TranslationRequest, cancel: CancelToken) -> TranslationResult:
        started = time.perf_counter()
        body = {
            "model": self._model,
            "messages": build_messages(request),
            "stream": True,
            "think": False,  # Qwen3 and other reasoning models answer directly
            "keep_alive": self.keep_alive,
            "options": self._options(),
        }
        parts: list[str] = []
        prompt_tokens: int | None = None
        if cancel.cancelled:
            raise TranslationCancelledError(request.text)
        try:
            with self._client.stream("POST", "/api/chat", json=body) as response:
                # Closing the stream from the cancelling thread ends a wait for the first token.
                cancel.on_cancel(response.close)
                _raise_for_status(response, self._model)
                for line in response.iter_lines():
                    if cancel.cancelled:
                        raise TranslationCancelledError(request.text)
                    if not line:
                        continue
                    chunk = json.loads(line)
                    if "error" in chunk:
                        raise TranslationError(chunk["error"])
                    parts.append(chunk.get("message", {}).get("content", ""))
                    if chunk.get("done"):
                        # The whole prompt, also when Ollama reused a cached prefix of it.
                        prompt_tokens = chunk.get("prompt_eval_count")
                        break
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise BackendUnavailableError(f"Ollama is not reachable: {exc}") from exc
        except (httpx.HTTPError, httpx.StreamError) as exc:
            if cancel.cancelled:  # the stream was closed under us on purpose
                raise TranslationCancelledError(request.text) from exc
            raise TranslationError(f"Ollama request failed: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise TranslationError(f"unexpected Ollama response: {exc}") from exc

        if cancel.cancelled:
            raise TranslationCancelledError(request.text)
        text = clean_output("".join(parts))
        if not text:
            raise TranslationError("the model returned an empty translation")
        elapsed = (time.perf_counter() - started) * 1000
        return TranslationResult(
            text, self._model, self._lane, elapsed, prompt_tokens=prompt_tokens
        )

    def load(self) -> None:
        """Load the model into memory (empty prompt)."""
        self._generate({"model": self._model, "keep_alive": self.keep_alive})

    def warm_up(self) -> float:
        """Load the model and run one short translation; returns the time it took in ms.

        Loading alone is not enough: the first chat request after a load still took ~4 s
        (measured), the following ones 100-600 ms. Call this when translation starts so the
        first real subtitle is fast.
        """
        started = time.perf_counter()
        self.translate(TranslationRequest("Hi."), CancelToken())
        return (time.perf_counter() - started) * 1000

    def unload(self) -> None:
        """Free the model's memory at once (``keep_alive: 0``)."""
        self._generate({"model": self._model, "keep_alive": 0})

    def _generate(self, body: dict[str, Any]) -> None:
        try:
            response = self._client.post("/api/generate", json={**body, "stream": False})
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise BackendUnavailableError(f"Ollama is not reachable: {exc}") from exc
        except httpx.HTTPError as exc:
            raise TranslationError(f"Ollama request failed: {exc}") from exc
        _raise_for_status(response, self._model)

    def close(self) -> None:
        self._client.close()


def _raise_for_status(response: httpx.Response, model: str) -> None:
    if response.status_code < 400:
        return
    response.read()
    try:
        message = response.json().get("error", response.text)
    except ValueError:
        message = response.text
    if response.status_code == 404:
        raise ModelNotFoundError(f"model {model!r} is not installed ({message})")
    raise TranslationError(f"Ollama returned {response.status_code}: {message}")
