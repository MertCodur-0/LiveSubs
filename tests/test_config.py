from pathlib import Path

import pytest
from pydantic import ValidationError

from livesubs.core import paths
from livesubs.core.config import AppConfig, WebSettings, load_config, save_config


def test_missing_file_gives_defaults(tmp_path: Path) -> None:
    config = load_config(tmp_path / "config.json")
    assert config == AppConfig()
    assert config.ollama.gpu_model == "gemma4:12b-it-qat"
    assert not config.cpu_lane.enabled
    assert not config.recording_mode


def test_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "sub" / "config.json"
    config = AppConfig(last_profile="vice-city", recording_mode=True)
    config.ollama.num_ctx = 4096
    save_config(config, path)
    assert load_config(path) == config
    assert not list(path.parent.glob(".*.tmp"))


def test_corrupt_file_is_kept_aside(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text("{ not json", encoding="utf-8")
    assert load_config(path) == AppConfig()
    assert (tmp_path / "config.json.bad").read_text(encoding="utf-8") == "{ not json"
    assert not path.exists()


def test_newer_schema_is_not_loaded(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text('{"schema_version": 99, "recording_mode": true}', encoding="utf-8")
    assert load_config(path) == AppConfig()


def test_unknown_fields_in_files_are_dropped(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(
        '{"recording_mode": true, "from_the_future": 1, "web": {"port": 9000, "tls": true}}',
        encoding="utf-8",
    )
    config = load_config(path)
    assert config.recording_mode
    assert config.web.port == 9000


def test_unknown_fields_in_code_are_errors() -> None:
    with pytest.raises(ValidationError):
        AppConfig(recordng_mode=True)  # typo
    with pytest.raises(ValidationError):
        WebSettings(prot=9000)


def test_home_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(paths.HOME_ENV_VAR, str(tmp_path))
    assert paths.config_file() == tmp_path / "config.json"
    assert paths.profiles_dir() == tmp_path / "profiles"
    assert paths.cache_dir() == tmp_path / "cache"
