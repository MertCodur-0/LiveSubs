from pathlib import Path

import pytest
from pydantic import ValidationError

from livesubs.core.geometry import NormRect
from livesubs.core.jsonio import StorageError
from livesubs.core.profiles import (
    GAME_VRAM_RESERVE_LOCAL_GB,
    GAME_VRAM_RESERVE_REMOTE_GB,
    MonitorSource,
    Profile,
    ProfileStore,
    SourceKind,
    VideoDeviceSource,
    VramSettings,
    WindowSource,
    slugify,
)


@pytest.fixture
def store(tmp_path: Path) -> ProfileStore:
    return ProfileStore(tmp_path / "profiles")


def test_round_trip_keeps_source_type(store: ProfileStore) -> None:
    profile = Profile(
        name="Capture card",
        source=VideoDeviceSource(device_name="Elgato HD60 X", width=1920, height=1080, fps=60),
        region=NormRect(x=0.1, y=0.8, width=0.8, height=0.12),
    )
    profile.vision.text_colors = ["#ffd700"]
    profile.translation.glossary = {"Tommy": "Tommy"}

    profile_id = store.save(profile)
    loaded = store.load(profile_id)

    assert loaded == profile
    assert isinstance(loaded.source, VideoDeviceSource)
    assert loaded.source.kind is SourceKind.VIDEO_DEVICE


def test_colors_are_validated_and_normalized() -> None:
    assert Profile(name="x", vision={"text_colors": ["#ffd700"]}).vision.text_colors == ["#FFD700"]
    with pytest.raises(ValidationError):
        Profile(name="x", vision={"text_colors": ["gold"]})


def test_edits_are_validated() -> None:
    profile = Profile(name="x")
    with pytest.raises(ValidationError):
        profile.overlay.text_color = "pink"
    with pytest.raises(ValidationError):
        profile.capture.fps = 0


@pytest.mark.parametrize(
    ("source", "on_this_pc"),
    [
        (None, True),
        (WindowSource(exe_name="GTA5.exe", title="Grand Theft Auto V"), True),
        (WindowSource(exe_name="RemotePlay.exe", title="PS Remote Play"), False),
        (MonitorSource(device_name=r"\\.\DISPLAY1"), True),
        (VideoDeviceSource(device_name="USB Video"), False),
    ],
)
def test_vram_reserve_depends_on_where_the_game_runs(source: object, on_this_pc: bool) -> None:
    profile = Profile(name="x", source=source)
    assert profile.game_on_this_pc() is on_this_pc
    expected = GAME_VRAM_RESERVE_LOCAL_GB if on_this_pc else GAME_VRAM_RESERVE_REMOTE_GB
    assert profile.game_vram_reserve_gb() == expected


def test_vram_reserve_overrides() -> None:
    remote_play = WindowSource(exe_name="RemotePlay.exe")
    assert Profile(
        name="x", source=remote_play, vram=VramSettings(game_on_this_pc=True)
    ).game_vram_reserve_gb() == (GAME_VRAM_RESERVE_LOCAL_GB)
    assert Profile(name="x", vram=VramSettings(game_reserve_gb=3.5)).game_vram_reserve_gb() == 3.5


@pytest.mark.parametrize(
    ("name", "slug"),
    [
        ("Vice City Gece", "vice-city-gece"),
        ("İstanbul Şoförü: Ağır Çekim", "istanbul-soforu-agir-cekim"),  # noqa: RUF001
        ("Pokémon", "pokemon"),
        ("!!!", "profile"),
    ],
)
def test_slugify(name: str, slug: str) -> None:
    assert slugify(name) == slug


def test_ids_do_not_collide(store: ProfileStore) -> None:
    first = store.save(Profile(name="GTA"))
    second = store.save(Profile(name="gta"))
    assert (first, second) == ("gta", "gta-2")
    assert store.ids() == ["gta", "gta-2"]

    store.save(Profile(name="GTA renamed"), first)  # saving under an id keeps it
    assert store.ids() == ["gta", "gta-2"]

    store.delete(first)
    assert store.ids() == ["gta-2"]


def test_path_traversal_is_rejected(store: ProfileStore) -> None:
    with pytest.raises(ValueError):
        store.load("../config")


def test_load_all_skips_broken_files(store: ProfileStore) -> None:
    store.save(Profile(name="Good"))
    (store.directory / "broken.json").write_text("[]", encoding="utf-8")
    assert list(store.load_all()) == ["good"]
    with pytest.raises(StorageError):
        store.load("broken")


def test_empty_store(store: ProfileStore) -> None:
    assert store.ids() == []
    assert store.load_all() == {}
