"""Against a running Ollama with the default GPU model installed (marked ``ollama``)."""

import httpx
import pytest

from livesubs.core.config import OllamaSettings
from livesubs.engine.translate.base import (
    CancelToken,
    ContextLine,
    ModelNotFoundError,
    TranslationCancelledError,
    TranslationRequest,
)
from livesubs.engine.translate.ollama import DEFAULT_URL, OllamaBackend

pytestmark = pytest.mark.ollama
MODEL = OllamaSettings().gpu_model


def installed_models() -> set[str]:
    try:
        tags = httpx.get(f"{DEFAULT_URL}/api/tags", timeout=3).json()
    except httpx.HTTPError:
        return set()
    return {m["name"] for m in tags.get("models", [])}


@pytest.fixture(scope="module")
def backend() -> OllamaBackend:
    models = installed_models()
    if not models:
        pytest.skip("Ollama is not running")
    if MODEL not in models:
        pytest.skip(f"{MODEL} is not installed")
    b = OllamaBackend(MODEL, keep_alive="5m")
    b.warm_up()
    return b


def test_translates_a_line_quickly(backend: OllamaBackend) -> None:
    request = TranslationRequest(
        "Get in the car.", history=[ContextLine("We're late.", "Geç kaldık.")]
    )
    result = backend.translate(request, CancelToken())
    assert result.text
    assert result.text != request.text  # actually translated
    assert "\n" not in result.text
    assert result.elapsed_ms < 3000  # warm model on the GPU


def test_cancelled_request_raises(backend: OllamaBackend) -> None:
    cancel = CancelToken()
    cancel.cancel()
    with pytest.raises(TranslationCancelledError):
        backend.translate(TranslationRequest("Get in the car."), cancel)


def test_unknown_model() -> None:
    if not installed_models():
        pytest.skip("Ollama is not running")
    missing = OllamaBackend("livesubs-no-such-model:1b")
    with pytest.raises(ModelNotFoundError):
        missing.translate(TranslationRequest("Hi."), CancelToken())
