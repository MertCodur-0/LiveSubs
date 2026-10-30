"""Model management: VRAM estimates, Ollama model calls, the side-by-side test and the tab."""

import gc
import json
import threading
import weakref
from collections.abc import Callable

import httpx
import pytest
import respx
from pytestqt.qtbot import QtBot

from livesubs.core.config import AppConfig, OllamaSettings
from livesubs.core.profiles import Profile, VideoDeviceSource, VramSettings
from livesubs.engine.translate.base import (
    CancelToken,
    Lane,
    ModelNotFoundError,
    TranslationRequest,
    TranslationResult,
)
from livesubs.ollama import vram
from livesubs.ollama.client import (
    DEFAULT_URL,
    InstalledModel,
    LoadedModel,
    OllamaError,
    delete_model,
    loaded_models,
    model_info,
)
from livesubs.ollama.compare import LineResult, ModelRun, run_comparison
from livesubs.ollama.vram import GB, Fit
from livesubs.platform.gpu import GpuAdapter
from livesubs.ui.tabs import models as models_tab
from livesubs.ui.tabs.models import ModelsTab, Snapshot, cpu_share, fetch_snapshot

# Shapes of real models, as /api/show reports them (trimmed to the keys the estimate reads).
QWEN3_8B = {
    "general.architecture": "qwen3",
    "qwen3.block_count": 36,
    "qwen3.attention.head_count": 32,
    "qwen3.attention.head_count_kv": 8,
    "qwen3.attention.key_length": 128,
    "qwen3.attention.value_length": 128,
    "qwen3.embedding_length": 4096,
}
GEMMA4_26B = {
    "general.architecture": "gemma4",
    "gemma4.block_count": 30,
    "gemma4.attention.head_count": 16,
    "gemma4.attention.head_count_kv": [8, 8, 8, 8, 8, 2] * 5,
    "gemma4.attention.key_length": 512,
    "gemma4.attention.key_length_swa": 256,
    "gemma4.attention.value_length": 512,
    "gemma4.attention.value_length_swa": 256,
    "gemma4.attention.sliding_window": 1024,
    "gemma4.attention.sliding_window_pattern": [True] * 5 + [False],  # shorter: repeats last
    "gemma4.embedding_length": 2816,
    "gemma4.expert_count": 128,
}
GEMMA3_4B = {
    "general.architecture": "gemma3",
    "gemma3.block_count": 34,
    "gemma3.attention.head_count": 8,
    "gemma3.attention.head_count_kv": 4,
    "gemma3.attention.key_length": 256,
    "gemma3.attention.value_length": 256,
    "gemma3.attention.sliding_window": 1024,
    "gemma3.embedding_length": 2560,
}


# --- VRAM estimate ------------------------------------------------------------------------------


def test_dense_model_caches_every_token_in_every_layer() -> None:
    arch = vram.parse_arch(QWEN3_8B)
    assert arch is not None and not arch.is_moe
    assert len(arch.layers) == 36
    assert vram.kv_cache_bytes(arch, 2048) == 36 * 2048 * 8 * (128 + 128) * 2  # 0.30 GB


def test_sliding_window_layers_cache_only_their_window() -> None:
    arch = vram.parse_arch(GEMMA4_26B)
    assert arch is not None and arch.is_moe
    # The pattern list is shorter than the layers here; the per-layer values go on as given.
    windowed = [layer for layer in arch.layers if layer.window is not None]
    assert windowed[0] == vram.LayerCache(8, 256, 256, 1024)
    full = [layer for layer in arch.layers if layer.window is None]
    assert full[0] == vram.LayerCache(2, 512, 512, None)
    small = vram.kv_cache_bytes(arch, 1024)
    big = vram.kv_cache_bytes(arch, 32768)
    # Only the full-context layers grow with the context once it exceeds window + batch.
    per_token_full = sum(layer.kv_heads * 1024 * 2 for layer in full)
    windowed_bytes = sum(layer.kv_heads * 512 * 2 * (1024 + 512) for layer in windowed)
    assert big == windowed_bytes + per_token_full * 32768
    assert small < big


def test_gemma3_global_layers_without_a_pattern() -> None:
    arch = vram.parse_arch(GEMMA3_4B)
    assert arch is not None
    globals_ = [i for i, layer in enumerate(arch.layers) if layer.window is None]
    assert globals_ == [5, 11, 17, 23, 29]


def test_unknown_metadata_has_no_shape() -> None:
    assert vram.parse_arch({}) is None
    assert vram.parse_arch({"general.architecture": "x"}) is None


def test_head_dim_defaults_from_the_embedding() -> None:
    info = {k: v for k, v in QWEN3_8B.items() if "length" not in k or "embedding" in k}
    arch = vram.parse_arch(info)
    assert arch is not None
    assert arch.layers[0].key_length == 4096 // 32


@pytest.mark.parametrize(
    ("estimate_gb", "moe", "fit"),
    [
        (5.0, False, Fit.FITS),
        (6.5, False, Fit.BORDERLINE),
        (8.0, False, Fit.TOO_BIG),
        (16.0, True, Fit.MOE_SPLIT),
        (5.0, True, Fit.FITS),  # a small MoE model fits like any other
    ],
)
def test_fit_against_the_budget(estimate_gb: float, moe: bool, fit: Fit) -> None:
    assert vram.judge(round(estimate_gb * GB), round(6.8 * GB), moe=moe) == fit


def test_budget_leaves_room_for_the_desktop_and_the_game() -> None:
    assert vram.budget_bytes(round(8.3 * GB), 0.5) == round(6.8 * GB)
    assert vram.budget_bytes(GB, 2.0) == 0
    assert vram.judge(GB, 0, moe=False) == Fit.UNKNOWN


def test_estimates_match_the_measurements() -> None:
    """Measured on the dev machine (CLAUDE.md, step 12): qwen3:8b used 5.42 GB of VRAM and fit;
    gemma4:12b (Ollama: 8.52 GB) spilled over an 8 GB card."""
    qwen = vram.estimate_bytes(5_225_388_164, vram.parse_arch(QWEN3_8B), 2048)
    assert 5.42 * GB <= qwen <= 6.0 * GB
    budget = vram.budget_bytes(8_334_082_048, 0.5)
    assert vram.judge(qwen, budget, moe=False) == Fit.FITS


# --- Ollama calls -------------------------------------------------------------------------------


@respx.mock
def test_loaded_models_and_offline() -> None:
    respx.get(f"{DEFAULT_URL}/api/ps").mock(
        return_value=httpx.Response(
            200, json={"models": [{"name": "a:1", "size": 100, "size_vram": 60}]}
        )
    )
    assert loaded_models() == [LoadedModel("a:1", 100, 60)]
    respx.get(f"{DEFAULT_URL}/api/ps").mock(side_effect=httpx.ConnectError("down"))
    assert loaded_models() is None


@respx.mock
def test_model_info_and_delete() -> None:
    show = respx.post(f"{DEFAULT_URL}/api/show").mock(
        return_value=httpx.Response(200, json={"model_info": QWEN3_8B, "details": {}})
    )
    assert model_info("qwen3:8b") == QWEN3_8B
    assert json.loads(show.calls[0].request.content) == {"model": "qwen3:8b"}

    delete = respx.delete(f"{DEFAULT_URL}/api/delete").mock(return_value=httpx.Response(200))
    delete_model("qwen3:8b")
    assert json.loads(delete.calls[0].request.content) == {"model": "qwen3:8b"}

    respx.delete(f"{DEFAULT_URL}/api/delete").mock(
        return_value=httpx.Response(404, json={"error": "model 'x' not found"})
    )
    with pytest.raises(OllamaError, match="not found"):
        delete_model("x")


def _show_route(infos: dict[str, dict]) -> Callable[[httpx.Request], httpx.Response]:
    def answer(request: httpx.Request) -> httpx.Response:
        name = json.loads(request.content)["model"]
        return httpx.Response(200, json={"model_info": infos[name]})

    return answer


@respx.mock
def test_snapshot_reads_each_model_shape_once() -> None:
    respx.get(f"{DEFAULT_URL}/api/tags").mock(
        return_value=httpx.Response(
            200,
            json={
                "models": [
                    {"name": "qwen3:8b", "size": 5, "digest": "q"},
                    {"name": "gemma4:26b", "size": 15, "digest": "g"},
                ]
            },
        )
    )
    respx.get(f"{DEFAULT_URL}/api/ps").mock(return_value=httpx.Response(200, json={"models": []}))
    show = respx.post(f"{DEFAULT_URL}/api/show").mock(
        side_effect=_show_route({"qwen3:8b": QWEN3_8B, "gemma4:26b": GEMMA4_26B})
    )
    snapshot = fetch_snapshot(DEFAULT_URL, known={"q"})
    assert [m.name for m in snapshot.models or []] == ["gemma4:26b", "qwen3:8b"]
    assert set(snapshot.archs) == {"g"}
    assert show.call_count == 1


@respx.mock
def test_snapshot_when_ollama_is_down() -> None:
    respx.get(f"{DEFAULT_URL}/api/tags").mock(side_effect=httpx.ConnectError("down"))
    assert fetch_snapshot(DEFAULT_URL, set()).models is None


@respx.mock
def test_cpu_share_warns_only_for_dense_models() -> None:
    respx.get(f"{DEFAULT_URL}/api/ps").mock(
        return_value=httpx.Response(
            200,
            json={
                "models": [
                    {"name": "gemma4:12b", "size": 8_520, "size_vram": 6_130},
                    {"name": "gemma4:26b", "size": 15_880, "size_vram": 5_240},
                    {"name": "qwen3:8b", "size": 5_280, "size_vram": 5_280},
                ]
            },
        )
    )
    respx.post(f"{DEFAULT_URL}/api/show").mock(
        side_effect=_show_route({"gemma4:12b": QWEN3_8B, "gemma4:26b": GEMMA4_26B})
    )
    assert cpu_share(DEFAULT_URL, "gemma4:12b") == pytest.approx(1 - 6_130 / 8_520)
    assert cpu_share(DEFAULT_URL, "gemma4:26b") is None  # MoE: experts in RAM on purpose
    assert cpu_share(DEFAULT_URL, "qwen3:8b") is None  # fully on the GPU
    assert cpu_share(DEFAULT_URL, "missing:1") is None  # not loaded


# --- side-by-side test --------------------------------------------------------------------------


class FakeBackend:
    def __init__(self, model: str, log: list[str], fail: bool = False) -> None:
        self.model, self.log, self.fail = model, log, fail
        self.requests: list[TranslationRequest] = []

    def translate(self, request: TranslationRequest, cancel: CancelToken) -> TranslationResult:
        if self.fail:
            raise ModelNotFoundError(f"model {self.model!r} is not installed")
        self.requests.append(request)
        self.log.append(f"{self.model}:{request.text}")
        return TranslationResult(f"TR {request.text}", self.model, Lane.GPU, 10.0)

    def unload(self) -> None:
        self.log.append(f"unload {self.model}")

    def close(self) -> None:
        pass


def test_comparison_runs_one_model_after_the_other() -> None:
    log: list[str] = []
    backends: dict[str, FakeBackend] = {}

    def factory(model: str, settings: OllamaSettings) -> FakeBackend:
        backends[model] = FakeBackend(model, log)
        return backends[model]

    runs: list[ModelRun] = []
    lines: list[LineResult] = []
    run_comparison(
        ["a:1", "b:1"],
        ["One.", "Two.", "Three."],
        OllamaSettings(context_lines=1),
        on_model=runs.append,
        on_line=lines.append,
        cancel=CancelToken(),
        game_context="Crime story.",
        glossary={"Ricky": "Ricky"},
        keep_loaded="b:1",
        backend_factory=factory,
        share_of=lambda url, model: 1.0,
    )
    assert log == [
        "a:1:Hi.",
        "a:1:One.",
        "a:1:Two.",
        "a:1:Three.",
        "unload a:1",
        "b:1:Hi.",  # b is the live model: warmed up but kept loaded afterwards
        "b:1:One.",
        "b:1:Two.",
        "b:1:Three.",
    ]
    assert [(r.model, r.gpu_share) for r in runs] == [("a:1", 1.0), ("b:1", 1.0)]
    assert [(r.model, r.index, r.text) for r in lines][:2] == [
        ("a:1", 0, "TR One."),
        ("a:1", 1, "TR Two."),
    ]
    third = backends["a:1"].requests[3]  # warm-up, One, Two, Three
    assert [h.source for h in third.history] == ["Two."]  # context_lines=1
    assert third.game_context == "Crime story."
    assert third.glossary == {"Ricky": "Ricky"}


def test_comparison_goes_on_after_a_broken_model_and_stops_on_cancel() -> None:
    log: list[str] = []
    cancel = CancelToken()
    runs: list[ModelRun] = []

    def on_line(result: LineResult) -> None:
        if result.model == "b:1":
            cancel.cancel()

    run_comparison(
        ["a:1", "b:1", "c:1"],
        ["One.", "Two."],
        OllamaSettings(),
        on_model=runs.append,
        on_line=on_line,
        cancel=cancel,
        backend_factory=lambda model, s: FakeBackend(model, log, fail=model == "a:1"),
        share_of=lambda url, model: None,
    )
    assert runs[0].model == "a:1" and "not installed" in runs[0].error
    assert "unload a:1" in log
    assert not any(entry.startswith("c:1") for entry in log)  # cancelled before c
    assert "unload b:1" in log  # cancelled mid-run, still cleaned up


# --- the tab ------------------------------------------------------------------------------------

ADAPTER = GpuAdapter("Test GPU", round(8.3 * GB))


def _model(name: str, size_gb: float, digest: str) -> InstalledModel:
    return InstalledModel(name, round(size_gb * GB), "8B", "Q4_0", digest)


SNAPSHOT = Snapshot(
    models=[
        _model("gemma4:26b-a4b-it-qat", 15.6, "g"),
        _model("qwen3:8b", 5.2, "q"),
    ],
    loaded=[LoadedModel("gemma4:26b-a4b-it-qat", round(15.9 * GB), round(5.2 * GB))],
    archs={"g": vram.parse_arch(GEMMA4_26B), "q": vram.parse_arch(QWEN3_8B)},
)


@pytest.fixture
def tab(qtbot: QtBot) -> ModelsTab:
    config = AppConfig()
    config.ollama.gpu_model = "gemma4:26b-a4b-it-qat"
    profile = Profile(name="PS5", source=VideoDeviceSource(device_name="card"))
    widget = ModelsTab(config, profile, fetch=lambda url, known: SNAPSHOT, adapter=lambda: ADAPTER)
    qtbot.addWidget(widget)
    widget._on_snapshot(SNAPSHOT)
    return widget


def test_a_running_refresh_does_not_keep_the_tab_alive(qtbot: QtBot) -> None:
    # A worker must never hold the last reference: the tab would be destroyed outside the UI
    # thread (a native crash).
    release = threading.Event()

    def fetch(url: str, known: set[str]) -> Snapshot:
        release.wait(5)
        return SNAPSHOT

    widget = ModelsTab(AppConfig(), Profile(name="x"), fetch=fetch, adapter=lambda: ADAPTER)
    bridge = widget._bridge
    widget.refresh()
    alive = weakref.ref(widget)
    del widget
    gc.collect()
    assert alive() is None
    with qtbot.waitSignal(bridge.snapshot):
        release.set()


def cell(tab: ModelsTab, row: int, column: str) -> str:
    return tab.table.item(row, models_tab.COLUMNS.index(column)).text()


def test_table_labels_against_the_profile_budget(tab: ModelsTab) -> None:
    assert "6.8 GB" in tab.budget_label.text()  # 8.3 - 1.0 desktop - 0.5 (capture card)
    assert cell(tab, 0, "fit") == "✔ Kısmen (MoE)"
    assert "Çeviride kullanılıyor" in cell(tab, 0, "state")
    assert "%33 ekran kartında" in cell(tab, 0, "state")
    assert cell(tab, 1, "fit") == "✔ Sığar"

    # The game runs on this PC now: 2 GB reserve, qwen3:8b no longer fits.
    tab.profile.vram = VramSettings(game_on_this_pc=True)
    tab.set_profile(tab.profile)
    assert "5.3 GB" in tab.budget_label.text()
    assert cell(tab, 1, "fit") == "✘ Sığmaz"


def test_configured_vram_overrides_detection(tab: ModelsTab) -> None:
    tab.config.total_vram_gb = 24
    tab.set_profile(tab.profile)
    assert cell(tab, 0, "fit") == "✔ Sığar"


def test_use_selected_model(tab: ModelsTab, qtbot: QtBot) -> None:
    tab.table.selectRow(0)
    assert not tab.use_button.isEnabled()  # already the translation model
    tab.table.selectRow(1)
    assert tab.use_button.isEnabled()
    with qtbot.waitSignal(tab.model_selected) as signal:
        tab.use_selected()
    assert signal.args == ["qwen3:8b"]
    with qtbot.waitSignal(tab.model_selected) as signal:  # double-click does the same
        tab.table.itemDoubleClicked.emit(tab.table.item(1, 1))
    assert signal.args == ["qwen3:8b"]


def test_delete_asks_first_and_never_while_the_model_translates(
    tab: ModelsTab, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    deleted: list[str] = []
    monkeypatch.setattr(models_tab, "delete_model", lambda name, url: deleted.append(name))
    answer = models_tab.QMessageBox.StandardButton.No
    monkeypatch.setattr(models_tab.QMessageBox, "question", lambda *args: answer)
    tab.table.selectRow(1)
    tab.delete_selected()
    assert deleted == []

    answer = models_tab.QMessageBox.StandardButton.Yes
    with qtbot.waitSignal(tab.models_changed):
        tab.delete_selected()
    assert deleted == ["qwen3:8b"]

    tab.set_busy(True)
    tab.table.selectRow(0)  # the translation model, while translation runs
    assert not tab.delete_button.isEnabled()
    tab.delete_selected()
    assert deleted == ["qwen3:8b"]


def test_offline_ollama_offers_start_and_setup(tab: ModelsTab) -> None:
    tab._on_snapshot(Snapshot(None))
    assert tab.table.rowCount() == 0
    assert not tab.setup_button.isHidden()
    assert not tab.pull_button.isEnabled()


def test_pull_refuses_an_installed_model(tab: ModelsTab) -> None:
    tab.pull_combo.setCurrentText("qwen3:8b")
    tab.start_pull()
    assert "zaten yüklü" in tab.pull_status.text()


def test_compare_fills_the_table(tab: ModelsTab, qtbot: QtBot) -> None:
    calls: list[tuple[list[str], list[str], str]] = []

    def comparison(models, lines, settings, *, on_model, on_line, cancel, keep_loaded, **kw):
        calls.append((models, lines, keep_loaded))
        for model in models:
            on_model(ModelRun(model, 2500.0, 1.0))
            for i, line in enumerate(lines):
                on_line(LineResult(model, i, f"{model} {line}", 400.0))

    tab._comparison = comparison
    tab.compare_a.setCurrentIndex(tab.compare_a.findData("qwen3:8b"))
    tab.compare_b.setCurrentIndex(tab.compare_b.findData("gemma4:26b-a4b-it-qat"))
    tab.compare_lines.setPlainText("Hello there.\n\n  Go away.  ")
    with qtbot.waitSignal(tab._bridge.compare_finished):
        tab.toggle_compare()
    qtbot.waitUntil(lambda: tab._compare_cancel is None)
    assert calls == [
        (
            ["qwen3:8b", "gemma4:26b-a4b-it-qat"],
            ["Hello there.", "Go away."],
            tab.config.ollama.gpu_model,
        )
    ]
    assert tab.compare_table.rowCount() == 2
    assert tab.compare_table.item(1, 1).text() == "qwen3:8b Go away.\n(400 ms)"
    header = tab.compare_table.horizontalHeaderItem(2).text()
    assert "yükleme 2.5 sn" in header and "ortanca 400 ms" in header


def test_no_comparison_while_translating(tab: ModelsTab) -> None:
    tab.set_busy(True)
    assert not tab.compare_button.isEnabled()
    tab.toggle_compare()
    assert tab._compare_cancel is None
    tab.set_busy(False)
    assert tab.compare_button.isEnabled()


def test_comparison_can_be_stopped(tab: ModelsTab, qtbot: QtBot) -> None:
    started = threading.Event()

    def comparison(models, lines, settings, *, cancel, **kw):
        started.set()
        while not cancel.cancelled:
            threading.Event().wait(0.01)

    tab._comparison = comparison
    tab.toggle_compare()
    assert started.wait(2)
    assert tab.compare_button.text() == "Durdur"
    with qtbot.waitSignal(tab._bridge.compare_finished):
        tab.toggle_compare()
    qtbot.waitUntil(lambda: tab._compare_cancel is None)
    assert tab.compare_status.text() == "Durduruldu."
