"""Rejecting reads that are not a subtitle: scenery picked up as glyphs, lone marks, noise.

Each check names its reason so the calibration Inspector can show why a read was dropped.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

MIN_LETTERS = 2
MIN_LETTER_RATIO = 0.5
# Letter runs at least this long must contain a vowel ("Hmm", "Shh" are shorter; "Wsffz" is not).
VOWELLESS_RUN = 5
_VOWELS = set("aeiouyAEIOUYàáâãäåèéêëìíîïòóôõöùúûüýÿÀÁÂÃÄÅÈÉÊËÌÍÎÏÒÓÔÕÖÙÚÛÜÝ")
# The same non-alphanumeric character four or more times in a row: "||||", "----", "....." etc.
_REPEATED_JUNK = re.compile(r"([^\w\s.])\1{3,}|\.{5,}")
_LETTER_RUN = re.compile(r"[^\W\d_]+")


class Reject(StrEnum):
    EMPTY = "empty"
    TOO_SHORT = "too_short"
    FEW_LETTERS = "few_letters"
    NO_VOWELS = "no_vowels"
    REPEATED_JUNK = "repeated_junk"


@dataclass(frozen=True, slots=True)
class Verdict:
    reason: Reject | None  # None = plausible subtitle

    @property
    def ok(self) -> bool:
        return self.reason is None


def check(text: str) -> Verdict:
    """Judge cleaned OCR text (see ``normalize.clean``)."""
    visible = [ch for ch in text if not ch.isspace()]
    if not visible:
        return Verdict(Reject.EMPTY)
    letters = [ch for ch in visible if ch.isalpha()]
    if len(letters) < MIN_LETTERS:
        return Verdict(Reject.TOO_SHORT)
    # Before the letter ratio: junk runs lower the ratio too, but this names the cause.
    if _REPEATED_JUNK.search(text):
        return Verdict(Reject.REPEATED_JUNK)
    if len(letters) / len(visible) < MIN_LETTER_RATIO:
        return Verdict(Reject.FEW_LETTERS)
    if any(
        len(run) >= VOWELLESS_RUN and not _VOWELS.intersection(run)
        for run in _LETTER_RUN.findall(text)
    ):
        return Verdict(Reject.NO_VOWELS)
    return Verdict(None)
