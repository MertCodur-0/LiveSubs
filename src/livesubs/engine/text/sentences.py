"""Sentences split over several subtitles.

Games cut long sentences into pieces ("I knew Fisk" / "wouldn't go quietly."). Translated one by
one, the first piece means something else ("Fisk'i tanıyordum."), so the pipeline translates a
continuation together with the piece before it. A piece continues the previous one only when
both signs agree: the previous piece has no sentence end, and the new one starts in lower case.
Games without punctuation or in capitals therefore never merge.
"""

from __future__ import annotations

SENTENCE_ENDS = ".!?"
# Closing marks that may follow the sentence end ('"Go!"', "(Sighs.)").
CLOSERS = "\"')]"
# An interrupted line ("Location--") usually hands over to another speaker.
INTERRUPTED = "-"
# Pieces in one merged sentence, so a game that never punctuates cannot grow it forever.
MAX_PIECES = 3


def is_open(text: str) -> bool:
    """``text`` stops in the middle of a sentence (no end mark, not interrupted)."""
    stripped = text.rstrip().rstrip(CLOSERS)
    return bool(stripped) and stripped[-1] not in SENTENCE_ENDS + INTERRUPTED


def starts_lower(text: str) -> bool:
    """The first letter of ``text`` is lower case ("...and he's", "wouldn't go")."""
    return next((c.islower() for c in text if c.isalpha()), False)


def continues(previous: str, text: str) -> bool:
    return is_open(previous) and starts_lower(text)
