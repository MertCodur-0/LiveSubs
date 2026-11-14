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
# Punctuation between two letters ("Stop!h'he", "the.weapon", "t)ie"): a misread glyph or a lost
# space. Initialisms ("U.S.", "a.m.") are single letters between dots, so they do not match.
_GLUED = re.compile(
    r"(?<=[A-Za-z]{2})[.!?,;:()\"](?=[A-Za-z])|(?<=[A-Za-z])[!?,;:()\"](?=[A-Za-z])"
)
# Runs of sentence punctuation: dots, then "!" and "?" ("...", "?!", "a.m.?", "Wait...?") are
# real ones; "met:.", "Stop!." are specks read as marks.
_PUNCT_RUN = re.compile(r"[.,!?:;]{2,}")
_GOOD_RUN = re.compile(r"\.*[!?]*")
# A dropped g ("walkin'", "lovin'"): the dictionary knows only the full form.
_DROPPED_G = re.compile(r"[A-Za-z]+in'")

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
        elif _CONTRACTION.fullmatch(core) and (word in unknown or core in unknown):
            # Slang ("I'ma") is a word too, if a doubtful one: a dictionary word read in its
            # place ("lima") must not win on score alone. An unknown possessive is more often a
            # garbled word ("Tltnat's") than a name.
            penalty += 0.3
            good += not core.lower().endswith("'s")
        elif word in unknown or core in unknown or unknown.intersection(core.split("-")):
            penalty += 1  # the dictionary reports one part of a hyphenated word ("Uh-uhr")
        else:
            if word.endswith("'") and not core.endswith(("in", "s")):
                penalty += 0.5  # "Stop'" (a misread "!"); "playin'" and "boys'" are fine
            good += 1
    penalty += len(_ODD.findall(text)) + len(_GLUED.findall(text))
    # A mixed run of punctuation ("met:.", "Stop!."): specks read as marks.
    penalty += 0.5 * sum(not _GOOD_RUN.fullmatch(run) for run in _PUNCT_RUN.findall(text))
    # A lone double quote or bracket ('one") You'): a misread "?" or a speck of background.
    penalty += 0.5 * (text.count('"') % 2 + abs(text.count("(") - text.count(")")))
    return penalty, good


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
        if unknown:
            unknown = self._known_g_dropped(cleaned, unknown)
        penalty, good = penalties(cleaned, unknown)
        value = good * WORD_VALUE - penalty
        if len(self._cache) > 512:
            self._cache.clear()
        self._cache[text] = value
        return value

    def _known_g_dropped(self, text: str, unknown: Iterable[str]) -> list[str]:
        """``unknown`` without the words that are a known word with its g dropped."""
        unknown = list(unknown)
        dropped = {w[:-1] for w in _DROPPED_G.findall(text)}
        for word in dropped.intersection(unknown):
            if not self._misspelled(word + "g"):
                unknown = [u for u in unknown if u not in (word, word + "'")]
        return unknown

    def best(self, reads: list[str]) -> str:
        """The highest-scoring read; the first one on a tie."""
        return max(reads, key=self.score) if reads else ""
