"""A second LiveSubs start finds the first one and asks it to show its window."""

import uuid
from pathlib import Path

import pytest
from pytestqt.qtbot import QtBot

from livesubs.core import paths
from livesubs.ui.single_instance import SingleInstance, instance_name


def test_second_start_activates_the_first(qtbot: QtBot) -> None:
    name = f"livesubs-test-{uuid.uuid4().hex}"
    first = SingleInstance(name)
    assert first.listen()
    try:
        with qtbot.waitSignal(first.activated, timeout=2000):
            assert SingleInstance(name).notify_running()
    finally:
        first.close()


def test_nothing_running(qtbot: QtBot) -> None:
    assert not SingleInstance(f"livesubs-test-{uuid.uuid4().hex}").notify_running()


def test_name_follows_the_settings_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    default = instance_name()
    monkeypatch.setenv(paths.HOME_ENV_VAR, str(tmp_path))
    assert instance_name() != default
    assert instance_name().startswith("LiveSubs-")
