"""First-run setup: model recommendation, model download, settings backup and the dialog."""

import json
import threading
import zipfile
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
import respx
from pytestqt.qtbot import QtBot

from livesubs.core.backup import BackupError, export_settings, import_settings
from livesubs.core.config import AppConfig
from livesubs.ollama import catalog
from livesubs.ollama.client import (
    DEFAULT_URL,
    OllamaError,
    PullCancelledError,
    PullError,
    PullProgress,
    create_text_only,
    parse_parameters,
    pull_model,
)
from livesubs.ollama.install import WINGET_ID, install_command
from livesubs.platform.shortcuts import _quoted
from livesubs.ui import setup_check
from livesubs.ui.setup_check import SetupDialog, SetupStatus, free_bytes

BIG, SMALL = (m.name for m in catalog.RECOMMENDED)
BIG_TEXT = catalog.RECOMMENDED[0].text_only


# --- catalog ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("ram_gb", "expected"), [(31.7, BIG), (24, BIG), (15.8, SMALL), (4, SMALL)]
)
def test_recommendation_follows_ram(ram_gb: float, expected: str) -> None:
    assert catalog.recommend(ram_gb).name == expected


def test_catalog_entries_are_found_by_their_text_only_copy() -> None:
    assert BIG_TEXT
    assert catalog.entry_for(BIG_TEXT).name == BIG
    assert catalog.entry_for(BIG).use_name == BIG_TEXT
    assert catalog.entry_for(SMALL).use_name == SMALL
    assert catalog.entry_for("qwen3:8b") is None


def test_untagged_names_mean_latest() -> None:
    assert catalog.same_model("qwen3", "qwen3:latest")
    assert not catalog.same_model("qwen3:8b", "qwen3:latest")


# --- model download -----------------------------------------------------------------------------


def ndjson(*updates: dict) -> bytes:
    return b"".join(json.dumps(u).encode() + b"\n" for u in updates)


@respx.mock
def test_pull_reports_progress_over_all_files() -> None:
    route = respx.post(f"{DEFAULT_URL}/api/pull").mock(
        return_value=httpx.Response(
            200,
            content=ndjson(
                {"status": "pulling manifest"},
                {"status": "pulling aa", "digest": "aa", "total": 1000, "completed": 500},
                {"status": "pulling bb", "digest": "bb", "total": 100, "completed": 0},
                {"status": "pulling aa", "digest": "aa", "total": 1000, "completed": 1000},
                {"status": "pulling bb", "digest": "bb", "total": 100, "completed": 100},
                {"status": "verifying sha256 digest"},
                {"status": "success"},
            ),
        )
    )
    seen: list[PullProgress] = []
    pull_model("gemma4:12b-it-qat", seen.append, threading.Event())
    assert json.loads(route.calls[0].request.content) == {
        "model": "gemma4:12b-it-qat",
        "stream": True,
    }
    assert seen[0] == PullProgress("pulling manifest", 0, 0)
    assert (seen[2].completed, seen[2].total) == (500, 1100)
    assert (seen[-1].status, seen[-1].completed, seen[-1].total) == ("success", 1100, 1100)


@respx.mock
def test_pull_of_an_unknown_model_fails_with_ollamas_reason() -> None:
    respx.post(f"{DEFAULT_URL}/api/pull").mock(
        return_value=httpx.Response(500, json={"error": "pull model manifest: file does not exist"})
    )
    with pytest.raises(PullError, match="file does not exist"):
        pull_model("nope:1b", lambda _: None, threading.Event())


@respx.mock
def test_pull_error_inside_the_stream() -> None:
    respx.post(f"{DEFAULT_URL}/api/pull").mock(
        return_value=httpx.Response(
            200, content=ndjson({"status": "pulling manifest"}, {"error": "no space left"})
        )
    )
    with pytest.raises(PullError, match="no space left"):
        pull_model("m", lambda _: None, threading.Event())


@respx.mock
def test_pull_without_success_is_an_error() -> None:
    respx.post(f"{DEFAULT_URL}/api/pull").mock(
        return_value=httpx.Response(200, content=ndjson({"status": "pulling manifest"}))
    )
    with pytest.raises(PullError):
        pull_model("m", lambda _: None, threading.Event())


@respx.mock
def test_pull_server_down() -> None:
    respx.post(f"{DEFAULT_URL}/api/pull").mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(PullError):
        pull_model("m", lambda _: None, threading.Event())


@respx.mock
def test_pull_can_be_cancelled() -> None:
    respx.post(f"{DEFAULT_URL}/api/pull").mock(
        return_value=httpx.Response(
            200,
            content=ndjson(
                {"status": "pulling aa", "digest": "aa", "total": 10, "completed": 1},
                {"status": "pulling aa", "digest": "aa", "total": 10, "completed": 2},
                {"status": "success"},
            ),
        )
    )
    cancel = threading.Event()
    seen: list[PullProgress] = []

    def on_progress(progress: PullProgress) -> None:
        seen.append(progress)
        cancel.set()

    with pytest.raises(PullCancelledError):
        pull_model("m", on_progress, cancel)
    assert len(seen) == 1


# --- text-only copy -----------------------------------------------------------------------------


def write_manifest(models: Path, name: str, layers: list[tuple[str, str]]) -> None:
    model, tag = name.split(":")
    path = models / "manifests" / "registry.ollama.ai" / "library" / model / tag
    path.parent.mkdir(parents=True)
    entries = [
        {"mediaType": f"application/vnd.ollama.image.{kind}", "digest": digest}
        for kind, digest in layers
    ]
    path.write_text(json.dumps({"layers": entries}), encoding="utf-8")


def test_show_parameters_become_create_parameters() -> None:
    text = 'temperature                    1\ntop_p     0.95\nstop "<a>"\nstop "<b>"\n'
    assert parse_parameters(text) == {"temperature": 1, "top_p": 0.95, "stop": ["<a>", "<b>"]}


@respx.mock
def test_text_only_copy_reuses_the_weights_without_the_projector(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OLLAMA_MODELS", str(tmp_path))
    write_manifest(tmp_path, BIG, [("model", "sha256:weights"), ("projector", "sha256:vision")])
    respx.post(f"{DEFAULT_URL}/api/show").mock(
        return_value=httpx.Response(
            200,
            json={
                "parameters": "temperature    1\ntop_k    64",
                "template": "{{ .Prompt }}",
                "renderer": "gemma4-large",
                "parser": "gemma4",
            },
        )
    )
    create = respx.post(f"{DEFAULT_URL}/api/create").mock(
        return_value=httpx.Response(200, json={"status": "success"})
    )
    create_text_only(BIG, BIG_TEXT)
    assert json.loads(create.calls[0].request.content) == {
        "model": BIG_TEXT,
        "files": {"model.gguf": "sha256:weights"},
        "parameters": {"temperature": 1, "top_k": 64},
        "template": "{{ .Prompt }}",
        "renderer": "gemma4-large",
        "parser": "gemma4",
        "stream": False,
    }


def test_text_only_copy_needs_the_manifest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OLLAMA_MODELS", str(tmp_path))
    with pytest.raises(OllamaError):
        create_text_only(BIG, BIG_TEXT)


def test_free_space_of_a_folder_that_does_not_exist_yet(tmp_path: Path) -> None:
    assert free_bytes(tmp_path / "models" / "blobs") > 0


# --- Ollama installation and shortcuts ----------------------------------------------------------


def test_ollama_installs_silently_without_prompts() -> None:
    command = install_command("winget.exe")
    assert command[:2] == ["winget.exe", "install"]
    assert WINGET_ID in command
    assert {"--silent", "--accept-package-agreements", "--accept-source-agreements"} <= set(command)


def test_shortcut_paths_stay_literal_in_powershell() -> None:
    assert _quoted(Path(r"C:\Oyunlar\Mert's $Apps\LiveSubs.exe")) == (
        r"C:\Oyunlar\Mert''s $Apps\LiveSubs.exe"
    )


# --- settings backup ----------------------------------------------------------------------------


def write_settings(folder: Path) -> None:
    (folder / "profiles").mkdir(parents=True)
    (folder / "config.json").write_text('{"schema_version": 1}', encoding="utf-8")
    (folder / "profiles" / "varsayilan.json").write_text('{"name": "Ä"}', encoding="utf-8")
    (folder / "cache").mkdir()
    (folder / "cache" / "translations.sqlite3").write_bytes(b"not settings")


def test_settings_survive_a_reinstall(tmp_path: Path) -> None:
    old, new = tmp_path / "old", tmp_path / "new"
    write_settings(old)
    archive = tmp_path / "LiveSubs-ayarlar.zip"
    assert export_settings(archive, old) == 2
    assert sorted(zipfile.ZipFile(archive).namelist()) == [
        "config.json",
        "profiles/varsayilan.json",
    ]
    assert import_settings(archive, new) == 2
    assert (new / "profiles" / "varsayilan.json").read_text(encoding="utf-8") == '{"name": "Ä"}'
    assert (new / "config.json").read_text(encoding="utf-8") == '{"schema_version": 1}'


def test_import_takes_only_settings_files(tmp_path: Path) -> None:
    archive = tmp_path / "x.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("config.json", "{}")
        z.writestr("../evil.json", "{}")
        z.writestr("profiles/../../evil.json", "{}")
        z.writestr("profiles/sub/deep.json", "{}")
        z.writestr("notes.txt", "hi")
    target = tmp_path / "target"
    assert import_settings(archive, target) == 1
    assert [p.name for p in target.rglob("*")] == ["config.json"]
    assert not (tmp_path / "evil.json").exists()


@pytest.mark.parametrize("content", [b"not a zip", None])
def test_import_rejects_files_without_settings(tmp_path: Path, content: bytes | None) -> None:
    archive = tmp_path / "x.zip"
    if content is None:
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr("readme.txt", "hi")
    else:
        archive.write_bytes(content)
    with pytest.raises(BackupError):
        import_settings(archive, tmp_path / "target")


def test_nothing_to_export(tmp_path: Path) -> None:
    with pytest.raises(BackupError):
        export_settings(tmp_path / "x.zip", tmp_path)


# --- status and dialog --------------------------------------------------------------------------

READY = SetupStatus("en-US", True, (f"{SMALL}", "qwen3:latest"))


def test_setup_is_complete_with_ocr_and_the_chosen_model() -> None:
    assert READY.complete(SMALL)
    assert not READY.complete(BIG)
    assert READY.has_model("qwen3")
    assert not SetupStatus(None, True, (SMALL,)).complete(SMALL)
    assert not SetupStatus("en-US", True, None).complete(SMALL)


def dialog(qtbot: QtBot, status: SetupStatus, model: str = SMALL) -> SetupDialog:
    config = AppConfig()
    config.ollama.gpu_model = model
    dlg = SetupDialog(config, checker=lambda url: status, ram_gb=32)
    qtbot.addWidget(dlg)
    return dlg


def test_startup_check_stays_hidden_when_everything_is_ready(qtbot: QtBot) -> None:
    dlg = dialog(qtbot, READY)
    dlg.check_on_startup()
    qtbot.waitUntil(lambda: dlg._status is not None)
    assert not dlg.isVisible()


def test_startup_check_opens_on_a_fresh_pc(qtbot: QtBot) -> None:
    dlg = dialog(qtbot, SetupStatus(None, False, None))
    dlg.check_on_startup()
    qtbot.waitUntil(dlg.isVisible)
    assert dlg.ocr_button.isVisibleTo(dlg)
    assert dlg.ollama_button.text() == "Kur"
    assert not dlg.model_button.isEnabled()  # a model needs Ollama first
    # The fresh PC gets the model its RAM can run best.
    assert dlg.model_combo.currentData() == BIG


def test_installed_model_is_used_without_downloading(qtbot: QtBot) -> None:
    dlg = dialog(qtbot, READY, model=BIG)
    dlg.refresh()
    qtbot.waitUntil(lambda: dlg._status is not None)
    dlg.model_combo.setCurrentIndex(1)  # SMALL, installed
    assert dlg.model_button.text() == "Bunu kullan"
    with qtbot.waitSignal(dlg.model_selected) as signal:
        dlg.model_button.click()
    assert signal.args == [SMALL]


def fake_pull(updates: list[PullProgress], error: Exception | None = None) -> Callable[..., None]:
    def pull(name: str, on_progress: Callable, cancel: threading.Event, url: str) -> None:
        for update in updates:
            on_progress(update)
        if error is not None:
            raise error

    return pull


def test_download_shows_progress_and_selects_the_text_only_copy(
    qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    created: list[tuple[str, str]] = []
    monkeypatch.setattr(
        setup_check, "create_text_only", lambda name, new, url: created.append((name, new))
    )
    monkeypatch.setattr(setup_check, "free_bytes", lambda folder: 10**12)
    monkeypatch.setattr(
        setup_check,
        "pull_model",
        fake_pull([PullProgress("pulling aa", 2 * 1024**3, 8 * 1024**3)]),
    )
    dlg = dialog(qtbot, SetupStatus("en-US", True, ()))
    dlg.refresh()
    qtbot.waitUntil(lambda: dlg._status is not None)
    assert dlg.model_button.text() == "İndir"
    with qtbot.waitSignal(dlg.model_selected) as signal:
        dlg.model_button.click()
    assert signal.args == [BIG_TEXT]
    assert created == [(BIG, BIG_TEXT)]
    assert not dlg.busy
    assert dlg.model_button.isEnabled()


def test_installed_big_model_switches_to_its_text_only_copy(
    qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A PC set up before the copy existed: the base model is installed and in use.
    monkeypatch.setattr(setup_check, "create_text_only", lambda name, new, url: None)
    dlg = dialog(qtbot, SetupStatus("en-US", True, (BIG,)), model=BIG)
    dlg.refresh()
    qtbot.waitUntil(lambda: dlg._status is not None)
    assert dlg.model_combo.currentData() == BIG
    assert dlg.model_button.text() == "Bunu kullan"
    with qtbot.waitSignal(dlg.model_selected) as signal:
        dlg.model_button.click()
    assert signal.args == [BIG_TEXT]


def test_failed_text_only_copy_falls_back_to_the_model(
    qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(name: str, new: str, url: str) -> None:
        raise OllamaError("create refused")

    monkeypatch.setattr(setup_check, "create_text_only", fail)
    dlg = dialog(qtbot, SetupStatus("en-US", True, (BIG,)), model=SMALL)
    dlg.refresh()
    qtbot.waitUntil(lambda: dlg._status is not None)
    dlg.model_combo.setCurrentIndex(0)
    with qtbot.waitSignal(dlg.model_selected) as signal:
        dlg.model_button.click()
    assert signal.args == [BIG]
    assert "create refused" in dlg.progress_label.text()


def test_text_only_copy_in_use_needs_no_action(qtbot: QtBot) -> None:
    dlg = dialog(qtbot, SetupStatus("en-US", True, (BIG, BIG_TEXT)), model=BIG_TEXT)
    dlg.refresh()
    qtbot.waitUntil(lambda: dlg._status is not None)
    assert dlg.model_combo.currentData() == BIG
    assert dlg.model_button.text() == "Kullanılıyor"


def test_cancelled_download_says_it_resumes(qtbot: QtBot, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(setup_check, "free_bytes", lambda folder: 10**12)
    monkeypatch.setattr(setup_check, "pull_model", fake_pull([], PullCancelledError()))
    dlg = dialog(qtbot, SetupStatus("en-US", True, ()))
    dlg.refresh()
    qtbot.waitUntil(lambda: dlg._status is not None)
    dlg.model_button.click()
    qtbot.waitUntil(lambda: not dlg.downloading)
    assert "kaldığı yerden" in dlg.progress_label.text()


def test_download_needs_disk_space(qtbot: QtBot, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(setup_check, "free_bytes", lambda folder: 1024**3)
    dlg = dialog(qtbot, SetupStatus("en-US", True, ()))
    dlg.refresh()
    qtbot.waitUntil(lambda: dlg._status is not None)
    dlg.model_button.click()
    assert not dlg.downloading
    assert "Diskte yer yok" in dlg.progress_label.text()


def test_model_in_use_needs_no_action(qtbot: QtBot) -> None:
    dlg = dialog(qtbot, READY, model=SMALL)
    dlg.refresh()
    qtbot.waitUntil(lambda: dlg._status is not None)
    assert dlg.model_combo.currentData() == SMALL  # the installed, configured model is shown
    assert dlg.model_button.text() == "Kullanılıyor"
    assert not dlg.model_button.isEnabled()
