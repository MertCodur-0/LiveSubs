"""Loader for the screenshot samples described in ``tests/samples/samples.json``."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
import pytest

from livesubs.core.geometry import NormRect
from livesubs.core.profiles import VisionSettings

SAMPLES_DIR = Path(__file__).parent / "samples"


@dataclass(frozen=True)
class Sample:
    path: Path
    region: NormRect
    vision: VisionSettings
    expected: str
    reference_tr: str
    # Allowed mask components per glyph; widened only for documented hard samples.
    component_ratio: tuple[float, float] = (0.9, 1.25)

    @property
    def name(self) -> str:
        return self.path.stem

    @property
    def has_subtitle(self) -> bool:
        return bool(self.expected)

    def load(self) -> np.ndarray:
        """The full screenshot (BGR); skips the test when the image is not present."""
        if not self.path.is_file():
            pytest.skip(f"sample image not available: {self.path.name}")
        # cv2.imread cannot open non-ASCII paths on Windows.
        return cv2.imdecode(np.fromfile(self.path, np.uint8), cv2.IMREAD_COLOR)

    def load_region(self) -> np.ndarray:
        image = self.load()
        r = self.region.to_pixels(image.shape[1], image.shape[0])
        return image[r.y : r.bottom, r.x : r.right]


def load_samples() -> list[Sample]:
    spec = json.loads((SAMPLES_DIR / "samples.json").read_text(encoding="utf-8"))
    defaults = spec.get("defaults", {})
    samples = []
    for entry in spec["samples"]:
        x, y, width, height = entry.get("region", defaults["region"])
        vision = {**defaults.get("vision", {}), **entry.get("vision", {})}
        samples.append(
            Sample(
                path=SAMPLES_DIR / entry["file"],
                region=NormRect(x=x, y=y, width=width, height=height),
                vision=VisionSettings(**vision),
                expected=entry["expected"],
                reference_tr=entry.get("reference_tr", ""),
                component_ratio=tuple(entry.get("component_ratio", (0.9, 1.25))),
            )
        )
    return samples


# Curly single/double quotes and the ellipsis character, by code point.
_FOLD = str.maketrans(
    {chr(0x2018): "'", chr(0x2019): "'", chr(0x201C): '"', chr(0x201D): '"', chr(0x2026): "..."}
)


def fold(text: str) -> str:
    """Typographic quotes/ellipsis to ASCII and collapsed whitespace, for comparing OCR output."""
    return " ".join(text.translate(_FOLD).split())


def sample_params(samples: list[Sample]) -> list[object]:
    return [pytest.param(s, id=s.name[-6:]) for s in samples]
