import json
import threading
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import respx

from livesubs.engine.translate.base import (
    BackendUnavailableError,
    CancelToken,
    ContextLine,
    Lane,
    ModelNotFoundError,
    TranslationCancelledError,
    TranslationError,
    TranslationRequest,
    TranslationResult,
)
from livesubs.engine.translate.cache import TranslationCache
from livesubs.engine.translate.ollama import OllamaBackend
from livesubs.engine.translate.orchestrator import (
    LineFailed,
    LineTranslated,
    TranslationEvent,
    TranslationOrchestrator,
)
from livesubs.engine.translate.prompt import EXAMPLES, SYSTEM_PROMPT, build_messages, clean_output

URL = "http://ollama.test"

# --- prompt --------------------------------------------------------------------------------------


def test_messages_carry_history_as_turns() -> None:
    request = TranslationRequest(
        text="Get in the car.",
        history=[ContextLine("Yo.", "Selam."), ContextLine("We're late.", "Geç kaldık.")],
        game_context="Crime story in 1980s Miami.",
        glossary={"Vice City": "Vice City", "cop": "aynasız"},
    )
    messages = build_messages(request)
    examples = 2 * len(EXAMPLES)
    turns = len(EXAMPLES) + len(request.history)
    assert [m["role"] for m in messages] == ["system", *["user", "assistant"] * turns, "user"]
    assert messages[0]["content"].startswith(SYSTEM_PROMPT)
    assert "1980s Miami" in messages[0]["content"]
    assert "- cop -> aynasız" in messages[0]["content"]
    # Fixed examples first, then the dialogue so far, then the line to translate.
    assert messages[1:3] == [
        {"role": "user", "content": EXAMPLES[0][0]},
        {"role": "assistant", "content": EXAMPLES[0][1]},
    ]
    assert messages[1 + examples : 5 + examples] == [
        {"role": "user", "content": "Yo."},
        {"role": "assistant", "content": "Selam."},
        {"role": "user", "content": "We're late."},
        {"role": "assistant", "content": "Geç kaldık."},
    ]
    assert messages[-1] == {"role": "user", "content": "Get in the car."}


def test_minimal_messages() -> None:
    messages = build_messages(TranslationRequest("Hi."))
    assert messages[0] == {"role": "system", "content": SYSTEM_PROMPT}
    assert [m["role"] for m in messages[1:-1]] == ["user", "assistant"] * len(EXAMPLES)
    assert messages[-1] == {"role": "user", "content": "Hi."}


def test_profile_examples_replace_the_built_in_ones() -> None:
    examples = [("Yo, what the fuck?", "Lan, bu ne?")]
    messages = build_messages(TranslationRequest("Hi.", examples=examples))
    assert messages[1:-1] == [
        {"role": "user", "content": "Yo, what the fuck?"},
        {"role": "assistant", "content": "Lan, bu ne?"},
    ]


def test_examples_are_not_test_samples() -> None:
    """The few-shot examples must not leak the evaluation lines in tests/samples."""
    from sample_data import load_samples

    sample_lines = {s.expected for s in load_samples()}
    assert not sample_lines & {source for source, _ in EXAMPLES}


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        ("Arabaya bin.", "Arabaya bin."),
        ('"Arabaya bin."', "Arabaya bin."),
        ("Türkçe: Arabaya bin.", "Arabaya bin."),
        ("<think>\nshort line\n</think>\n\nArabaya bin.", "Arabaya bin."),
        ("Arabaya\nbin.", "Arabaya bin."),
        ("  'Hey kanka'  ", "Hey kanka"),
        ("Bin şu arabaya, 'hemen'", "Bin şu arabaya, 'hemen'"),  # inner quotes stay
    ],
)
def test_clean_output(raw: str, clean: str) -> None:
    assert clean_output(raw) == clean


# --- Ollama backend ------------------------------------------------------------------------------


def ndjson(*contents: str, done_extra: dict | None = None) -> bytes:
    lines = [{"message": {"role": "assistant", "content": c}, "done": False} for c in contents]
    lines.append(
        {"message": {"role": "assistant", "content": ""}, "done": True, **(done_extra or {})}
    )
    return b"\n".join(json.dumps(line).encode() for line in lines) + b"\n"


@pytest.fixture
def backend() -> Iterator[OllamaBackend]:
    b = OllamaBackend("qwen3:8b", url=URL, num_ctx=2048, num_gpu=99, keep_alive="30m")
    yield b
    b.close()


@respx.mock
def test_streamed_translation(backend: OllamaBackend) -> None:
    route = respx.post(f"{URL}/api/chat").mock(
        return_value=httpx.Response(200, content=ndjson("Arabaya", " bin", "."))
    )
    result = backend.translate(TranslationRequest("Get in the car."), CancelToken())
    assert result.text == "Arabaya bin."
    assert (result.model, result.lane, result.cached) == ("qwen3:8b", Lane.GPU, False)
    assert result.elapsed_ms >= 0

    body = json.loads(route.calls.last.request.content)
    assert body["model"] == "qwen3:8b"
    assert body["stream"] is True
    assert body["think"] is False
    assert body["keep_alive"] == "30m"
    assert body["options"]["num_ctx"] == 2048
    assert body["options"]["num_gpu"] == 99
    assert body["messages"][-1] == {"role": "user", "content": "Get in the car."}


@respx.mock
def test_num_gpu_is_left_to_ollama_by_default() -> None:
    route = respx.post(f"{URL}/api/chat").mock(
        return_value=httpx.Response(200, content=ndjson("Selam."))
    )
    with_default = OllamaBackend("qwen3:8b", url=URL)
    with_default.translate(TranslationRequest("Hi."), CancelToken())
    assert "num_gpu" not in json.loads(route.calls.last.request.content)["options"]
    with_default.close()


@respx.mock
def test_missing_model(backend: OllamaBackend) -> None:
    respx.post(f"{URL}/api/chat").mock(
        return_value=httpx.Response(404, json={"error": "model 'qwen3:8b' not found"})
    )
    with pytest.raises(ModelNotFoundError, match="not found"):
        backend.translate(TranslationRequest("Hi."), CancelToken())


@respx.mock
@pytest.mark.parametrize("error", [httpx.ConnectError("refused"), httpx.ConnectTimeout("dropped")])
def test_server_down(backend: OllamaBackend, error: httpx.HTTPError) -> None:
    # A closed port is refused or, behind a firewall, simply times out.
    respx.post(f"{URL}/api/chat").mock(side_effect=error)
    with pytest.raises(BackendUnavailableError):
        backend.translate(TranslationRequest("Hi."), CancelToken())


@respx.mock
def test_error_inside_the_stream(backend: OllamaBackend) -> None:
    respx.post(f"{URL}/api/chat").mock(
        return_value=httpx.Response(200, content=b'{"error": "out of memory"}\n')
    )
    with pytest.raises(TranslationError, match="out of memory"):
        backend.translate(TranslationRequest("Hi."), CancelToken())


@respx.mock
def test_empty_answer_is_an_error(backend: OllamaBackend) -> None:
    respx.post(f"{URL}/api/chat").mock(return_value=httpx.Response(200, content=ndjson('""')))
    with pytest.raises(TranslationError, match="empty"):
        backend.translate(TranslationRequest("Hi."), CancelToken())


def test_already_cancelled_request_is_not_sent(backend: OllamaBackend) -> None:
    cancel = CancelToken()
    cancel.cancel()
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(f"{URL}/api/chat")
        with pytest.raises(TranslationCancelledError):
            backend.translate(TranslationRequest("Hi."), cancel)
        assert not route.called


@respx.mock
def test_cancel_mid_stream(backend: OllamaBackend) -> None:
    cancel = CancelToken()

    def chunks() -> Iterator[bytes]:
        yield json.dumps({"message": {"content": "Araba"}, "done": False}).encode() + b"\n"
        cancel.cancel()  # a newer line arrived while the model was writing
        yield json.dumps({"message": {"content": "ya bin."}, "done": False}).encode() + b"\n"
        yield json.dumps({"message": {"content": ""}, "done": True}).encode() + b"\n"

    respx.post(f"{URL}/api/chat").mock(return_value=httpx.Response(200, content=chunks()))
    with pytest.raises(TranslationCancelledError):
        backend.translate(TranslationRequest("Get in the car."), cancel)


@respx.mock
def test_load_and_unload(backend: OllamaBackend) -> None:
    route = respx.post(f"{URL}/api/generate").mock(return_value=httpx.Response(200, json={}))
    backend.load()
    backend.unload()
    bodies = [json.loads(call.request.content) for call in route.calls]
    assert [b["keep_alive"] for b in bodies] == ["30m", 0]
    assert all(b["model"] == "qwen3:8b" and b["stream"] is False for b in bodies)


@respx.mock
def test_warm_up_runs_one_real_chat(backend: OllamaBackend) -> None:
    route = respx.post(f"{URL}/api/chat").mock(
        return_value=httpx.Response(200, content=ndjson("Selam."))
    )
    assert backend.warm_up() >= 0
    assert route.call_count == 1
    assert json.loads(route.calls.last.request.content)["messages"][-1]["content"] == "Hi."


def test_cancel_callbacks() -> None:
    token, calls = CancelToken(), []
    token.on_cancel(lambda: calls.append("a"))
    token.cancel()
    token.cancel()  # idempotent
    token.on_cancel(lambda: calls.append("b"))  # late registration runs at once
    assert calls == ["a", "b"]


# --- Cache ---------------------------------------------------------------------------------------


def test_cache_key_ignores_case_and_punctuation() -> None:
    cache = TranslationCache()
    cache.put("qwen3:8b", "gta", "Get in the car!", "Arabaya bin!")
    assert cache.get("qwen3:8b", "gta", "get in the car") == "Arabaya bin!"
    assert cache.get("qwen3:8b", "other", "Get in the car!") is None
    assert cache.get("aya-expanse:8b", "gta", "Get in the car!") is None


def test_cache_persists_in_sqlite(tmp_path: Path) -> None:
    path = tmp_path / "cache" / "translations.sqlite3"
    first = TranslationCache(path)
    first.put("qwen3:8b", "gta", "Hi.", "Selam.")
    first.close()
    second = TranslationCache(path)
    assert second.get("qwen3:8b", "gta", "Hi.") == "Selam."
    second.clear("qwen3:8b")
    assert second.get("qwen3:8b", "gta", "Hi.") is None
    second.close()


def test_cache_memory_is_bounded() -> None:
    cache = TranslationCache(memory_size=2)
    for i in range(3):
        cache.put("m", "p", f"line {i}", f"satır {i}")
    assert cache.get("m", "p", "line 0") is None  # evicted, no disk behind it
    assert cache.get("m", "p", "line 2") == "satır 2"


# --- Orchestrator --------------------------------------------------------------------------------


class FakeBackend:
    """Translates by upper-casing; can hold requests until released, honors cancellation."""

    model = "fake:1b"
    lane = Lane.GPU

    def __init__(self) -> None:
        self.requests: list[TranslationRequest] = []
        self.started = threading.Semaphore(0)
        self.release = threading.Event()
        self.release.set()
        self.ignore_cancel = False
        self.fail_with: TranslationError | None = None

    def translate(self, request: TranslationRequest, cancel: CancelToken) -> TranslationResult:
        self.requests.append(request)
        self.started.release()
        self.release.wait(5)
        if cancel.cancelled and not self.ignore_cancel:
            raise TranslationCancelledError(request.text)
        if self.fail_with is not None:
            raise self.fail_with
        return TranslationResult(request.text.upper(), self.model, self.lane, 12.0)


class Events:
    def __init__(self) -> None:
        self.items: list[TranslationEvent] = []
        self._cond = threading.Condition()

    def __call__(self, event: TranslationEvent) -> None:
        with self._cond:
            self.items.append(event)
            self._cond.notify_all()

    def wait_for(self, count: int, timeout: float = 5.0) -> list[TranslationEvent]:
        with self._cond:
            assert self._cond.wait_for(lambda: len(self.items) >= count, timeout), self.items
            return list(self.items)


@pytest.fixture
def fake() -> FakeBackend:
    return FakeBackend()


@pytest.fixture
def events() -> Events:
    return Events()


@pytest.fixture
def orchestrator(fake: FakeBackend, events: Events) -> Iterator[TranslationOrchestrator]:
    orch = TranslationOrchestrator(fake, TranslationCache(), events, profile_id="gta")
    yield orch
    orch.close()


def test_translates_and_then_serves_from_cache(
    orchestrator: TranslationOrchestrator, fake: FakeBackend, events: Events
) -> None:
    orchestrator.submit(1, "Get in the car.")
    first = events.wait_for(1)[0]
    assert isinstance(first, LineTranslated)
    assert (first.line_id, first.source, first.result.text) == (
        1,
        "Get in the car.",
        "GET IN THE CAR.",
    )
    assert not first.result.cached

    orchestrator.submit(2, "get in the car")  # same line again, read slightly differently
    second = events.wait_for(2)[1]
    assert second.result.cached
    assert second.result.text == "GET IN THE CAR."
    assert len(fake.requests) == 1


def test_orchestrator_passes_the_profile_hints(fake: FakeBackend, events: Events) -> None:
    orch = TranslationOrchestrator(
        fake,
        TranslationCache(),
        events,
        game_context="Vice City crime story.",
        glossary={"Leonida": "Leonida"},
        examples={"Copy.": "Anlaşıldı."},
    )
    try:
        orch.submit(1, "Get in.")
        events.wait_for(1)
    finally:
        orch.close()
    request = fake.requests[0]
    assert request.game_context == "Vice City crime story."
    assert request.glossary == {"Leonida": "Leonida"}
    assert request.examples == (("Copy.", "Anlaşıldı."),)


def test_short_reply_between_long_lines_is_translated(
    orchestrator: TranslationOrchestrator, fake: FakeBackend, events: Events
) -> None:
    """Regression: a new line used to cancel the one in flight, so a short reply that is on
    screen for less time than a translation takes was never translated or shown."""
    fake.release.clear()
    orchestrator.submit(1, "I mean, look at Lori's Snapmatic. Looks like they're loving it.")
    assert fake.started.acquire(timeout=5)
    fake.release.set()
    events.wait_for(1)
    fake.release.clear()
    orchestrator.submit(2, "Yeah.")
    assert fake.started.acquire(timeout=5)  # the short reply is in flight...
    orchestrator.submit(3, "Who is this guy? You know, besides Brian's old cellmate.")
    fake.release.set()  # ...when the next line arrives
    assert [e.line_id for e in events.wait_for(3)] == [1, 2, 3]
    assert events.items[1].result.text == "YEAH."


def test_lines_are_published_in_reading_order(
    orchestrator: TranslationOrchestrator, fake: FakeBackend, events: Events
) -> None:
    orchestrator.submit(1, "Cached line.")
    events.wait_for(1)
    fake.release.clear()
    orchestrator.submit(2, "A slow line.")
    assert fake.started.acquire(timeout=5)
    orchestrator.submit(3, "Cached line.")  # a cache hit must not jump ahead of line 2
    fake.release.set()
    published = events.wait_for(3)
    assert [e.line_id for e in published] == [1, 2, 3]
    assert published[2].result.cached


def test_backlog_is_bounded(fake: FakeBackend, events: Events) -> None:
    orch = TranslationOrchestrator(fake, TranslationCache(), events, max_backlog=2)
    try:
        fake.release.clear()
        orch.submit(1, "One.")
        assert fake.started.acquire(timeout=5)
        for line_id, text in ((2, "Two."), (3, "Three."), (4, "Four.")):
            orch.submit(line_id, text)  # only the 2 newest may wait: line 2 is skipped
        fake.release.set()
        assert [e.line_id for e in events.wait_for(3)] == [1, 3, 4]
    finally:
        orch.close()


def test_upgrade_replaces_its_own_line_only(
    orchestrator: TranslationOrchestrator, fake: FakeBackend, events: Events
) -> None:
    fake.release.clear()
    orchestrator.submit(1, "know, besides Brian's old cellmate.")
    assert fake.started.acquire(timeout=5)
    orchestrator.submit(1, "You know, besides Brian's old cellmate.")  # better reading
    orchestrator.submit(2, "Look what the law chased in.")
    fake.release.set()
    published = events.wait_for(2)
    assert [(e.line_id, e.source) for e in published] == [
        (1, "You know, besides Brian's old cellmate."),
        (2, "Look what the law chased in."),
    ]


def test_history_feeds_the_next_request(
    orchestrator: TranslationOrchestrator, fake: FakeBackend, events: Events
) -> None:
    for line_id, text in enumerate(["Yo.", "We're late.", "Get in the car."], start=1):
        orchestrator.submit(line_id, text)
        events.wait_for(line_id)
    assert fake.requests[2].history == [
        ContextLine("Yo.", "YO."),
        ContextLine("We're late.", "WE'RE LATE."),
    ]

    # An upgraded reading of line 3 replaces it and does not see itself as context.
    orchestrator.submit(3, "Now get in the car.")
    events.wait_for(4)
    assert [c.source for c in fake.requests[3].history] == ["Yo.", "We're late."]
    orchestrator.submit(4, "Go!")
    events.wait_for(5)
    assert [c.source for c in fake.requests[4].history] == [
        "Yo.",
        "We're late.",
        "Now get in the car.",
    ]


def test_history_is_bounded_and_can_be_cleared(fake: FakeBackend, events: Events) -> None:
    orch = TranslationOrchestrator(fake, TranslationCache(), events, history_size=1)
    try:
        for line_id, text in enumerate(["One.", "Two.", "Three."], start=1):
            orch.submit(line_id, text)
            events.wait_for(line_id)
        assert [c.source for c in fake.requests[2].history] == ["Two."]
        orch.clear_history()
        orch.submit(4, "Four.")
        events.wait_for(4)
        assert fake.requests[3].history == []
    finally:
        orch.close()


def test_failures_are_reported(
    orchestrator: TranslationOrchestrator, fake: FakeBackend, events: Events
) -> None:
    fake.fail_with = ModelNotFoundError("model 'fake:1b' is not installed")
    orchestrator.submit(1, "Hi.")
    failed = events.wait_for(1)[0]
    assert isinstance(failed, LineFailed)
    assert isinstance(failed.error, ModelNotFoundError)


def test_close_cancels_work(fake: FakeBackend, events: Events) -> None:
    fake.release.clear()
    orch = TranslationOrchestrator(fake, TranslationCache(), events)
    orch.submit(1, "Hi.")
    assert fake.started.acquire(timeout=5)
    fake.release.set()
    orch.close()
    assert events.items == []
