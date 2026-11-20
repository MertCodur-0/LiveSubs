"""Speaker names in front of a subtitle ("Lucia: You know, ...").

Games that name the speaker (GTA VI with speaker names on) draw the name in the character's own
colour and the sentence in white; lines without a name are people in the background (drawn
grey in GTA VI). A named line is the dialogue the player follows: when both are on screen, only
the named lines are translated and shown (``engine.pipeline``), and the translation carries the
name in its colour. Only the sentence after the name goes to the model; the name is never
translated.
"""

from __future__ import annotations

import re

# One to three capitalised words before a colon and a space: "Lucia:", "Cal:", "Officer Diaz:",
# "Man 2:" (not "10:30").
_SPEAKER = re.compile(r"^\s*([A-Z][\w'.\-]*(?:\s+[A-Z0-9][\w'.\-]*){0,2})\s*:\s+(\S.*)$")
# Labels that look like a name but are none ("In Spanish: Hola." is a language note).
NOT_NAMES = frozenset({"in", "on", "at", "note", "translation", "subtitles"})
MAX_NAME_LENGTH = 24


def split(text: str) -> tuple[str, str]:
    """``(speaker, sentence)``; the speaker is "" when ``text`` names none."""
    match = _SPEAKER.match(text)
    if match is None:
        return "", text
    name, sentence = match.group(1).strip(), match.group(2).strip()
    first = name.split()[0].casefold()
    if (
        first in NOT_NAMES
        or len(name) > MAX_NAME_LENGTH
        or not any(c.isalpha() for c in name)
        or not any(c.isalpha() for c in sentence)
    ):
        return "", text
    return name, sentence


def speaker(text: str) -> str:
    return split(text)[0]
