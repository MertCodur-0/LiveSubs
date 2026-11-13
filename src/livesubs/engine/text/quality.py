"""How much an OCR read looks like a real English subtitle.

Windows OCR misreads the same line differently from frame to frame ("Heyl Get outl Nowl",
"Hey! Get out! Now!"). Reading two images per frame and several frames per line gives several
candidates; ``ReadScorer.score`` ranks them: every plausible word counts a little, every
implausible thing (a word the dictionary does not know, letters mixed with digits, a stray letter,
an odd symbol) costs much more. So a complete clean read beats a partial clean read, and a clean
read beats a longer garbled one.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable

from livesubs.engine.text.normalize import clean

WORD_VALUE = 0.2  # per plausible word
_WORD = re.compile(r"[A-Za-z0-9'\-]+")
_ODD = re.compile(r"[^A-Za-z0-9\s.,!?'\"\-:;()]")
_CONTRACTION = re.compile(r"[A-Za-z]+'[A-Za-z]+")  # slang the dictionary lacks: I'ma, cops'll
_SINGLE_LETTERS = {"I", "a", "A", "O"}

Misspelled = Callable[[str], Iterable[str]]


def penalties(text: str, misspelled: Iterable[str] = ()) -> tuple[float, int]:
    """``(penalty, plausible words)`` of an already cleaned read."""
    unknown = set(misspelled)
    penalty, good = 0.0, 0
    for word in _WORD.findall(text):
        core = word.strip("'-")
        if not core:
            penalty += 1
        elif any(c.isdigit() for c in core) and any(c.isalpha() for c in core):
            penalty += 1  # "Yeah0", "J0ints"
        elif len(core) == 1 and core.isalpha() and core not in _SINGLE_LETTERS:
            penalty += 1  # "government s"
        elif word in unknown or core in unknown:
            penalty += 0.3 if _CONTRACTION.fullmatch(core) else 1
        else:
            if word.endswith("'") and not core.endswith(("in", "s")):
                penalty += 0.5  # "Stop'" (a misread "!"); "playin'" and "boys'" are fine
            good += 1
    return penalty + len(_ODD.findall(text)), good


class ReadScorer:
    """``score(raw_read)``; higher is better. ``misspelled`` is a dictionary check (Windows'
    spell checker in the app); without one only the shape rules apply."""

    def __init__(self, misspelled: Misspelled | None = None) -> None:
        self._misspelled = misspelled
        self._cache: dict[str, float] = {}

    def score(self, text: str) -> float:
        cached = self._cache.get(text)
        if cached is not None:
            return cached
        cleaned = clean(text)
        unknown = self._misspelled(cleaned) if self._misspelled and cleaned else ()
        penalty, good = penalties(cleaned, unknown)
        value = good * WORD_VALUE - penalty
        if len(self._cache) > 512:
            self._cache.clear()
        self._cache[text] = value
        return value

    def best(self, reads: list[str]) -> str:
        """The highest-scoring read; the first one on a tie."""
        return max(reads, key=self.score) if reads else ""
