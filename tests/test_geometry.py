import pytest
from pydantic import ValidationError

from livesubs.core.geometry import NormRect, Rect


def test_rect_intersection() -> None:
    a = Rect(0, 0, 100, 50)
    assert a.intersection(Rect(50, 25, 100, 100)) == Rect(50, 25, 50, 25)
    assert a.intersection(Rect(100, 0, 10, 10)) is None  # touching edges do not overlap
    assert a.intersects(Rect(99, 49, 5, 5))


def test_norm_rect_rejects_rect_outside_frame() -> None:
    with pytest.raises(ValidationError):
        NormRect(x=0.5, y=0.0, width=0.6, height=0.5)
    with pytest.raises(ValidationError):
        NormRect(x=0.0, y=0.0, width=0.0, height=0.5)


def test_norm_rect_to_pixels() -> None:
    region = NormRect(x=0.25, y=0.8, width=0.5, height=0.1)
    assert region.to_pixels(1920, 1080) == Rect(480, 864, 960, 108)


def test_norm_rect_to_pixels_never_empty_or_outside() -> None:
    tiny = NormRect(x=0.9999, y=0.9999, width=0.0001, height=0.0001)
    assert tiny.to_pixels(100, 100) == Rect(99, 99, 1, 1)


def test_norm_rect_survives_resolution_change() -> None:
    region = NormRect.from_pixels(Rect(480, 864, 960, 108), 1920, 1080)
    assert region.to_pixels(3840, 2160) == Rect(960, 1728, 1920, 216)


def test_from_pixels_clips_to_frame() -> None:
    region = NormRect.from_pixels(Rect(-100, 900, 400, 400), 1000, 1000)
    assert region.to_pixels(1000, 1000) == Rect(0, 900, 300, 100)
    with pytest.raises(ValueError):
        NormRect.from_pixels(Rect(2000, 0, 10, 10), 1000, 1000)
